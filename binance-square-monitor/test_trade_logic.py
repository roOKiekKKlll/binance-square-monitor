"""
验证 trade_logic.update_paper_positions 的止盈止损逻辑。
用 in-memory SQLite 跑整个开仓 -> 价格涨到 TP1 -> TP2 -> trail 的流程。
"""
import sys, os
sys.path.insert(0, "/home/claude/work")
os.environ.setdefault("PYTHONPATH", "/home/claude/work")

import sqlite3
import json
from unittest.mock import patch

import config
import storage


def _setup_db():
    """创建一个完全内存的测试库"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 用 storage 里的完整 schema 初始化
    for statement in storage.SCHEMA.split(";"):
        s = statement.strip()
        if s:
            try:
                conn.execute(s)
            except sqlite3.OperationalError:
                pass
    conn.commit()
    return conn


def _insert_position(conn, entry=100, stop=98, tp1=102, tp2=104, qty=10):
    """塞一个已开仓的模拟 OPEN 持仓"""
    conn.execute("""
        INSERT INTO trade_positions
          (token, symbol, side, status, mode, margin_amount, leverage, notional,
           quantity, entry_price, limit_price, current_price, stop_loss_price,
           tp1_price, tp2_price, highest_price)
        VALUES
          ('TEST', 'TESTUSDT', 'LONG', 'OPEN', 'paper', 500, 2, 1000,
           ?, ?, ?, ?, ?, ?, ?, ?)
    """, (qty, entry, entry, entry, stop, tp1, tp2, entry))
    return conn.execute("SELECT id FROM trade_positions").fetchone()["id"]


def test_tp2_can_trigger():
    """
    原 bug：TP1 平 50% 后 closed_qty=5，qty*0.79=7.9，5<7.9 成立，进入 TP2 分支 ✓
    但 TP2 平 30% 后 closed_qty=8，8<7.9 不成立，直接进入 trailing 分支。

    看起来 TP2 能触发（因为 5<7.9），问题出在**哪一步会走错**：
    仔细算：TP2 那个分支要求 closed_qty > 0 AND closed_qty < qty*0.79。
    TP1 已平后 closed_qty=5.0，qty*0.79=7.9，5<7.9 ✓，所以 TP2 能触发。
    触发后 closed_qty=8.0，下一次进入函数时 closed_qty >= qty*0.79 (7.9)，
    进入 trailing 分支。看起来逻辑是"自洽的"。

    **真正的 bug 场景**：如果用户改了 TP1_CLOSE_PCT 到 80%，
    TP1 后 closed_qty=8，8 < 7.9 为假，TP2 永远不会触发。
    我们的修复版用 tp1_done 布尔判断，就不会有这个问题。
    """
    import trade_logic

    conn = _setup_db()
    # 构造一个 TP1 平 80% 的场景
    with patch.object(config, 'TRADING_TP1_CLOSE_PCT', 80.0), \
         patch.object(config, 'TRADING_TP2_CLOSE_PCT', 15.0):
        pid = _insert_position(conn, entry=100, stop=98, tp1=102, tp2=104, qty=10)

        # Step 1: 价格冲到 TP1
        with patch('trade_logic._position_price', return_value=102.5), \
             patch('trade_logic._load_market', return_value={"snapshot": {}, "analysis": {}}), \
             patch('trade_logic._load_realtime', return_value={}):
            trade_logic.update_paper_positions(conn)
        pos = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
        assert pos["closed_qty"] == 8.0, f"TP1 应平 80%=8，实际 {pos['closed_qty']}"
        assert pos["status"] == "PARTIAL"
        print(f"OK TP1 触发: closed_qty={pos['closed_qty']}, stop移到保本={pos['stop_loss_price']}")

        # Step 2: 价格冲到 TP2 —— 老代码会漏掉（8 < 7.9 为 False）
        with patch('trade_logic._position_price', return_value=104.5), \
             patch('trade_logic._load_market', return_value={"snapshot": {}, "analysis": {}}), \
             patch('trade_logic._load_realtime', return_value={}):
            trade_logic.update_paper_positions(conn)
        pos = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
        # 修复后应该继续平 15%，closed_qty=9.5
        assert abs(pos["closed_qty"] - 9.5) < 0.01, \
            f"修复后 TP2 应触发，closed_qty=9.5，实际 {pos['closed_qty']}（老 bug 会停在 8.0）"
        print(f"OK TP2 触发: closed_qty={pos['closed_qty']}")


def test_stop_loss_with_slippage():
    """验证止损触发时考虑了滑点"""
    import trade_logic

    conn = _setup_db()
    with patch.object(config, 'TRADING_STOP_SLIPPAGE_PCT', 0.5):
        pid = _insert_position(conn, entry=100, stop=98, qty=10)

        # 价格跌到 97（穿破止损 98）
        with patch('trade_logic._position_price', return_value=97.0), \
             patch('trade_logic._load_market', return_value={"snapshot": {}, "analysis": {}}), \
             patch('trade_logic._load_realtime', return_value={}):
            trade_logic.update_paper_positions(conn)

        pos = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
        assert pos["status"] == "CLOSED"
        # 成交价应 <= 97（因为滑点让成交价更差）
        assert pos["current_price"] <= 97.0, f"成交价应含滑点 <= 97，实际 {pos['current_price']}"
        expected = min(97.0, 98 * (1 - 0.5/100))  # min(97, 97.51) = 97
        assert abs(pos["current_price"] - expected) < 0.01
        print(f"OK 止损含滑点: 市价=97, 实际成交={pos['current_price']}, 盈亏={pos['realized_pnl']:.2f}")


def test_tp1_already_done_not_retriggered():
    """TP1 已经触发过后，再次调用不应重复平仓"""
    import trade_logic
    import config

    conn = _setup_db()
    pid = _insert_position(conn, entry=100, stop=98, tp1=102, tp2=104, qty=10)

    # 连续两次价格 >= TP1
    for _ in range(2):
        with patch('trade_logic._position_price', return_value=102.5), \
             patch('trade_logic._load_market', return_value={"snapshot": {}, "analysis": {}}), \
             patch('trade_logic._load_realtime', return_value={}):
            trade_logic.update_paper_positions(conn)

    pos = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    # TP1 按 config 里的比例平仓，只应触发一次
    expected = 10 * (config.TRADING_TP1_CLOSE_PCT / 100)
    assert abs(pos["closed_qty"] - expected) < 0.01, \
        f"TP1 只应触发一次，closed_qty={expected}，实际 {pos['closed_qty']}"
    print(f"OK TP1 不重复触发: closed_qty={pos['closed_qty']} (TP1_PCT={config.TRADING_TP1_CLOSE_PCT}%)")


if __name__ == "__main__":
    tests = [test_tp2_can_trigger, test_stop_loss_with_slippage, test_tp1_already_done_not_retriggered]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    if failed:
        sys.exit(1)
