import unittest
from unittest.mock import patch

from binance_client import BinanceAPIError
from executor import BinanceLiveExecutor


class _FakeClient:
    def __init__(self, stop_error):
        self.stop_error = stop_error
        self.market_sell_calls = 0

    def set_margin_type(self, symbol, margin_type):
        return {"ok": True}

    def set_leverage(self, symbol, leverage):
        return {"ok": True}

    def market_buy(self, symbol, quantity):
        return {
            "orderId": 1001,
            "avgPrice": "10",
            "executedQty": str(quantity),
            "status": "FILLED",
        }

    def stop_market_sell(self, symbol, quantity, stop_price):
        raise self.stop_error

    def market_sell(self, symbol, quantity):
        self.market_sell_calls += 1
        return {"orderId": 1002}


class BinanceLiveExecutorStopFailureTests(unittest.TestCase):
    def test_transient_stop_failure_keeps_position_for_repair_without_emergency_close(self):
        client = _FakeClient(TimeoutError("timed out"))
        executor = BinanceLiveExecutor(client)

        with patch("executor.config.LIVE_STOP_ORDER_RETRY_COUNT", 1, create=True), \
             patch("executor.config.LIVE_EMERGENCY_CLOSE_ON_STOP_TRANSIENT_FAILURE", False, create=True):
            result = executor.open_long(
                symbol="HYPEUSDT",
                quantity=1.0,
                entry_price=10,
                stop_loss_price=9,
                tp1_price=12,
                tp1_qty=0,
                leverage=8,
            )

        self.assertTrue(result.success)
        self.assertEqual(client.market_sell_calls, 0)
        self.assertTrue(result.extra.get("stop_order_pending"))
        self.assertIn("timed out", result.extra.get("stop_error", ""))

    def test_hard_stop_failure_keeps_position_for_manual_review(self):
        client = _FakeClient(BinanceAPIError(-1116, "Invalid orderType."))
        executor = BinanceLiveExecutor(client)

        with patch("executor.config.LIVE_STOP_ORDER_RETRY_COUNT", 1, create=True):
            result = executor.open_long(
                symbol="HYPEUSDT",
                quantity=1.0,
                entry_price=10,
                stop_loss_price=9,
                tp1_price=12,
                tp1_qty=0,
                leverage=8,
            )

        self.assertTrue(result.success)
        self.assertEqual(client.market_sell_calls, 0)
        self.assertEqual(result.status, "STOP_PENDING")
        self.assertTrue(result.extra.get("stop_order_pending"))
        self.assertIn("Invalid orderType", result.extra.get("stop_error", ""))


class _FakeClientCaptureStop:
    def __init__(self, fill_price=110.0):
        self.fill_price = fill_price
        self.last_stop_price = None

    def set_margin_type(self, symbol, margin_type):
        return {"ok": True}

    def set_leverage(self, symbol, leverage):
        return {"ok": True}

    def market_buy(self, symbol, quantity):
        return {
            "orderId": 2001,
            "avgPrice": str(self.fill_price),
            "executedQty": str(quantity),
            "status": "FILLED",
        }

    def stop_market_sell(self, symbol, quantity, stop_price):
        self.last_stop_price = float(stop_price)
        return {"orderId": 2002, "status": "NEW"}

    def round_quantity(self, symbol, quantity):
        return quantity

    def take_profit_market_sell(self, symbol, quantity, stop_price):
        return {"orderId": 2003, "status": "NEW"}


class BinanceLiveExecutorStopPriceTests(unittest.TestCase):
    def test_stop_is_reanchored_to_fill_and_capped_by_open_max_loss(self):
        client = _FakeClientCaptureStop(fill_price=110.0)
        executor = BinanceLiveExecutor(client)

        with patch("executor.get_mark_price", return_value=100.0), \
             patch("executor.config.TRADING_OPEN_MAX_LOSS_PCT", 5.0, create=True):
            result = executor.open_long(
                symbol="BTCUSDT",
                quantity=1.0,
                entry_price=100.0,
                stop_loss_price=95.0,  # 期望 -5%
                tp1_price=120.0,
                tp1_qty=0.0,
                leverage=5,
            )

        self.assertTrue(result.success)
        self.assertIsNotNone(client.last_stop_price)
        # 若不修复，这里会因 mark=100 下移到 99.8（约 -9.27%）。
        # 修复后应受 5% 上限保护：110 * 0.95 = 104.5。
        self.assertAlmostEqual(client.last_stop_price, 104.5, places=6)
        self.assertAlmostEqual(result.extra.get("placed_stop_price"), 104.5, places=6)


if __name__ == "__main__":
    unittest.main()
