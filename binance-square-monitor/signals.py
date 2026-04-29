"""
综合信号打分：把社交热度 + 行情数据组合起来，输出模式标签

核心原则：这不是交易建议，是数据模式识别。
任何"看起来有机会"的信号都可能反向走。
"""
from __future__ import annotations
import config


# 资金费率阈值（每 8 小时）
FR_EXTREME_POSITIVE = 0.001   # 0.1%，极高 = 多头过度拥挤
FR_HIGH_POSITIVE = 0.0005     # 0.05%
FR_NORMAL_HIGH = 0.0002       # 0.02%
FR_NEGATIVE = -0.0001         # 负费率 = 空头付费

# 多空比阈值
LS_EXTREME = 3.0              # >3 或 <0.33 都是极端
LS_HIGH = 2.0

# 短期动量阈值（%）
MOM_PUMP_15M = 3.0            # 15m 内涨超 3% 已经很猛
MOM_PUMP_1H = 8.0             # 1h 涨超 8%
MOM_MILD_UP_1H = 1.5          # 温和上涨

# OI 变化阈值（%，1h）
OI_SURGE = 10.0               # 1h OI 增加 >10% 是大量新仓进场
OI_DROP = -5.0                # OI 下降 >5% 是平仓
OI_FAST_SURGE = 4.0           # 15m OI 快速增加
OI_4H_SURGE = 15.0            # 4h OI 明显增加
TAKER_BUY_STRONG = 1.25
TAKER_SELL_STRONG = 0.80


def analyze(snap: dict, social_score: float) -> dict:
    """
    综合分析，返回：
      verdict:    总体标签（中性/健康/过热/新兴/出货嫌疑/数据不足）
      tags:       各维度标签列表
      score:      综合分 0-100（越高越"看起来值得关注"）
      notes:      人类可读的解读列表
    """
    tags = []
    notes = []
    score = 0.0

    fr = snap.get("funding_rate")
    fr_pct = snap.get("funding_rate_pct")
    oi_change_15m = snap.get("oi_change_15m_pct")
    oi_change = snap.get("oi_change_1h_pct")
    oi_change_4h = snap.get("oi_change_4h_pct")
    ch_15m = snap.get("change_15m_pct")
    ch_1h = snap.get("change_1h_pct")
    ch_4h = snap.get("change_4h_pct")
    ch_24h = snap.get("change_24h_pct")
    lsr = snap.get("long_short_ratio")
    top_lsr = snap.get("top_trader_ls_ratio")
    taker_ratio = snap.get("taker_buy_sell_ratio")
    spread_pct = snap.get("bid_ask_spread_pct")
    depth_bid = snap.get("depth_bid_1pct_usd")
    depth_ask = snap.get("depth_ask_1pct_usd")
    depth_imbalance = snap.get("depth_imbalance_pct")

    # 社交热度贡献基础分（最高 30）
    # social_score 来自 analyzer 的热度分，范围差异大，用对数压缩
    import math
    if social_score > 0:
        score += min(30, math.log1p(social_score) * 5)

    # === 资金费率分析 ===
    if fr is not None:
        if fr >= FR_EXTREME_POSITIVE:
            tags.append("funding:极高")
            notes.append(f"资金费率 {fr_pct:.3f}%/8h 极高，多头过度拥挤，易被反向收割")
            score -= 15
        elif fr >= FR_HIGH_POSITIVE:
            tags.append("funding:偏高")
            notes.append(f"资金费率 {fr_pct:.3f}%/8h 偏高")
            score -= 5
        elif fr >= FR_NORMAL_HIGH:
            tags.append("funding:温和正")
            score += 5
        elif fr <= FR_NEGATIVE:
            tags.append("funding:负")
            notes.append(f"资金费率 {fr_pct:.3f}%/8h 为负，空头在付费，可能有反弹基础")
            score += 10
        else:
            tags.append("funding:正常")
            score += 3

    # === 价格动量分析 ===
    if ch_15m is not None:
        if ch_15m >= MOM_PUMP_15M:
            tags.append("15m:急涨")
            notes.append(f"15m 已涨 {ch_15m:+.2f}%，短期急拉，追高风险高")
            score -= 10
        elif 0.3 <= ch_15m < MOM_PUMP_15M:
            tags.append("15m:温和涨")
            score += 5
        elif ch_15m <= -2.0:
            tags.append("15m:跌")
            score -= 5

    if ch_1h is not None:
        if ch_1h >= MOM_PUMP_1H:
            tags.append("1h:暴涨")
            notes.append(f"1h 涨 {ch_1h:+.2f}%，已经 pump，注意回撤")
            score -= 10
        elif MOM_MILD_UP_1H <= ch_1h < MOM_PUMP_1H:
            tags.append("1h:温和涨")
            score += 10
            notes.append(f"1h 温和上涨 {ch_1h:+.2f}%，节奏健康")
        elif ch_1h <= -3.0:
            tags.append("1h:跌")
            score -= 5

    # === OI 变化 ===
    if oi_change is not None:
        if oi_change >= OI_SURGE and ch_1h is not None and ch_1h > 0:
            tags.append("OI:激增+涨")
            notes.append(f"1h OI 增 {oi_change:+.1f}% + 价格涨 → 新多头进场")
            score += 15
        elif oi_change >= OI_SURGE and ch_1h is not None and ch_1h < 0:
            tags.append("OI:激增+跌")
            notes.append(f"1h OI 增 {oi_change:+.1f}% + 价格跌 → 新空头进场")
            score -= 5
        elif oi_change <= OI_DROP:
            tags.append("OI:下降")
            if ch_1h is not None and ch_1h > 2:
                notes.append(f"1h OI 减 {oi_change:+.1f}% + 价格涨 → 空头平仓推涨，动能有限")
                score -= 5
            else:
                notes.append(f"1h OI 减 {oi_change:+.1f}%，市场在退出")
                score -= 3

    # === 更细 OI 节奏：15m / 4h ===
    if oi_change_15m is not None:
        if oi_change_15m >= OI_FAST_SURGE and ch_15m is not None and ch_15m > 0:
            tags.append("OI15m:快增")
            notes.append(f"15m OI 增加 {oi_change_15m:+.1f}% 且价格走强，短线有新仓推动")
            score += 6
        elif oi_change_15m >= OI_FAST_SURGE and ch_15m is not None and ch_15m < 0:
            tags.append("OI15m:空压")
            notes.append(f"15m OI 增加 {oi_change_15m:+.1f}% 但价格走弱，可能是新空头进场")
            score -= 4

    if oi_change_4h is not None:
        if oi_change_4h >= OI_4H_SURGE and ch_4h is not None and abs(ch_4h) < 3:
            tags.append("OI4h:蓄势")
            notes.append(f"4h OI 增加 {oi_change_4h:+.1f}% 但价格未明显脱离，可能处于资金堆积阶段")
            score += 6
        elif oi_change_4h <= -8:
            tags.append("OI4h:退潮")
            notes.append(f"4h OI 下降 {oi_change_4h:+.1f}%，资金参与度在下降")
            score -= 5

    # === 主动买卖量 ===
    if taker_ratio is not None:
        if taker_ratio >= TAKER_BUY_STRONG:
            tags.append("taker:买盘强")
            notes.append(f"近 15m 主动买/卖比 {taker_ratio:.2f}，主动买盘占优")
            score += 8
        elif taker_ratio <= TAKER_SELL_STRONG:
            tags.append("taker:卖盘强")
            notes.append(f"近 15m 主动买/卖比 {taker_ratio:.2f}，主动卖盘占优")
            score -= 8

    # === 盘口流动性 ===
    if spread_pct is not None:
        if spread_pct > config.MAX_SPREAD_PCT:
            tags.append("depth:价差大")
            notes.append(f"买卖价差 {spread_pct:.3f}% 偏大，滑点风险较高")
            score -= 8
        else:
            score += 2
    if depth_bid is not None and depth_ask is not None:
        min_depth = min(depth_bid, depth_ask)
        if min_depth < config.MIN_DEPTH_1PCT_USD:
            tags.append("depth:薄")
            notes.append(f"1% 盘口单侧深度约 ${min_depth:,.0f}，流动性偏薄")
            score -= 10
        else:
            tags.append("depth:足")
            score += 5
        if depth_imbalance is not None and abs(depth_imbalance) >= 35:
            side = "买盘" if depth_imbalance > 0 else "卖盘"
            notes.append(f"1% 盘口深度向{side}倾斜 {depth_imbalance:+.0f}%")

    # === 多空比 ===
    if lsr is not None:
        if lsr >= LS_EXTREME:
            tags.append("多空:极端多")
            notes.append(f"散户多空比 {lsr:.2f}，极端看多，反向指标")
            score -= 10
        elif lsr <= 1 / LS_EXTREME:
            tags.append("多空:极端空")
            notes.append(f"散户多空比 {lsr:.2f}，极端看空，反向指标偏多")
            score += 8

    if top_lsr is not None and lsr is not None:
        # 大户和散户反向 → 经典的"聪明钱 vs 韭菜"信号
        if top_lsr > 1.5 and lsr < 0.7:
            tags.append("聪明钱:多/散户:空")
            notes.append(f"大户偏多（{top_lsr:.2f}）但散户偏空（{lsr:.2f}），值得留意")
            score += 10
        elif top_lsr < 0.7 and lsr > 1.5:
            tags.append("聪明钱:空/散户:多")
            notes.append(f"大户偏空（{top_lsr:.2f}）但散户偏多（{lsr:.2f}），注意风险")
            score -= 10

    # === 综合 verdict ===
    score = max(0, min(100, score + 50))  # 归一化到 0-100，基准 50

    # 数据不足判定
    data_points = sum(1 for v in [
        fr, oi_change, oi_change_15m, oi_change_4h,
        ch_1h, lsr, taker_ratio, spread_pct
    ] if v is not None)

    # 过热信号：funding 极高 / 15m 急涨 / 1h 暴涨 任一命中且评分偏低
    overheated = any(t in tags for t in
                     ("funding:极高", "15m:急涨", "1h:暴涨", "多空:极端多"))

    if data_points < 2:
        verdict = "数据不足"
    elif overheated and score < 45:
        verdict = "⚠️ 过热预警"
    elif score >= 65 and ("1h:温和涨" in tags or "OI:激增+涨" in tags):
        verdict = "✅ 看起来健康"
    elif score >= 55:
        verdict = "🎯 值得留意"
    elif score <= 35:
        verdict = "📉 信号偏弱"
    else:
        verdict = "⚪ 中性"

    # === 走向判断（direction）===
    # 基于当前快照的价格动量 + OI 变化。这是"当下态势"而不是预测。
    direction = _decide_direction(ch_15m, ch_1h, ch_4h, oi_change, fr)

    return {
        "verdict": verdict,
        "direction": direction,       # "偏多" / "偏空" / "震荡" / "不明"
        "tags": tags,
        "score": round(score, 1),
        "notes": notes,
        "oi_divergence": _detect_oi_divergence(snap),  # 新增：48h OI 背离
    }


def _detect_oi_divergence(snap: dict) -> dict | None:
    """
    检测"48h OI 变化大但价格基本没动"的情况
    这在量化里常被叫做"OI 在积累但价格没反应"——资金在进场但行情还没启动

    规则：
    - |48h OI 变化| >= 20%
    - |48h 价格变化| < 3%
    命中时返回 {type, direction, oi_pct, price_pct, note}
    """
    oi48 = snap.get("oi_change_48h_pct")
    px48 = snap.get("change_48h_pct")
    if oi48 is None or px48 is None:
        return None

    if abs(oi48) < 20 or abs(px48) >= 3:
        return None

    # 判断 OI 方向
    if oi48 > 0:
        direction = "积累中（多头/空头未定）"
        note = f"48h 内 OI 增加 {oi48:+.1f}% 但价格仅 {px48:+.2f}%——资金在进场但行情未启动"
    else:
        direction = "撤离中"
        note = f"48h 内 OI 减少 {oi48:+.1f}% 但价格仅 {px48:+.2f}%——资金在撤出但价格相对稳定"

    return {
        "type": "oi_accumulation" if oi48 > 0 else "oi_distribution",
        "direction": direction,
        "oi_pct": round(oi48, 1),
        "price_pct": round(px48, 2),
        "note": note,
    }


def _decide_direction(ch_15m, ch_1h, ch_4h, oi_change, fr) -> str:
    """
    走向判断（不是预测，是对"此刻态势"的描述）

    逻辑：
    - 取 15m 和 1h 的加权动量（1h 权重更高）
    - 结合 4h 方向判断一致性
    - OI 变化 + 价格方向 给一个资金面确认
    - 超过一定幅度才下判断，否则震荡
    """
    # 缺数据时不判断
    ch_1h = ch_1h if ch_1h is not None else 0
    ch_15m = ch_15m if ch_15m is not None else 0
    ch_4h = ch_4h if ch_4h is not None else 0
    oi_change = oi_change if oi_change is not None else 0

    if ch_1h == 0 and ch_15m == 0:
        return "不明"

    # 动量综合：1h 为主，15m 为辅（加速/减速的微调）
    momentum = ch_1h * 0.6 + ch_15m * 0.4

    # 多空倾向分
    bullish = 0.0
    bearish = 0.0

    # 1. 价格动量本身
    if momentum > 0.8:
        bullish += min(momentum, 5)     # 涨幅越大加分越多，上限 5
    elif momentum < -0.8:
        bearish += min(-momentum, 5)

    # 2. 4h 方向与动量是否一致（趋势确认）
    if ch_4h > 1.5 and momentum > 0:
        bullish += 1.5
    elif ch_4h < -1.5 and momentum < 0:
        bearish += 1.5
    elif ch_4h * momentum < 0:
        # 反向 —— 趋势可能在拐
        pass

    # 3. OI 变化配合：
    #   涨价 + OI 涨 = 新多头进场（强看多信号）
    #   涨价 + OI 跌 = 空头平仓推涨（信号较弱）
    #   跌价 + OI 涨 = 新空头进场（强看空信号）
    #   跌价 + OI 跌 = 多头平仓砍价（信号较弱）
    if momentum > 0.5 and oi_change > 3:
        bullish += 2
    elif momentum < -0.5 and oi_change > 3:
        bearish += 2
    elif momentum > 0.5 and oi_change < -3:
        bullish += 0.5  # 弱化
    elif momentum < -0.5 and oi_change < -3:
        bearish += 0.5

    # 4. 资金费率极端时削弱同向信号（过度拥挤）
    if fr is not None:
        if fr >= 0.001 and bullish > bearish:  # 0.1%/8h 极高
            bullish *= 0.5
        elif fr <= -0.0005 and bearish > bullish:
            bearish *= 0.5

    # 判决
    diff = bullish - bearish
    if abs(diff) < 1.0:
        return "震荡"
    if diff >= 1.0:
        return "↑ 偏多"
    return "↓ 偏空"
