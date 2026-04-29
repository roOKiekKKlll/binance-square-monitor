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
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FAPI_BASE = "https://fapi.binance.com"

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

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = True, retries: int = 3) -> dict | list:
        params = dict(params or {})
        if signed:
            params = self._sign(params)

        url = f"{FAPI_BASE}{path}"
        if method == "GET" or method == "DELETE":
            if params:
                url = f"{url}?{urlencode(params)}"
            body = None
        else:
            body = urlencode(params).encode()

        headers = {
            "X-MBX-APIKEY": self.api_key,
            "User-Agent": "binance-square-monitor/1.0",
        }
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_err = None
        for attempt in range(retries):
            try:
                req = Request(url, data=body, headers=headers, method=method)
                with urlopen(req, timeout=15) as resp:
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
        data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
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
        return self._request("GET", "/fapi/v2/account")

    def get_balance(self) -> dict:
        """获取 USDT 余额摘要"""
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
        return self._request("GET", "/fapi/v2/positionRisk", params)

    # === 杠杆 ===

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """POST /fapi/v1/leverage"""
        return self._request("POST", "/fapi/v1/leverage", {
            "symbol": symbol.upper(),
            "leverage": leverage,
        })

    def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> dict:
        """POST /fapi/v1/marginType — CROSSED 或 ISOLATED"""
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
        return self._request("POST", "/fapi/v1/order", clean)

    def market_buy(self, symbol: str, quantity: float) -> dict:
        """市价做多"""
        qty = self.round_quantity(symbol, quantity)
        if qty <= 0:
            raise BinanceAPIError(-1, f"数量精度修正后为 0（原始: {quantity}）")
        return self.place_order(
            symbol=symbol.upper(),
            side="BUY",
            type="MARKET",
            quantity=qty,
        )

    def market_sell(self, symbol: str, quantity: float, reduce_only: bool = True) -> dict:
        """市价卖出（平多）"""
        qty = self.round_quantity(symbol, quantity)
        if qty <= 0:
            raise BinanceAPIError(-1, f"数量精度修正后为 0（原始: {quantity}）")
        return self.place_order(
            symbol=symbol.upper(),
            side="SELL",
            type="MARKET",
            quantity=qty,
            reduceOnly="true" if reduce_only else "false",
        )

    def stop_market_sell(self, symbol: str, quantity: float, stop_price: float) -> dict:
        """止损单：价格到 stop_price 时市价卖出"""
        qty = self.round_quantity(symbol, quantity)
        sp = self.round_price(symbol, stop_price)
        return self.place_order(
            symbol=symbol.upper(),
            side="SELL",
            type="STOP_MARKET",
            quantity=qty,
            stopPrice=sp,
            reduceOnly="true",
            workingType="MARK_PRICE",
        )

    def take_profit_market_sell(self, symbol: str, quantity: float, stop_price: float) -> dict:
        """止盈单：价格到 stop_price 时市价卖出"""
        qty = self.round_quantity(symbol, quantity)
        sp = self.round_price(symbol, stop_price)
        return self.place_order(
            symbol=symbol.upper(),
            side="SELL",
            type="TAKE_PROFIT_MARKET",
            quantity=qty,
            stopPrice=sp,
            reduceOnly="true",
            workingType="MARK_PRICE",
        )

    # === 查单 / 撤单 ===

    def get_order(self, symbol: str, order_id: int) -> dict:
        """GET /fapi/v1/order"""
        return self._request("GET", "/fapi/v1/order", {
            "symbol": symbol.upper(),
            "orderId": order_id,
        })

    def get_open_orders(self, symbol: str = "") -> list[dict]:
        """GET /fapi/v1/openOrders"""
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/fapi/v1/openOrders", params)

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """DELETE /fapi/v1/order"""
        return self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol.upper(),
            "orderId": order_id,
        })

    def cancel_all_orders(self, symbol: str) -> dict:
        """DELETE /fapi/v1/allOpenOrders"""
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {
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
