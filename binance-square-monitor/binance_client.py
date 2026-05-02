"""币安合约认证 API 客户端（USDT-M 永续）

用于实盘交易的签名请求。公开行情接口仍在 market.py 中。

签名方式：HMAC-SHA256
依赖：无额外依赖（urllib + hmac + hashlib）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError, URLError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FAPI_BASE = "https://fapi.binance.com"
PAPI_BASE = "https://papi.binance.com"

# 交易对精度缓存
_PRECISION_CACHE: dict = {"ts": 0.0, "data": {}}
_PRECISION_TTL = 3600


def _log(msg: str):
    print(f"[binance-client] {msg}", file=sys.stderr, flush=True)


class BinanceAPIError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"Binance API error {code}: {msg}")


class BinanceFuturesClient:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "BINANCE_API_KEY 和 BINANCE_API_SECRET 未配置。"
                "请在 .env 文件中设置或通过环境变量传入。"
            )
        self.recv_window = self._load_recv_window()
        self._time_offset_ms = 0
        self.account_mode = (
            os.getenv("BINANCE_DERIVATIVES_ACCOUNT_MODE", "unified").strip().lower()
        )
        self.use_unified_account = self.account_mode in {"unified", "portfolio", "papi"}
        self.position_mode = (
            os.getenv("BINANCE_POSITION_MODE")
            or os.getenv("BINANCE_FUTURES_POSITION_MODE")
            or "hedge"
        ).strip().lower()
        self.use_hedge_mode = self.position_mode in {
            "hedge", "dual", "dual_side", "dual-side", "hedge_mode"
        }
        self.proxy_url = self._resolve_proxy_url()
        self._direct_opener = build_opener()
        if self.proxy_url:
            proxy_handler = ProxyHandler({
                "http": self.proxy_url,
                "https": self.proxy_url,
            })
            self._auth_opener = build_opener(proxy_handler)
            _log(f"已启用代理: {self.proxy_url.rsplit('@', 1)[-1]}")
        else:
            self._auth_opener = self._direct_opener
        self._sync_server_time_offset()

    def _api_base(self) -> str:
        return PAPI_BASE if self.use_unified_account else FAPI_BASE

    def _endpoint(self, standard_path: str, unified_path: str) -> str:
        return unified_path if self.use_unified_account else standard_path

    def _long_position_side(self) -> str | None:
        return "LONG" if self.use_hedge_mode else None

    def _reduce_only(self, value: bool) -> str | None:
        # Binance hedge mode rejects reduceOnly on UM orders; positionSide=LONG
        # is enough to make SELL orders reduce the long leg.
        if self.use_hedge_mode:
            return None
        return "true" if value else "false"

    def _select_opener(self, signed: bool, path: str):
        """交易类签名请求走代理；账户查询和公开行情默认直连。"""
        if signed and self.proxy_url and self._is_order_related_path(path):
            return self._auth_opener
        return self._direct_opener

    @staticmethod
    def _is_order_related_path(path: str) -> bool:
        p = (path or "").lower()
        order_prefixes = (
            "/fapi/v1/order",
            "/fapi/v1/allopenorders",
            "/fapi/v1/batchorders",
            "/fapi/v1/leverage",
            "/fapi/v1/margintype",
            "/papi/v1/um/order",
            "/papi/v1/um/algo/order",
            "/papi/v1/um/algo/openAlgoOrders",
            "/papi/v1/um/algo/allAlgoOrders",
            "/papi/v1/um/allopenorders",
            "/papi/v1/um/batchorders",
            "/papi/v1/um/leverage",
        )
        return any(p.startswith(prefix) for prefix in order_prefixes)

    @staticmethod
    def _load_recv_window() -> int:
        raw = (
            os.getenv("BINANCE_RECV_WINDOW_MS")
            or os.getenv("BINANCE_RECV_WINDOW")
            or "10000"
        )
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = 10000
        if val <= 0:
            val = 10000
        return min(val, 60000)

    @staticmethod
    def _resolve_proxy_url() -> str:
        use_proxy = os.getenv("BINANCE_USE_PROXY", "true").strip().lower()
        if use_proxy in {"0", "false", "no", "off"}:
            _log("BINANCE_USE_PROXY=false，鉴权请求将不使用代理")
            return ""

        raw = (
            os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("PROXY")
            or os.getenv("HTTP_PROXY")
            or os.getenv("http_proxy")
            or ""
        ).strip()
        if not raw:
            return ""
        if "://" in raw:
            return raw
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
        return f"http://{raw}"

    def _sync_server_time_offset(self):
        """校准本地时间与交易所时间偏移，降低 -1021 风险。"""
        req = Request(
            f"{FAPI_BASE}/fapi/v1/time",
            headers={"User-Agent": "binance-square-monitor/1.0"},
            method="GET",
        )
        last_err = None
        for opener in (self._direct_opener, self._auth_opener):
            try:
                with opener.open(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                server_ms = int(data.get("serverTime", 0))
                local_ms = int(time.time() * 1000)
                if server_ms > 0:
                    self._time_offset_ms = server_ms - local_ms
                    return
            except Exception as e:
                last_err = e
        _log(f"时间校准失败，继续使用本地时钟: {last_err}")

    def _sign(self, params: dict) -> dict:
        params.setdefault("recvWindow", self.recv_window)
        params["timestamp"] = int(time.time() * 1000) + int(self._time_offset_ms)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = True, retries: int = 3,
                 base_url: str | None = None) -> dict | list:
        base_params = dict(params or {})
        url = f"{base_url or self._api_base()}{path}"

        headers = {
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": "binance-square-monitor/1.0",
        }

        last_err = None
        for attempt in range(retries):
            req_params = dict(base_params)
            if signed:
                req_params = self._sign(req_params)

            if method == "GET" or method == "DELETE":
                req_url = url
                if req_params:
                    req_url = f"{url}?{urlencode(req_params)}"
                body = None
            else:
                req_url = url
                body = urlencode(req_params).encode()

            req_headers = dict(headers)
            if body:
                req_headers["Content-Type"] = "application/x-www-form-urlencoded"

            try:
                req = Request(req_url, data=body, headers=req_headers, method=method)
                opener = self._select_opener(signed, path)
                with opener.open(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except HTTPError as e:
                resp_body = e.read().decode() if e.fp else ""
                try:
                    err = json.loads(resp_body)
                    code = err.get("code", e.code)
                    msg = err.get("msg", resp_body)
                except (json.JSONDecodeError, ValueError):
                    code = e.code
                    msg = resp_body

                # 不可重试的错误：直接抛出
                if e.code in (400, 401, 403):
                    # -1021: 时间偏移。先校准时间，再走重试。
                    if code == -1021 and attempt < retries - 1:
                        new_window = min(max(self.recv_window * 2, 10000), 60000)
                        if new_window != self.recv_window:
                            _log(f"收到 -1021，放宽 recvWindow: {self.recv_window} -> {new_window}")
                            self.recv_window = new_window
                        _log("收到 -1021，正在校准服务器时间后重试")
                        self._sync_server_time_offset()
                        continue
                    raise BinanceAPIError(code, msg)

                # 429 限频：等待后重试
                if e.code == 429:
                    wait = min(2 ** attempt * 2, 30)
                    _log(f"限频 429，等待 {wait}s 后重试 ({attempt+1}/{retries})")
                    time.sleep(wait)
                    last_err = BinanceAPIError(code, msg)
                    continue

                # 5xx 服务器错误：重试
                if e.code >= 500:
                    wait = 2 ** attempt
                    _log(f"服务器错误 {e.code}，{wait}s 后重试 ({attempt+1}/{retries})")
                    time.sleep(wait)
                    last_err = BinanceAPIError(code, msg)
                    continue

                raise BinanceAPIError(code, msg)
            except (URLError, OSError, TimeoutError) as e:
                wait = 2 ** attempt
                _log(f"网络错误: {e}，{wait}s 后重试 ({attempt+1}/{retries})")
                time.sleep(wait)
                last_err = e
                continue

        raise last_err or RuntimeError("请求失败")

    # === 交易对精度 ===

    def get_exchange_info(self) -> dict:
        """获取交易所信息（交易对精度、限制等），带缓存。"""
        global _PRECISION_CACHE
        now = time.time()
        if now - _PRECISION_CACHE["ts"] < _PRECISION_TTL and _PRECISION_CACHE["data"]:
            return _PRECISION_CACHE["data"]
        data = self._request(
            "GET", "/fapi/v1/exchangeInfo", signed=False, base_url=FAPI_BASE
        )
        result = {}
        for s in data.get("symbols", []):
            sym = s.get("symbol", "")
            result[sym] = {
                "quantityPrecision": s.get("quantityPrecision", 3),
                "pricePrecision": s.get("pricePrecision", 2),
                "filters": {f["filterType"]: f for f in s.get("filters", [])},
                "status": s.get("status"),
            }
        _PRECISION_CACHE = {"ts": now, "data": result}
        return result

    def get_symbol_precision(self, symbol: str) -> tuple[int, int]:
        """返回 (quantityPrecision, pricePrecision)"""
        info = self.get_exchange_info()
        sym_info = info.get(symbol.upper(), {})
        return (
            sym_info.get("quantityPrecision", 3),
            sym_info.get("pricePrecision", 2),
        )

    def round_quantity(self, symbol: str, quantity: float) -> float:
        qty_prec, _ = self.get_symbol_precision(symbol)
        return round(quantity, qty_prec)

    def round_price(self, symbol: str, price: float) -> float:
        _, price_prec = self.get_symbol_precision(symbol)
        return round(price, price_prec)

    # === 账户 ===

    def get_account(self) -> dict:
        """GET /fapi/v2/account — 账户信息（余额+持仓）"""
        path = self._endpoint("/fapi/v2/account", "/papi/v2/um/account")
        return self._request("GET", path)

    def get_balance(self) -> dict:
        """获取 USDT 余额摘要"""
        if self.use_unified_account:
            data = self._request("GET", "/papi/v1/balance")
            rows = data if isinstance(data, list) else [data]
            for item in rows:
                if item.get("asset") == "USDT":
                    um_wallet = float(item.get("umWalletBalance", 0) or 0)
                    cross_free = float(item.get("crossMarginFree", 0) or 0)
                    total_wallet = float(item.get("totalWalletBalance", 0) or 0)
                    # 统一账户下，资金可能在 cross 维度而非 umWalletBalance。
                    # 实盘下单的可用资金优先取 umWalletBalance，若为 0 则回退到 crossMarginFree。
                    available = um_wallet if um_wallet > 0 else cross_free
                    # 展示余额优先取可用资金，若仍为 0 再回退到 totalWalletBalance。
                    wallet = available if available > 0 else total_wallet
                    unrealized = float(item.get("umUnrealizedPNL", 0) or 0)
                    if unrealized == 0:
                        unrealized = float(item.get("crossUnPnl", 0) or 0)
                    return {
                        "balance": wallet,
                        "available": available,
                        "unrealized_pnl": unrealized,
                    }
            return {"balance": 0, "available": 0, "unrealized_pnl": 0}

        data = self._request("GET", "/fapi/v2/balance")
        for item in data:
            if item.get("asset") == "USDT":
                return {
                    "balance": float(item.get("balance", 0)),
                    "available": float(item.get("availableBalance", 0)),
                    "unrealized_pnl": float(item.get("crossUnPnl", 0)),
                }
        return {"balance": 0, "available": 0, "unrealized_pnl": 0}

    def get_position_risk(self, symbol: str = "") -> list[dict]:
        """GET /fapi/v2/positionRisk — 持仓风险"""
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        path = self._endpoint("/fapi/v2/positionRisk", "/papi/v1/um/positionRisk")
        return self._request("GET", path, params)

    @staticmethod
    def _is_html_404_error(err: BinanceAPIError) -> bool:
        body = str(err.msg or "").lower()
        return err.code == 404 and ("<!doctype html" in body or "<html" in body)

    @staticmethod
    def _is_permission_error(err: BinanceAPIError) -> bool:
        return err.code in {-2015, -2014, -1002}

    def _probe_invalid_order_route(self, path: str, params: dict, label: str) -> None:
        """Probe a signed trade route without placing a real order.

        PAPI has no order/test endpoint, so we use an intentionally invalid
        symbol. A JSON validation error means auth and routing reached Binance;
        HTML 404 or permission errors mean live trading is not safe to start.
        """
        try:
            self._request("POST", path, params, signed=True, retries=1)
        except BinanceAPIError as e:
            if self._is_html_404_error(e):
                raise RuntimeError(f"{label}接口不可用（HTML 404）: {e}") from e
            if self._is_permission_error(e):
                raise RuntimeError(f"{label}权限不可用: {e}") from e
            return
        raise RuntimeError(f"{label}探测请求意外成功，请立即检查是否产生了订单")

    def validate_live_order_routes(self) -> None:
        """Validate live order and protective-order routes before auto trading.

        This intentionally avoids real symbols/orders for PAPI. It prevents the
        bot from opening positions when stop-loss/take-profit endpoints are not
        reachable for the current account/IP/API-key combination.
        """
        if self.use_unified_account:
            self._probe_invalid_order_route(
                "/papi/v1/um/order",
                {
                    "symbol": "NOTAREALUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": "0.001",
                    "price": "1",
                },
                "统一账户下单",
            )
            self._probe_invalid_order_route(
                "/papi/v1/um/algo/order",
                {
                    "symbol": "NOTAREALUSDT",
                    "side": "SELL",
                    "positionSide": self._long_position_side(),
                    "algoType": "CONDITIONAL",
                    "type": "STOP_MARKET",
                    "quantity": "1",
                    "triggerPrice": "1",
                    "workingType": "MARK_PRICE",
                },
                "统一账户条件单",
            )
            return

        # Standard UM futures provides a real test endpoint that never places orders.
        self._request("POST", "/fapi/v1/order/test", {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "0.001",
            "price": "1",
        }, signed=True, retries=1)

    # === 杠杆 ===

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """POST /fapi/v1/leverage"""
        path = self._endpoint("/fapi/v1/leverage", "/papi/v1/um/leverage")
        return self._request("POST", path, {
            "symbol": symbol.upper(),
            "leverage": leverage,
        })

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """POST /fapi/v1/marginType — CROSSED 或 ISOLATED"""
        if self.use_unified_account:
            # Portfolio Margin(统一账户)未提供等价的 UM marginType 切换接口，视为无需显式设置。
            return {"msg": "统一账户模式跳过 marginType 设置"}
        try:
            return self._request("POST", "/fapi/v1/marginType", {
                "symbol": symbol.upper(),
                "marginType": margin_type,
            })
        except BinanceAPIError as e:
            # -4046: No need to change margin type (已经是目标类型)
            if e.code == -4046:
                return {"msg": "已是目标保证金模式"}
            raise

    # === 下单 ===

    def place_order(self, **params) -> dict:
        """POST /fapi/v1/order — 通用下单"""
        clean = {k: v for k, v in params.items() if v is not None}
        path = self._endpoint("/fapi/v1/order", "/papi/v1/um/order")
        return self._request("POST", path, clean)

    def place_conditional_order(self, **params) -> dict:
        """Place protective conditional orders.

        Portfolio Margin uses the current UM Algo endpoint. The older
        /papi/v1/um/conditional/order endpoint is deprecated and may return
        Binance's HTML 404 page.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        path = self._endpoint("/fapi/v1/order", "/papi/v1/um/algo/order")
        return self._request("POST", path, clean)

    def market_buy(self, symbol: str, quantity: float) -> dict:
        """市价做多"""
        qty = self.round_quantity(symbol, quantity)
        if qty <= 0:
            raise BinanceAPIError(-1, f"数量精度修正后为 0（原始: {quantity}）")
        return self.place_order(
            symbol=symbol.upper(),
            side="BUY",
            positionSide=self._long_position_side(),
            type="MARKET",
            quantity=qty,
        )

    def market_sell(self, symbol: str, quantity: float, reduce_only: bool = True) -> dict:
        """市价卖出（平多）"""
        qty = self.round_quantity(symbol, quantity)
        if qty <= 0:
            raise BinanceAPIError(-1, f"数量精度修正后为 0（原始: {quantity}）")
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "positionSide": self._long_position_side(),
            "type": "MARKET",
            "quantity": qty,
        }
        reduce_only_value = self._reduce_only(reduce_only)
        if reduce_only_value is not None:
            params["reduceOnly"] = reduce_only_value
        return self.place_order(**params)

    def stop_market_sell(self, symbol: str, quantity: float, stop_price: float) -> dict:
        """止损单：价格到 stop_price 时市价卖出"""
        qty = self.round_quantity(symbol, quantity)
        sp = self.round_price(symbol, stop_price)
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "positionSide": self._long_position_side(),
            "quantity": qty,
            "workingType": "MARK_PRICE",
        }
        if self.use_unified_account:
            params["algoType"] = "CONDITIONAL"
            params["type"] = "STOP_MARKET"
            params["triggerPrice"] = sp
        else:
            params["type"] = "STOP_MARKET"
            params["stopPrice"] = sp
        reduce_only_value = self._reduce_only(True)
        if reduce_only_value is not None:
            params["reduceOnly"] = reduce_only_value
        if self.use_unified_account:
            return self.place_conditional_order(**params)
        return self.place_order(**params)

    def take_profit_market_sell(self, symbol: str, quantity: float, stop_price: float) -> dict:
        """止盈单：价格到 stop_price 时市价卖出"""
        qty = self.round_quantity(symbol, quantity)
        sp = self.round_price(symbol, stop_price)
        params = {
            "symbol": symbol.upper(),
            "side": "SELL",
            "positionSide": self._long_position_side(),
            "quantity": qty,
            "workingType": "MARK_PRICE",
        }
        if self.use_unified_account:
            params["algoType"] = "CONDITIONAL"
            params["type"] = "TAKE_PROFIT_MARKET"
            params["triggerPrice"] = sp
        else:
            params["type"] = "TAKE_PROFIT_MARKET"
            params["stopPrice"] = sp
        reduce_only_value = self._reduce_only(True)
        if reduce_only_value is not None:
            params["reduceOnly"] = reduce_only_value
        if self.use_unified_account:
            return self.place_conditional_order(**params)
        return self.place_order(**params)

    # === 查单 / 撤单 ===

    def get_order(self, symbol: str, order_id: int) -> dict:
        """GET /fapi/v1/order"""
        path = self._endpoint("/fapi/v1/order", "/papi/v1/um/order")
        return self._request("GET", path, {
            "symbol": symbol.upper(),
            "orderId": order_id,
        })

    def get_open_orders(self, symbol: str = "") -> list[dict]:
        """GET /fapi/v1/openOrders"""
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        path = self._endpoint("/fapi/v1/openOrders", "/papi/v1/um/openOrders")
        orders = self._request("GET", path, params)
        if not self.use_unified_account:
            return orders

        algo_params = dict(params)
        algo_params["algoType"] = "CONDITIONAL"
        algo_orders = self._request("GET", "/papi/v1/um/algo/openAlgoOrders", algo_params)
        merged = list(orders if isinstance(orders, list) else [])
        for algo in algo_orders if isinstance(algo_orders, list) else []:
            row = dict(algo)
            row["orderId"] = int(row.get("algoId"))
            row["status"] = row.get("algoStatus", "NEW")
            row["type"] = row.get("orderType")
            merged.append(row)
        return merged

    def get_conditional_order_status(self, symbol: str, algo_id: int) -> dict:
        """Query current UM Algo order status by algoId."""
        if not self.use_unified_account:
            return self.get_order(symbol, algo_id)
        rows = self._request("GET", "/papi/v1/um/algo/allAlgoOrders", {
            "symbol": symbol.upper(),
            "algoId": int(algo_id),
            "limit": 1,
        })
        if isinstance(rows, list) and rows:
            row = dict(rows[0])
            status = row.get("algoStatus", "UNKNOWN")
            mapped = {
                "ACTIVE": "NEW",
                "NEW": "NEW",
                "CANCELED": "CANCELED",
                "EXPIRED": "EXPIRED",
                "TRIGGERED": "FILLED",
                "FINISHED": "FILLED",
            }.get(status, status)
            row["orderId"] = int(row.get("algoId"))
            row["status"] = mapped
            row["avgPrice"] = row.get("actualPrice") or "0"
            row["executedQty"] = row.get("quantity") or "0"
            return row
        return {"orderId": int(algo_id), "status": "UNKNOWN"}

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """DELETE /fapi/v1/order"""
        path = self._endpoint("/fapi/v1/order", "/papi/v1/um/order")
        return self._request("DELETE", path, {
            "symbol": symbol.upper(),
            "orderId": order_id,
        })

    def cancel_conditional_order(self, symbol: str, strategy_id: int) -> dict:
        """取消条件单；普通 futures 条件单仍是普通 order。"""
        if not self.use_unified_account:
            return self.cancel_order(symbol, strategy_id)
        return self._request("DELETE", "/papi/v1/um/algo/order", {
            "algoId": int(strategy_id),
        })

    def cancel_all_orders(self, symbol: str) -> dict:
        """DELETE /fapi/v1/allOpenOrders"""
        path = self._endpoint("/fapi/v1/allOpenOrders", "/papi/v1/um/allOpenOrders")
        return self._request("DELETE", path, {
            "symbol": symbol.upper(),
        })

    # === 连接测试 ===

    def ping(self) -> bool:
        """测试 API 连通性和权限"""
        try:
            self.get_balance()
            return True
        except Exception:
            return False
