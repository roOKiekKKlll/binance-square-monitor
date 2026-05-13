"""临时脚本：验证 Binance API Key 权限（不会真实下单）。

用法：
    python _check_api_permissions.py

可选环境变量：
    CHECK_SYMBOL=BTCUSDT
    CHECK_TEST_QTY=0.001
"""
from __future__ import annotations

import json
import os
import traceback

from binance_client import BinanceAPIError, BinanceFuturesClient


def _print_json(title: str, data) -> None:
    print(f"\n=== {title} ===")
    try:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        print(repr(data))


def _run_check(name: str, fn) -> tuple[bool, object]:
    print(f"\n[CHECK] {name}")
    try:
        result = fn()
        _print_json(name, result)
        return True, result
    except Exception as e:
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False, e


def _build_test_order_params(client: BinanceFuturesClient, symbol: str, raw_qty: float) -> dict:
    raw_price = float(os.getenv("CHECK_TEST_PRICE", "50000"))
    price = raw_price
    qty = client.round_quantity(symbol, raw_qty)
    if qty <= 0:
        qty = raw_qty
    return {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": price,
    }


def _build_unified_trade_probe_params() -> dict:
    """统一账户 PAPI 没有 order/test，用无效交易对探测鉴权与路由，避免真实成交。"""
    return {
        "symbol": os.getenv("CHECK_INVALID_SYMBOL", "NOTAREALUSDT").strip().upper(),
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": "0.001",
        "price": "1",
    }


def _check_trade_permission(client: BinanceFuturesClient, symbol: str, qty: float) -> tuple[bool, str]:
    if getattr(client, "use_unified_account", False):
        path = "/papi/v1/um/order"
        params = _build_unified_trade_probe_params()
        probe_name = "unified_order_probe_params"
    else:
        path = "/fapi/v1/order/test"
        params = _build_test_order_params(client, symbol, qty)
        probe_name = "order_test_params"
    try:
        _print_json(probe_name, params)
        resp = client._request("POST", path, params, signed=True, retries=1)
        _print_json("order_test_response", resp)
        if getattr(client, "use_unified_account", False):
            return False, "统一账户探测请求意外成功，请立即检查是否产生了订单"
        return True, "交易权限可用（test order 通过）"
    except BinanceAPIError as e:
        if e.code == 404 or "<html" in str(e.msg).lower() or "<!doctype html" in str(e.msg).lower():
            return False, f"交易接口未正确命中 Binance API（错误码 {e.code}: {e.msg}）"
        # 常见权限错误码：-2015/-2014/-1002
        if e.code in (-2015, -2014, -1002):
            return False, f"交易权限疑似不可用（错误码 {e.code}: {e.msg}）"
        # 走到了下单参数校验，通常说明签名/权限已通过
        return True, f"已通过鉴权并进入下单参数校验（错误码 {e.code}: {e.msg}）"
    except Exception as e:
        return False, f"交易权限检测失败：{type(e).__name__}: {e}"


def main() -> None:
    symbol = os.getenv("CHECK_SYMBOL", "BTCUSDT").strip().upper()
    raw_qty = float(os.getenv("CHECK_TEST_QTY", "0.001"))

    print("创建 BinanceFuturesClient ...")
    client = BinanceFuturesClient()
    print(f"account_mode={client.account_mode}")
    print(f"use_unified_account={client.use_unified_account}")
    print(f"api_base={client._api_base()}")
    print(f"proxy_enabled={bool(client.proxy_url)}")
    if client.proxy_url:
        masked = client.proxy_url
        if "@" in masked:
            masked = masked.split("@", 1)[1]
        print(f"proxy={masked}")
    print(f"recvWindow={client.recv_window}")
    print(f"time_offset_ms={client._time_offset_ms}")
    print(f"check_symbol={symbol}, check_test_qty={raw_qty}")

    ok_balance, _ = _run_check("get_balance()", client.get_balance)
    ok_account, _ = _run_check("get_account()", client.get_account)
    ok_pos, _ = _run_check(
        f"get_position_risk('{symbol}')",
        lambda: client.get_position_risk(symbol),
    )
    ok_trade, trade_msg = _check_trade_permission(client, symbol, raw_qty)
    print(f"\n[CHECK] trade_permission => {trade_msg}")

    all_ok = ok_balance and ok_account and ok_pos and ok_trade
    print("\n==============================")
    print("API 权限验证结果:", "PASS" if all_ok else "PARTIAL/FAIL")
    print("==============================")


if __name__ == "__main__":
    main()
