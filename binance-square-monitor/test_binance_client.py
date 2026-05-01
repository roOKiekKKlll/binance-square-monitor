import os
import io
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError

import binance_client
from binance_client import BinanceAPIError, BinanceFuturesClient
from _check_api_permissions import _check_trade_permission


class BinanceClientConfigTests(unittest.TestCase):
    def test_sign_adds_default_recv_window_and_time_offset(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {}, clear=False):
                client = BinanceFuturesClient("k", "s")
        client._time_offset_ms = 250
        client.recv_window = 5000

        with patch("time.time", return_value=1000.0):
            signed = client._sign({"symbol": "BTCUSDT"})

        self.assertEqual(signed["timestamp"], 1000250)
        self.assertEqual(signed["recvWindow"], 5000)
        self.assertIn("signature", signed)

    def test_proxy_from_env_supports_auth_tuple_format(self):
        env = {"PROXY": "45.10.209.49:21412:user:pass"}
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")
        self.assertEqual(client.proxy_url, "http://user:pass@45.10.209.49:21412")

    def test_https_proxy_url_has_higher_priority(self):
        env = {
            "PROXY": "45.10.209.49:21412:user:pass",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
        }
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")
        self.assertEqual(client.proxy_url, "http://127.0.0.1:7890")

    def test_proxy_can_be_disabled_by_env_switch(self):
        env = {
            "BINANCE_USE_PROXY": "false",
            "PROXY": "45.10.209.49:21412:user:pass",
        }
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")
        self.assertEqual(client.proxy_url, "")

    def test_proxy_switch_true_keeps_existing_proxy_resolution(self):
        env = {
            "BINANCE_USE_PROXY": "true",
            "PROXY": "45.10.209.49:21412:user:pass",
        }
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")
        self.assertEqual(client.proxy_url, "http://user:pass@45.10.209.49:21412")

    def test_only_trade_related_signed_requests_use_proxy_opener(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"PROXY": "45.10.209.49:21412:user:pass"}, clear=False):
                client = BinanceFuturesClient("k", "s")

        direct_calls = []
        auth_calls = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def _direct_open(req, timeout=0):
            direct_calls.append((req.full_url, timeout))
            return _FakeResp()

        def _auth_open(req, timeout=0):
            auth_calls.append((req.full_url, timeout))
            return _FakeResp()

        client._direct_opener = SimpleNamespace(open=_direct_open)
        client._auth_opener = SimpleNamespace(open=_auth_open)

        client._request("GET", "/fapi/v1/exchangeInfo", signed=False, retries=1)
        client._request("GET", "/fapi/v2/account", signed=True, retries=1)
        client._request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.001}, signed=True, retries=1)
        client._request("POST", "/papi/v1/um/conditional/order", {"symbol": "BTCUSDT", "strategyType": "STOP_MARKET", "stopPrice": 1}, signed=True, retries=1)
        client._request("POST", "/papi/v1/um/leverage", {"symbol": "BTCUSDT", "leverage": 3}, signed=True, retries=1)

        self.assertEqual(len(direct_calls), 2)
        self.assertEqual(len(auth_calls), 3)

    def test_unified_mode_uses_papi_paths(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}, clear=False):
                client = BinanceFuturesClient("k", "s")
        self.assertTrue(client.use_unified_account)
        self.assertEqual(client._endpoint("/fapi/v1/order", "/papi/v1/um/order"), "/papi/v1/um/order")
        self.assertEqual(client._api_base(), "https://papi.binance.com")

    def test_unified_exchange_info_still_uses_fapi_base(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}, clear=False):
                client = BinanceFuturesClient("k", "s")

        requested_urls = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"symbols": []}'

        def _open(req, timeout=0):
            requested_urls.append(req.full_url)
            return _FakeResp()

        binance_client._PRECISION_CACHE = {"ts": 0.0, "data": {}}
        client._direct_opener = SimpleNamespace(open=_open)

        client.get_exchange_info()

        self.assertEqual(requested_urls, ["https://fapi.binance.com/fapi/v1/exchangeInfo"])

    def test_unified_mode_skips_margin_type_call(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}, clear=False):
                client = BinanceFuturesClient("k", "s")
        resp = client.set_margin_type("BTCUSDT", "CROSSED")
        self.assertIn("跳过", resp.get("msg", ""))

    def test_hedge_mode_market_buy_sends_long_position_side(self):
        env = {"BINANCE_POSITION_MODE": "hedge"}
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")

        with patch.object(client, "round_quantity", return_value=0.123):
            with patch.object(client, "place_order", return_value={"orderId": 1}) as place_order:
                client.market_buy("HYPEUSDT", 0.123)

        params = place_order.call_args.kwargs
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["positionSide"], "LONG")

    def test_hedge_mode_closing_orders_use_long_position_side_without_reduce_only(self):
        env = {
            "BINANCE_DERIVATIVES_ACCOUNT_MODE": "futures",
            "BINANCE_POSITION_MODE": "hedge",
        }
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")

        with patch.object(client, "round_quantity", return_value=0.123):
            with patch.object(client, "round_price", return_value=38.5):
                with patch.object(client, "place_order", return_value={"orderId": 1}) as place_order:
                    client.market_sell("HYPEUSDT", 0.123)
                    client.stop_market_sell("HYPEUSDT", 0.123, 38.5)
                    client.take_profit_market_sell("HYPEUSDT", 0.123, 42.0)

        for call in place_order.call_args_list:
            params = call.kwargs
            self.assertEqual(params["side"], "SELL")
            self.assertEqual(params["positionSide"], "LONG")
            self.assertNotIn("reduceOnly", params)

    def test_unified_stop_and_take_profit_use_conditional_order_endpoint(self):
        env = {
            "BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified",
            "BINANCE_POSITION_MODE": "hedge",
        }
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")

        calls = []

        def _request(method, path, params, signed=True, retries=3, base_url=None):
            calls.append((method, path, params))
            return {"strategyId": 123}

        client._request = _request

        with patch.object(client, "round_quantity", return_value=0.123):
            with patch.object(client, "round_price", return_value=38.5):
                client.stop_market_sell("HYPEUSDT", 0.123, 38.5)
                client.take_profit_market_sell("HYPEUSDT", 0.123, 42.0)

        self.assertEqual(calls[0][1], "/papi/v1/um/conditional/order")
        self.assertEqual(calls[0][2]["strategyType"], "STOP_MARKET")
        self.assertNotIn("type", calls[0][2])
        self.assertEqual(calls[0][2]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", calls[0][2])
        self.assertEqual(calls[1][1], "/papi/v1/um/conditional/order")
        self.assertEqual(calls[1][2]["strategyType"], "TAKE_PROFIT_MARKET")
        self.assertNotIn("type", calls[1][2])

    def test_unified_cancel_conditional_order_uses_strategy_id_endpoint(self):
        env = {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, env, clear=False):
                client = BinanceFuturesClient("k", "s")

        calls = []

        def _request(method, path, params, signed=True, retries=3, base_url=None):
            calls.append((method, path, params))
            return {"strategyStatus": "CANCELED"}

        client._request = _request

        resp = client.cancel_conditional_order("HYPEUSDT", 12345)

        self.assertEqual(resp["strategyStatus"], "CANCELED")
        self.assertEqual(calls, [(
            "DELETE",
            "/papi/v1/um/conditional/order",
            {"symbol": "HYPEUSDT", "strategyId": 12345},
        )])

    def test_unified_balance_prefers_um_wallet_balance(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}, clear=False):
                client = BinanceFuturesClient("k", "s")
        with patch.object(client, "_request", return_value=[{
            "asset": "USDT",
            "umWalletBalance": "88.8",
            "crossMarginFree": "200.0",
            "totalWalletBalance": "210.0",
            "umUnrealizedPNL": "1.2",
            "crossUnPnl": "9.9",
        }]):
            bal = client.get_balance()
        self.assertEqual(bal["balance"], 88.8)
        self.assertEqual(bal["available"], 88.8)
        self.assertEqual(bal["unrealized_pnl"], 1.2)

    def test_unified_balance_falls_back_to_cross_margin_when_um_zero(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_DERIVATIVES_ACCOUNT_MODE": "unified"}, clear=False):
                client = BinanceFuturesClient("k", "s")
        with patch.object(client, "_request", return_value=[{
            "asset": "USDT",
            "umWalletBalance": "0",
            "crossMarginFree": "200.8455761",
            "totalWalletBalance": "200.8455761",
            "umUnrealizedPNL": "0",
            "crossUnPnl": "0",
        }]):
            bal = client.get_balance()
        self.assertEqual(bal["balance"], 200.8455761)
        self.assertEqual(bal["available"], 200.8455761)
        self.assertEqual(bal["unrealized_pnl"], 0.0)

    def test_1021_retry_refreshes_timestamp_for_signed_get(self):
        with patch.object(BinanceFuturesClient, "_sync_server_time_offset", return_value=None):
            with patch.dict(os.environ, {"BINANCE_USE_PROXY": "false"}, clear=False):
                client = BinanceFuturesClient("k", "s")

        calls = {"count": 0}
        requested_urls = []

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def _auth_open(req, timeout=0):
            requested_urls.append(req.full_url)
            calls["count"] += 1
            if calls["count"] == 1:
                err_body = io.BytesIO(json.dumps({"code": -1021, "msg": "timestamp outside recvWindow"}).encode())
                raise HTTPError(req.full_url, 400, "Bad Request", hdrs=None, fp=err_body)
            return _FakeResp()

        client._auth_opener = SimpleNamespace(open=lambda req, timeout=0: _FakeResp())
        client._direct_opener = SimpleNamespace(open=_auth_open)

        with patch.object(client, "_sync_server_time_offset", return_value=None):
            with patch("time.time", side_effect=[1000.0, 1001.0]):
                client._request("GET", "/fapi/v2/account", signed=True, retries=2)

        self.assertEqual(len(requested_urls), 2)
        self.assertNotEqual(requested_urls[0], requested_urls[1])

    def test_trade_permission_check_treats_html_404_as_failure(self):
        class _FakeClient:
            use_unified_account = True

            def _endpoint(self, standard_path, unified_path):
                return unified_path

            def round_quantity(self, symbol, raw_qty):
                return raw_qty

            def _request(self, method, path, params, signed=True, retries=1):
                raise BinanceAPIError(404, "<!DOCTYPE html>")

        ok, msg = _check_trade_permission(_FakeClient(), "BTCUSDT", 0.001)

        self.assertFalse(ok)
        self.assertIn("404", msg)

    def test_unified_trade_permission_probe_uses_real_order_endpoint_with_invalid_symbol(self):
        class _FakeClient:
            use_unified_account = True

            def _request(self, method, path, params, signed=True, retries=1):
                self.call = (method, path, params, signed, retries)
                raise BinanceAPIError(-1121, "Invalid symbol.")

        client = _FakeClient()

        ok, msg = _check_trade_permission(client, "BTCUSDT", 0.001)

        self.assertTrue(ok)
        method, path, params, signed, retries = client.call
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/papi/v1/um/order")
        self.assertEqual(params["symbol"], "NOTAREALUSDT")
        self.assertTrue(signed)
        self.assertEqual(retries, 1)
        self.assertIn("下单参数校验", msg)


if __name__ == "__main__":
    unittest.main()
