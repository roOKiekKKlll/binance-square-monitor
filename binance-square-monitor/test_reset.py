"""验证 trade_reset_all：插入几条数据 → 重置 → 确认都清了 + 设置保留"""
import sys
sys.path.insert(0, "/home/claude/work")

import sqlite3
import storage


def _setup_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for s in storage.SCHEMA.split(";"):
        s = s.strip()
        if s:
            try:
                conn.execute(s)
            except sqlite3.OperationalError:
                pass
    conn.commit()
    return conn


def test_reset_clears_positions_keeps_settings():
    conn = _setup_db()

    # 先写点配置
    storage.trading_settings_update(conn, {
        "enabled": True, "mode": "paper",
        "initial_balance": 1000, "leverage": 3, "order_amount": 50,
    })

    # 塞一些交易数据
    conn.execute("""
        INSERT INTO trade_positions
          (token, symbol, side, status, mode, margin_amount, leverage, notional,
           quantity, entry_price)
        VALUES
          ('BTC', 'BTCUSDT', 'LONG', 'OPEN', 'paper', 100, 2, 200, 0.01, 20000),
          ('ETH', 'ETHUSDT', 'LONG', 'CLOSED', 'paper', 100, 2, 200, 0.1, 2000)
    """)
    conn.execute("INSERT INTO trade_signal_locks (token, signal_key) VALUES ('BTC', 'k1')")
    conn.execute("""
        INSERT INTO trade_loss_archive (token, symbol, entry_price, exit_price, realized_pnl)
        VALUES ('PEPE', 'PEPEUSDT', 0.001, 0.0008, -20)
    """)

    # 验证数据存在
    assert conn.execute("SELECT COUNT(*) FROM trade_positions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM trade_signal_locks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trade_loss_archive").fetchone()[0] == 1

    # 重置，顺便改初始金额到 2000
    result = storage.trade_reset_all(conn, new_initial_balance=2000)

    # 验证清理
    assert conn.execute("SELECT COUNT(*) FROM trade_positions").fetchone()[0] == 0, "持仓应清空"
    assert conn.execute("SELECT COUNT(*) FROM trade_signal_locks").fetchone()[0] == 0, "锁应清空"
    assert conn.execute("SELECT COUNT(*) FROM trade_loss_archive").fetchone()[0] == 0, "归档应清空"

    # 验证返回值
    assert result["positions_deleted"] == 2
    assert result["locks_deleted"] == 1
    assert result["loss_archive_deleted"] == 1

    # 验证配置保留 + 初始金额更新
    settings = storage.trading_settings_get(conn)
    assert settings["enabled"] is True, f"enabled 应保留，实际 {settings['enabled']}"
    assert settings["mode"] == "paper"
    assert settings["leverage"] == 3, f"leverage 应保留为 3，实际 {settings['leverage']}"
    assert settings["order_amount"] == 50
    assert settings["initial_balance"] == 2000, f"初始余额应更新为 2000，实际 {settings['initial_balance']}"

    print("OK 重置清空三表 + 保留配置 + 更新初始金额")


def test_reset_without_changing_balance():
    """不传 new_initial_balance 应保留原有金额"""
    conn = _setup_db()
    storage.trading_settings_update(conn, {"initial_balance": 5000})
    conn.execute("""
        INSERT INTO trade_positions (token, symbol, side, status, mode, margin_amount, leverage, notional, quantity)
        VALUES ('BTC', 'BTCUSDT', 'LONG', 'OPEN', 'paper', 100, 2, 200, 0.01)
    """)

    result = storage.trade_reset_all(conn)  # 不传 new_initial_balance
    settings = storage.trading_settings_get(conn)
    assert settings["initial_balance"] == 5000, f"应保留 5000，实际 {settings['initial_balance']}"
    assert result["positions_deleted"] == 1
    print("OK 不改初始金额时保留原值")


def test_reset_id_counter_reset():
    """重置后新插入的仓位 id 应从 1 开始"""
    conn = _setup_db()
    for i in range(3):
        conn.execute("""
            INSERT INTO trade_positions (token, symbol, side, status, mode, margin_amount, leverage, notional, quantity)
            VALUES (?, ?, 'LONG', 'OPEN', 'paper', 100, 2, 200, 0.01)
        """, (f"T{i}", f"T{i}USDT"))

    storage.trade_reset_all(conn)

    conn.execute("""
        INSERT INTO trade_positions (token, symbol, side, status, mode, margin_amount, leverage, notional, quantity)
        VALUES ('NEW', 'NEWUSDT', 'LONG', 'OPEN', 'paper', 100, 2, 200, 0.01)
    """)
    new_id = conn.execute("SELECT id FROM trade_positions WHERE token='NEW'").fetchone()["id"]
    assert new_id == 1, f"新 id 应从 1 开始，实际 {new_id}"
    print("OK 自增 id 重置")


if __name__ == "__main__":
    tests = [test_reset_clears_positions_keeps_settings, test_reset_without_changing_balance, test_reset_id_counter_reset]
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
    sys.exit(1 if failed else 0)
