"""简单测试 risk.py 的核心计算逻辑，无外部依赖"""
import sys
sys.path.insert(0, "/home/claude/work")

import risk
import config


def test_atr():
    # 构造一组 K 线，每根 TR 都是 1，当前价 100 -> ATR% = 1%
    klines = []
    for i in range(20):
        klines.append({"high": 100.5, "low": 99.5, "close": 100})
    atr_pct = risk.compute_atr_pct(klines, period=14)
    assert atr_pct is not None, "ATR 应该能算出"
    assert 0.9 < atr_pct < 1.1, f"ATR% 应 ≈ 1.0，实际 {atr_pct}"
    print(f"OK ATR 计算: {atr_pct:.3f}%")


def test_atr_insufficient():
    atr = risk.compute_atr_pct([{"high": 1, "low": 1, "close": 1}] * 3, period=14)
    assert atr is None
    print("OK K 线不足返回 None")


def test_stop_distance_atr_mode():
    config.TRADING_STOP_MODE = "atr"
    klines = [{"high": 102, "low": 98, "close": 100}] * 20  # TR=4, 价=100, ATR%=4
    stop_pct, mode = risk.compute_stop_distance_pct(klines)
    # 4% × 1.5 = 6%，被 TRADING_STOP_LOSS_MAX_PCT (-5%) 夹紧
    assert abs(stop_pct - config.TRADING_STOP_LOSS_MAX_PCT) < 0.01, \
        f"应被 MAX clamp 到 {config.TRADING_STOP_LOSS_MAX_PCT}，实际 {stop_pct}"
    assert "clamped" in mode
    print(f"OK 止损 clamp: {stop_pct}% mode={mode}")


def test_stop_distance_fallback():
    config.TRADING_STOP_MODE = "atr"
    stop_pct, mode = risk.compute_stop_distance_pct(None)
    assert stop_pct == config.TRADING_STOP_LOSS_PCT
    assert mode == "atr_fallback_fixed"
    print(f"OK K线缺失回退: {stop_pct}% mode={mode}")


def test_position_size_risk_based():
    config.TRADING_SIZING_MODE = "risk_based"
    config.TRADING_RISK_PER_TRADE_PCT = 1.0  # 每笔 1%
    account = risk.AccountContext(equity=1000, available_balance=1000)
    # entry=100, stop=98 -> per_unit_risk=2
    # risk_amount = 1000 × 1% = 10
    # quantity = 10 / 2 = 5 coins
    # notional = 5 × 100 = 500, margin = 500 / 2 lev = 250
    result = risk.compute_position_size(account, 100, 98, 2, tier="full")
    assert abs(result["quantity"] - 5) < 0.01, f"quantity 应 ≈ 5，实际 {result['quantity']}"
    assert abs(result["risk_amount"] - 10) < 0.01
    assert abs(result["margin"] - 250) < 0.01
    print(f"OK 风险反推仓位: qty={result['quantity']} margin={result['margin']}")


def test_position_size_half_tier():
    config.TRADING_SIZING_MODE = "risk_based"
    config.TRADING_RISK_PER_TRADE_PCT = 1.0
    account = risk.AccountContext(equity=1000, available_balance=1000)
    full = risk.compute_position_size(account, 100, 98, 2, tier="full")
    half = risk.compute_position_size(account, 100, 98, 2, tier="half")
    assert abs(half["quantity"] - full["quantity"] / 2) < 0.01, \
        f"half 应是 full 的一半 ({full['quantity']/2})，实际 {half['quantity']}"
    print(f"OK half tier = full/2: full={full['quantity']} half={half['quantity']}")


def test_position_size_capped_by_notional():
    config.TRADING_SIZING_MODE = "risk_based"
    config.TRADING_RISK_PER_TRADE_PCT = 1.0
    config.TRADING_MAX_NOTIONAL_PCT = 50.0
    # equity=1000，max_notional = 500
    # 如果 stop 只有 0.1% 距离，原始 quantity 会巨大
    # entry=100, stop=99.9，per_unit=0.1, risk=10, qty=100, notional=10000 -> 应被压到 500
    account = risk.AccountContext(equity=1000, available_balance=1000)
    result = risk.compute_position_size(account, 100, 99.9, 2)
    assert result["notional"] <= 500.01, f"名义价值应被压到 500 以内，实际 {result['notional']}"
    assert "压缩" in result["note"]
    print(f"OK 名义价值上限: notional={result['notional']:.2f}")


def test_position_size_rejected_low_balance():
    config.TRADING_SIZING_MODE = "risk_based"
    account = risk.AccountContext(equity=1000, available_balance=10)  # 只有 10 可用
    result = risk.compute_position_size(account, 100, 98, 2)
    assert result["quantity"] == 0
    assert "余额" in result["note"]
    print(f"OK 余额不足拒绝: {result['note']}")


def test_risk_check_daily_loss_circuit_breaker():
    config.TRADING_MAX_DAILY_LOSS_PCT = 5.0
    account = risk.AccountContext(
        equity=1000, available_balance=1000,
        realized_pnl_today=-40, unrealized_pnl=-20,  # 总亏损 60 = 6%
    )
    decision = risk.check_account_risk(account, "BTC")
    assert not decision.allowed
    assert "日亏损熔断" in decision.reason
    print(f"OK 日亏损熔断: {decision.reason}")


def test_risk_check_max_positions():
    config.TRADING_MAX_CONCURRENT_POSITIONS = 3
    account = risk.AccountContext(
        equity=1000, available_balance=1000, open_positions_count=3,
    )
    decision = risk.check_account_risk(account, "BTC")
    assert not decision.allowed
    assert "持仓数" in decision.reason
    print(f"OK 持仓上限: {decision.reason}")


def test_risk_check_sector_concentration():
    config.TRADING_CORRELATED_LIMIT = 2
    account = risk.AccountContext(
        equity=1000, available_balance=1000, open_positions_count=2,
        open_positions_by_sector={"meme": 2},
    )
    decision = risk.check_account_risk(account, "PEPE")  # PEPE 是 meme
    assert not decision.allowed
    assert "板块" in decision.reason and "meme" in decision.reason
    print(f"OK 板块集中度: {decision.reason}")


def test_risk_check_cooldown():
    from datetime import datetime, timezone, timedelta
    config.TRADING_COOLDOWN_MINUTES_AFTER_LOSS = 30
    now = datetime.now(timezone.utc)
    recent_stop = (now - timedelta(minutes=10)).isoformat()
    account = risk.AccountContext(
        equity=1000, available_balance=1000,
        last_stop_loss_by_token={"PEPE": recent_stop},
    )
    decision = risk.check_account_risk(account, "PEPE", now=now)
    assert not decision.allowed
    assert "冷却" in decision.reason
    print(f"OK 止损冷却: {decision.reason}")


def test_entry_quality_tiered_full():
    config.TRADING_ENTRY_MODE = "tiered"
    config.TRADING_SIGNAL_FULL_THRESHOLD = 65
    snap = {
        "change_15m_pct": 2.0, "change_1h_pct": 5.0,
        "change_4h_pct": 10, "change_24h_pct": 20,
        "oi_change_15m_pct": 2, "oi_change_1h_pct": 3, "oi_change_4h_pct": 5,
        "taker_buy_sell_ratio": 1.3,
    }
    result = risk.evaluate_entry_quality(snap, {}, signal_score=70,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "full", f"应 full，实际 {result['tier']}"
    assert result["pass_count"] == 7
    print(f"OK 全通过 -> full: pass={result['pass_count']}/7")


def test_entry_quality_tiered_half():
    config.TRADING_ENTRY_MODE = "tiered"
    config.TRADING_CORE_REQUIRED_PASS_COUNT = 5
    config.TRADING_SIGNAL_HALF_THRESHOLD = 55
    snap = {
        "change_15m_pct": 2.0, "change_1h_pct": 5.0,
        "change_4h_pct": 10, "change_24h_pct": 20,
        "oi_change_15m_pct": 2, "oi_change_1h_pct": -1,  # 1 项不过
        "oi_change_4h_pct": 5,
        "taker_buy_sell_ratio": 1.0,  # 又 1 项不过（<1.15）
    }
    result = risk.evaluate_entry_quality(snap, {}, signal_score=60,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "half", f"5/7 + 60分 应 half，实际 {result['tier']}"
    print(f"OK 5/7通过 + 60分 -> half")


def test_entry_quality_hard_block_追高():
    config.TRADING_MAX_CHANGE_4H_PCT = 25.0
    snap = {
        "change_15m_pct": 1, "change_1h_pct": 5,
        "change_4h_pct": 30,  # 追高！
        "oi_change_15m_pct": 2, "oi_change_1h_pct": 3, "oi_change_4h_pct": 5,
        "taker_buy_sell_ratio": 1.3,
    }
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "skip", f"4h 追高应 skip，实际 {result['tier']}"
    assert any("4h" in x for x in result["hard_block"])
    print(f"OK 追高硬否决: {result['hard_block'][0]}")


def test_entry_quality_overheated_block():
    snap = {"change_15m_pct": 10}  # 不重要，verdict 过热直接拒
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="⚠️ 过热预警")
    assert result["tier"] == "skip"
    assert any("过热" in x for x in result["hard_block"])
    print("OK 过热硬否决")


def test_sector_mapping():
    assert risk.sector_of("PEPE") == "meme"
    assert risk.sector_of("ARB") == "l2"
    assert risk.sector_of("UNKNOWN999") == "other"
    print("OK 板块映射")


if __name__ == "__main__":
    tests = [
        test_atr, test_atr_insufficient,
        test_stop_distance_atr_mode, test_stop_distance_fallback,
        test_position_size_risk_based, test_position_size_half_tier,
        test_position_size_capped_by_notional, test_position_size_rejected_low_balance,
        test_risk_check_daily_loss_circuit_breaker, test_risk_check_max_positions,
        test_risk_check_sector_concentration, test_risk_check_cooldown,
        test_entry_quality_tiered_full, test_entry_quality_tiered_half,
        test_entry_quality_hard_block_追高, test_entry_quality_overheated_block,
        test_sector_mapping,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"{len(tests) - failed}/{len(tests)} 测试通过")
    if failed:
        sys.exit(1)
