"""v2.4 新增：taker 趋势衰退硬否决 + _taker_metrics 趋势计算"""
import sys
sys.path.insert(0, "/home/claude/work")

import risk
import config
from market import _taker_metrics


def _base_snap():
    return {
        "change_15m_pct": 1.0, "change_1h_pct": 5.0,
        "change_4h_pct": 10, "change_24h_pct": 20,
        "oi_change_15m_pct": 2, "oi_change_1h_pct": 3, "oi_change_4h_pct": 5,
        "taker_buy_sell_ratio": 1.3,
        "funding_rate_pct": 0.01,
        "long_short_ratio": 1.0,
        "top_trader_ls_ratio": 1.0,
        "taker_trend_pct": 5.0,  # 默认：买盘温和增强
    }


# ===================== _taker_metrics 单元测试 =====================

def test_taker_metrics_empty():
    m = _taker_metrics([])
    assert m["taker_buy_sell_ratio"] is None
    assert m["taker_trend_pct"] is None
    print("OK _taker_metrics 空输入")


def test_taker_metrics_single_row():
    """单根数据无法算趋势"""
    m = _taker_metrics([{"buyVol": 100, "sellVol": 50}])
    assert m["taker_buy_sell_ratio"] == 2.0
    assert m["taker_trend_pct"] is None, f"单根应无趋势，实际 {m['taker_trend_pct']}"
    print("OK _taker_metrics 单根无趋势")


def test_taker_metrics_trend_up():
    """买盘从弱到强 → trend_pct > 0"""
    rows = [
        {"buyVol": 50, "sellVol": 100},   # ratio = 0.5
        {"buyVol": 70, "sellVol": 100},   # ratio = 0.7
        {"buyVol": 100, "sellVol": 100},  # ratio = 1.0
        {"buyVol": 150, "sellVol": 100},  # ratio = 1.5 (最新)
    ]
    m = _taker_metrics(rows)
    assert m["taker_ratio_recent"] == 1.5
    # older = 前 3 根平均 buy/平均 sell = (50+70+100)/3 / 100 = 73.33/100 = 0.733
    assert abs(m["taker_ratio_older"] - 0.7333) < 0.01
    # trend = (1.5 - 0.733) / 0.733 * 100 ≈ +104%
    assert m["taker_trend_pct"] > 50
    print(f"OK 买盘增强 trend_pct={m['taker_trend_pct']:+.1f}%")


def test_taker_metrics_trend_down():
    """买盘从强到弱 → trend_pct < 0"""
    rows = [
        {"buyVol": 200, "sellVol": 100},  # ratio = 2.0
        {"buyVol": 180, "sellVol": 100},  # ratio = 1.8
        {"buyVol": 160, "sellVol": 100},  # ratio = 1.6
        {"buyVol": 120, "sellVol": 100},  # ratio = 1.2 (最新，明显衰退)
    ]
    m = _taker_metrics(rows)
    assert m["taker_ratio_recent"] == 1.2
    # older = (200+180+160)/3 / 100 = 1.8
    assert abs(m["taker_ratio_older"] - 1.8) < 0.01
    # trend = (1.2 - 1.8) / 1.8 * 100 = -33.3%
    assert m["taker_trend_pct"] < -20
    print(f"OK 买盘衰退 trend_pct={m['taker_trend_pct']:+.1f}%")


def test_taker_metrics_zero_sell():
    """极端：某些根 sell=0，不应崩溃"""
    rows = [
        {"buyVol": 100, "sellVol": 0},
        {"buyVol": 100, "sellVol": 50},
    ]
    m = _taker_metrics(rows)
    # 至少总体 ratio 能算（sell_total=50 > 0）
    assert m["taker_buy_sell_ratio"] == 4.0
    # 不崩就行
    print("OK _taker_metrics 0 值兼容")


# ===================== evaluate_entry_quality 入场否决测试 =====================

def test_taker_decay_hard_block():
    """taker 趋势衰退超 10% → 硬否决"""
    snap = _base_snap()
    snap["taker_trend_pct"] = -15.0  # 衰退 15%
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "skip", f"应 skip，实际 {result['tier']}"
    assert any("taker" in x and "衰退" in x for x in result["hard_block"]), \
        f"应有衰退拒绝原因，实际 {result['hard_block']}"
    print(f"OK taker 衰退 -15% 硬否决: {result['hard_block'][0][:60]}")


def test_taker_decay_at_boundary():
    """taker 趋势恰好 -10%（阈值边界）→ 拒绝"""
    snap = _base_snap()
    snap["taker_trend_pct"] = -10.0
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    # <= 阈值就拒
    assert result["tier"] == "skip"
    print("OK 边界 -10% 正好拒绝")


def test_taker_mild_decay_ok():
    """-3% 轻微衰退仍在容忍范围（v2.5 阈值收紧到 -5% 后，-3% 仍应放行）"""
    snap = _base_snap()
    snap["taker_trend_pct"] = -3.0
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] != "skip" or not any("衰退" in x for x in result["hard_block"]), \
        "-3% 不应被 taker_trend 硬否决"
    print(f"OK 轻微衰退 -3% 放行 (tier={result['tier']})")


def test_taker_rising_ok():
    """买盘增强 +8% → 不应触发硬否决"""
    snap = _base_snap()
    snap["taker_trend_pct"] = 8.0
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert not any("taker" in x and "衰退" in x for x in result["hard_block"])
    print("OK 买盘增强通过")


def test_taker_trend_missing_not_blocking():
    """没有趋势数据时不应拦截（向后兼容）"""
    snap = _base_snap()
    snap["taker_trend_pct"] = None  # 没数据
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert not any("taker" in x and "衰退" in x for x in result["hard_block"])
    print("OK 无趋势数据时不拦截")


if __name__ == "__main__":
    tests = [
        test_taker_metrics_empty,
        test_taker_metrics_single_row,
        test_taker_metrics_trend_up,
        test_taker_metrics_trend_down,
        test_taker_metrics_zero_sell,
        test_taker_decay_hard_block,
        test_taker_decay_at_boundary,
        test_taker_mild_decay_ok,
        test_taker_rising_ok,
        test_taker_trend_missing_not_blocking,
    ]
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
