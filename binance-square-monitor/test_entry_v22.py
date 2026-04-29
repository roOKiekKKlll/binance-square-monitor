"""v2.2 新增入场逻辑测试：funding/lsr/taker 硬门槛 + 15m 回调 + 聪明钱分歧"""
import sys
sys.path.insert(0, "/home/claude/work")

import risk
import config


def _base_snap():
    """一个会通过所有条件的基础快照"""
    return {
        "change_15m_pct": 1.0, "change_1h_pct": 5.0,
        "change_4h_pct": 10, "change_24h_pct": 20,
        "oi_change_15m_pct": 2, "oi_change_1h_pct": 3, "oi_change_4h_pct": 5,
        "taker_buy_sell_ratio": 1.3,
        "funding_rate_pct": 0.01,   # 正常
        "long_short_ratio": 1.0,    # 正常
        "top_trader_ls_ratio": 1.0, # 正常
    }


def test_funding_hard_block():
    snap = _base_snap()
    snap["funding_rate_pct"] = 0.08  # 高于 0.05 上限
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "skip"
    assert any("资金费率" in x for x in result["hard_block"])
    print(f"OK funding 过热硬否决: {result['hard_block'][0]}")


def test_lsr_hard_block():
    snap = _base_snap()
    snap["long_short_ratio"] = 2.5  # 高于 2.0
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "skip"
    assert any("散户多空比" in x for x in result["hard_block"])
    print(f"OK LSR 过热硬否决: {result['hard_block'][0]}")


def test_taker_hard_block():
    """taker 太高说明买盘透支，很快消退"""
    snap = _base_snap()
    snap["taker_buy_sell_ratio"] = 2.0  # 高于 1.8
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] == "skip"
    assert any("taker" in x.lower() or "买盘" in x for x in result["hard_block"])
    print(f"OK taker 透支硬否决")


def test_15m_pullback_allowed():
    """15m 小幅回调（-1% 左右）应该被接受，不是拒绝"""
    snap = _base_snap()
    snap["change_15m_pct"] = -1.0  # -1% 回调
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    # 不应该因为 15m 是负数就被拒
    c2_passed = any("15m" in r and "在" in r for r in result["reasons_pass"])
    assert c2_passed, f"15m -1% 应通过，但失败列表={result['reasons_fail']}"
    print("OK 15m 小回调允许入场（买回调）")


def test_15m_急拉拒绝():
    """15m 涨幅 > 2% 应被拒绝（避免追急拉顶部）"""
    snap = _base_snap()
    snap["change_15m_pct"] = 3.0  # 急拉 3%
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    c2_failed = any("15m" in r for r in result["reasons_fail"])
    assert c2_failed, f"15m 3% 应失败，实际 reasons_pass={result['reasons_pass']}"
    print("OK 15m 急拉被拒绝")


def test_smart_money_divergence_upgrades_tier():
    """聪明钱分歧：half → full"""
    snap = _base_snap()
    # 让通过数 = 7
    snap["top_trader_ls_ratio"] = 1.8
    snap["long_short_ratio"] = 0.5
    # signal_score 60，本来是 half
    result = risk.evaluate_entry_quality(snap, {}, signal_score=60,
                                          analysis_verdict="✅ 看起来健康")
    assert result["smart_money_bonus"] is True
    # 原本 7/7 + score=60 < 65 → half，聪明钱升档到 full
    assert result["tier"] == "full", f"应 full，实际 {result['tier']}"
    print(f"OK 聪明钱分歧升档 half→full: tier={result['tier']}")


def test_smart_money_no_divergence_keeps_tier():
    """无分歧时不升档"""
    snap = _base_snap()
    result = risk.evaluate_entry_quality(snap, {}, signal_score=60,
                                          analysis_verdict="✅ 看起来健康")
    assert result["smart_money_bonus"] is False
    assert result["tier"] == "half"  # 保持 half
    print("OK 无分歧时保持 half")


def test_funding_normal_passes():
    """正常 funding 不会硬否决"""
    snap = _base_snap()
    snap["funding_rate_pct"] = 0.02  # 正常偏高
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    assert result["tier"] in ("full", "half"), f"应通过，实际 skip: {result['hard_block']}"
    print(f"OK 正常 funding 通过，tier={result['tier']}")


def test_taker_too_low_also_rejected():
    """taker < 下限（买盘不足）仍然被拒"""
    snap = _base_snap()
    snap["taker_buy_sell_ratio"] = 1.0  # 低于 1.15 下限
    result = risk.evaluate_entry_quality(snap, {}, signal_score=80,
                                          analysis_verdict="✅ 看起来健康")
    c7_failed = any("taker" in r.lower() for r in result["reasons_fail"])
    assert c7_failed
    print("OK taker 不足被拒")


if __name__ == "__main__":
    tests = [
        test_funding_hard_block, test_lsr_hard_block, test_taker_hard_block,
        test_15m_pullback_allowed, test_15m_急拉拒绝,
        test_smart_money_divergence_upgrades_tier,
        test_smart_money_no_divergence_keeps_tier,
        test_funding_normal_passes, test_taker_too_low_also_rejected,
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
