import unittest
from unittest.mock import patch

import live_manager


class _FakeClient:
    def __init__(self, open_orders, position_map):
        self._position_map = position_map
        self.canceled_symbols = []

    def get_position_risk(self, symbol=""):
        return self._position_map.get(symbol.upper(), [])

    def cancel_all_orders(self, symbol):
        self.canceled_symbols.append(symbol.upper())


class _FakeExecutor:
    def __init__(self, open_orders, position_map):
        self.client = _FakeClient(open_orders, position_map)
        self._orders_by_symbol = {}
        for row in open_orders:
            symbol = str(row.get("symbol") or "").upper()
            self._orders_by_symbol.setdefault(symbol, []).append(dict(row))
        self.cancel_safe_calls = []

    def get_open_orders(self, symbol):
        sym = str(symbol or "").upper()
        if not sym:
            merged = []
            for rows in self._orders_by_symbol.values():
                merged.extend(dict(r) for r in rows)
            return merged
        return [dict(r) for r in self._orders_by_symbol.get(sym, [])]

    def cancel_order_safe(self, symbol, order_id):
        sym = str(symbol or "").upper()
        oid = str(order_id)
        self.cancel_safe_calls.append((sym, oid))
        rows = self._orders_by_symbol.get(sym, [])
        self._orders_by_symbol[sym] = [r for r in rows if str(r.get("orderId")) != oid]
        return True


class LiveManagerOrphanOrderCleanupTests(unittest.TestCase):
    def test_cleanup_cancels_only_symbols_without_db_or_exchange_position(self):
        open_orders = [
            {"symbol": "BTCUSDT", "orderId": 1},
            {"symbol": "ETHUSDT", "orderId": 2},
        ]
        # BTC 无持仓；ETH 仍有持仓（不应被撤）
        position_map = {
            "BTCUSDT": [{"symbol": "BTCUSDT", "positionAmt": "0"}],
            "ETHUSDT": [{"symbol": "ETHUSDT", "positionAmt": "0.8"}],
        }
        executor = _FakeExecutor(open_orders, position_map)

        live_manager._cleanup_orphan_orders(executor, db_positions=[])

        self.assertEqual(executor.client.canceled_symbols, ["BTCUSDT"])
        # cancel_all_orders 后对剩余单逐单兜底撤单
        self.assertEqual(executor.cancel_safe_calls, [("BTCUSDT", "1")])

    def test_cleanup_skips_symbols_with_db_live_positions(self):
        open_orders = [{"symbol": "BTCUSDT", "orderId": 1}]
        position_map = {"BTCUSDT": [{"symbol": "BTCUSDT", "positionAmt": "0"}]}
        executor = _FakeExecutor(open_orders, position_map)
        db_positions = [{"symbol": "BTCUSDT"}]

        live_manager._cleanup_orphan_orders(executor, db_positions=db_positions)

        self.assertEqual(executor.client.canceled_symbols, [])


class LiveManagerOrderStatusTests(unittest.TestCase):
    class _UnifiedClient:
        use_unified_account = True

        def __init__(self):
            self.conditional_calls = 0
            self.order_calls = 0

        def get_conditional_order_status(self, symbol, order_id):
            self.conditional_calls += 1
            return {"status": "UNKNOWN"}

        def get_order(self, symbol, order_id):
            self.order_calls += 1
            return {"status": "FILLED"}

    class _PlainClient:
        use_unified_account = False

        def get_order(self, symbol, order_id):
            return {"status": "NEW"}

    class _Exec:
        def __init__(self, client):
            self.client = client

    def test_get_order_status_uses_fallback_for_unified_unknown(self):
        client = self._UnifiedClient()
        status = live_manager._get_order_status(self._Exec(client), "trxusdt", "123")
        self.assertEqual(status, "FILLED")
        self.assertEqual(client.conditional_calls, 1)
        self.assertEqual(client.order_calls, 1)

    def test_get_order_status_keeps_non_unified_path(self):
        status = live_manager._get_order_status(self._Exec(self._PlainClient()), "trxusdt", "123")
        self.assertEqual(status, "NEW")

    def test_unknown_retry_log_is_rate_limited(self):
        logs = []
        old_log = live_manager._log
        old_cache = dict(live_manager._last_status_unknown_log_at)
        old_interval = getattr(live_manager.config, "LIVE_STATUS_QUERY_RETRY_LOG_INTERVAL_S", None)
        live_manager._last_status_unknown_log_at.clear()
        setattr(live_manager.config, "LIVE_STATUS_QUERY_RETRY_LOG_INTERVAL_S", 60)

        try:
            live_manager._log = logs.append
            live_manager._log_status_unknown_retry(1, "TP1", "TRX")
            live_manager._log_status_unknown_retry(1, "TP1", "TRX")
            self.assertEqual(logs, ["TRX TP1 状态查询失败，下一轮重试"])
        finally:
            live_manager._log = old_log
            live_manager._last_status_unknown_log_at.clear()
            live_manager._last_status_unknown_log_at.update(old_cache)
            if old_interval is None:
                delattr(live_manager.config, "LIVE_STATUS_QUERY_RETRY_LOG_INTERVAL_S")
            else:
                setattr(live_manager.config, "LIVE_STATUS_QUERY_RETRY_LOG_INTERVAL_S", old_interval)


class LiveManagerStaleTpOrderCleanupTests(unittest.TestCase):
    class _FakeConn:
        pass

    class _FakeConnCtx:
        def __enter__(self):
            return LiveManagerStaleTpOrderCleanupTests._FakeConn()

        def __exit__(self, exc_type, exc, tb):
            return False

    def setUp(self):
        self.pos = {
            "id": 99,
            "token": "TRX",
            "symbol": "TRXUSDT",
        }
        self.old_threshold = getattr(live_manager.config, "LIVE_STALE_ORDER_UNKNOWN_THRESHOLD", None)
        live_manager._stale_tp_unknown_counts.clear()
        setattr(live_manager.config, "LIVE_STALE_ORDER_UNKNOWN_THRESHOLD", 2)

    def tearDown(self):
        live_manager._stale_tp_unknown_counts.clear()
        if self.old_threshold is None:
            delattr(live_manager.config, "LIVE_STALE_ORDER_UNKNOWN_THRESHOLD")
        else:
            setattr(live_manager.config, "LIVE_STALE_ORDER_UNKNOWN_THRESHOLD", self.old_threshold)

    def test_clears_stale_tp1_order_after_threshold_unknown(self):
        with patch.object(live_manager.storage, "get_conn", return_value=self._FakeConnCtx()):
            with patch.object(live_manager.storage, "trade_position_update") as update:
                cleared_1 = live_manager._clear_stale_tp_order_id_if_needed(
                    self.pos, "tp1", "3001", "UNKNOWN", open_orders=[]
                )
                cleared_2 = live_manager._clear_stale_tp_order_id_if_needed(
                    self.pos, "tp1", "3001", "UNKNOWN", open_orders=[]
                )
        self.assertFalse(cleared_1)
        self.assertTrue(cleared_2)
        update.assert_called_once()
        args = update.call_args.args
        self.assertEqual(args[1], 99)
        self.assertEqual(args[2]["exchange_tp1_order_id"], "")

    def test_does_not_clear_when_order_still_open(self):
        with patch.object(live_manager.storage, "get_conn", return_value=self._FakeConnCtx()):
            with patch.object(live_manager.storage, "trade_position_update") as update:
                cleared = live_manager._clear_stale_tp_order_id_if_needed(
                    self.pos,
                    "tp1",
                    "3001",
                    "UNKNOWN",
                    open_orders=[{"orderId": 3001}],
                )
        self.assertFalse(cleared)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
