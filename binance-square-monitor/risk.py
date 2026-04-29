"""
风控中枢：集中处理仓位 sizing、止损计算、熔断/冷却/集中度检查。

设计原则（专业量化风格）：
1. 先定单笔风险（% of equity），再反推仓位，而不是反过来
2. 止损用 ATR 自适应，不用一刀切的固定百分比
3. 多层熔断：单币冷却 / 日亏损熔断 / 最大并发持仓 / 板块集中度
4. 所有决策都返回 (allowed, reason, details)，方便日志 & UI 展示

这个模块**不直接读写数据库**，接收已经查好的数据，返回决策。
这样更容易单元测试，也方便以后接实盘 API。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal

import config


# ---------- 板块粗分类（用于集中度风控） ----------
# 不求精确，求"不要同时在相似赛道上重复下注"
SECTOR_MAP = {
    # L1 / 主流
    "BTC": "majors", "ETH": "majors",
    # L2
    "ARB": "l2", "OP": "l2", "STRK": "l2", "ZK": "l2", "MANTA": "l2", "METIS": "l2",
    # Meme
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "WIF": "meme", "BONK": "meme",
    "FLOKI": "meme", "MEME": "meme", "BOME": "meme", "POPCAT": "meme", "MEW": "meme",
    # AI
    "FET": "ai", "AGIX": "ai", "OCEAN": "ai", "RNDR": "ai", "WLD": "ai", "TAO": "ai",
    "AI16Z": "ai", "VIRTUAL": "ai",
    # DeFi
    "UNI": "defi", "AAVE": "defi", "CRV": "defi", "MKR": "defi", "LDO": "defi",
    # 其他公链
    "SOL": "alt_l1", "AVAX": "alt_l1", "SUI": "alt_l1", "APT": "alt_l1", "SEI": "alt_l1",
    "INJ": "alt_l1", "NEAR": "alt_l1", "TIA": "alt_l1",
}


def sector_of(token: str) -> str:
    """返回 token 所属板块；没登记的一律归 'other'。"""
    return SECTOR_MAP.get(token.upper(), "other")


# ---------- 数据类：一次检查需要的账户上下文 ----------

@dataclass
class AccountContext:
    """一次风控检查需要的账户级上下文。由调用方（trade_logic）组装。"""
    equity: float                                  # 账户净值 = initial + realized + unrealized
    available_balance: float                       # 可用余额 = initial + realized - locked
    realized_pnl_today: float = 0.0                # 今日已实现盈亏
    unrealized_pnl: float = 0.0                    # 当前浮动盈亏
    open_positions_count: int = 0                  # 当前活跃仓位数（含 PENDING/OPEN/PARTIAL）
    open_positions_by_sector: dict = field(default_factory=dict)  # {sector: count}
    trades_opened_today: int = 0                   # 今日已开仓次数
    last_stop_loss_by_token: dict = field(default_factory=dict)   # {token: datetime} 最近一次止损时间


# ---------- 决策结果 ----------

@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    tier: Literal["full", "half", "skip"] = "skip"  # 开仓档位
    size_multiplier: float = 0.0                    # 最终仓位相对"满仓"的倍数
    details: dict = field(default_factory=dict)


# ==========================================================================
#                           ATR 止损计算
# ==========================================================================

def compute_atr_pct(klines_1h: list[dict], period: int = 14) -> float | None:
    """
    从 1h K 线算 ATR 百分比（ATR / 当前价）。
    K 线格式：{"high", "low", "close"}；输入按时间升序。

    返回：ATR 占当前价的百分比（如 2.5 表示 2.5%）。数据不足时返回 None。
    """
    if not klines_1h or len(klines_1h) < period + 1:
        return None

    trs = []
    prev_close = None
    for kl in klines_1h[-(period + 1):]:
        try:
            high = float(kl["high"])
            low = float(kl["low"])
            close = float(kl["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    last_close = float(klines_1h[-1]["close"])
    if last_close <= 0:
        return None
    return (atr / last_close) * 100


def compute_stop_distance_pct(klines_1h: list[dict] | None) -> tuple[float, str]:
    """
    计算止损距离百分比（返回负数，如 -2.3 表示止损在入场价下方 2.3%）。

    返回：(stop_pct, mode)
      stop_pct: 止损距离，如 -2.3
      mode: 使用的模式，用于日志（如 "atr:14x1.5" / "fixed" / "atr_fallback_fixed"）
    """
    if config.TRADING_STOP_MODE == "fixed":
        return config.TRADING_STOP_LOSS_PCT, "fixed"

    # ATR 模式
    atr_pct = compute_atr_pct(klines_1h or [], config.TRADING_ATR_PERIOD)
    if atr_pct is None:
        # K 线拿不到，退回固定止损
        return config.TRADING_STOP_LOSS_PCT, "atr_fallback_fixed"

    raw_stop = -(atr_pct * config.TRADING_ATR_STOP_MULTIPLIER)
    # 夹在 [MAX, MIN] 之间（注意都是负数，MIN 更靠近 0）
    clamped = max(config.TRADING_STOP_LOSS_MAX_PCT,
                  min(config.TRADING_STOP_LOSS_MIN_PCT, raw_stop))
    mode = f"atr:{config.TRADING_ATR_PERIOD}x{config.TRADING_ATR_STOP_MULTIPLIER}"
    if clamped != raw_stop:
        mode += ":clamped"
    return clamped, mode


# ==========================================================================
#                           仓位 sizing
# ==========================================================================

def compute_position_size(
    account: AccountContext,
    entry_price: float,
    stop_price: float,
    leverage: float,
    tier: Literal["full", "half"] = "full",
) -> dict:
    """
    基于风险反推仓位。

    风险金额 = equity × RISK_PER_TRADE_PCT%
    仓位（币数量） = 风险金额 / |entry - stop|
    名义价值 = 数量 × entry
    所需保证金 = 名义价值 / leverage

    tier="half" 时，风险减半。

    返回：{"quantity", "notional", "margin", "risk_amount", "stop_distance_pct", "note"}
    若无法下单返回 {"quantity": 0, "note": "原因"}
    """
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        return {"quantity": 0, "note": "stop_price 不合法（>=entry）"}

    risk_pct = config.TRADING_RISK_PER_TRADE_PCT / 100.0
    if tier == "half":
        risk_pct *= 0.5

    equity = max(account.equity, 0)
    if equity <= 0:
        return {"quantity": 0, "note": "账户净值为 0"}

    # 兼容旧模式：fixed_margin 时直接用保证金开仓，风险反算出来
    if config.TRADING_SIZING_MODE == "fixed_margin":
        margin = float(config.TRADING_ORDER_AMOUNT)
        if tier == "half":
            margin *= 0.5
        notional = margin * leverage
        quantity = notional / entry_price
        risk_amount = (entry_price - stop_price) * quantity
        stop_distance_pct = (stop_price - entry_price) / entry_price * 100
        return {
            "quantity": quantity,
            "notional": notional,
            "margin": margin,
            "risk_amount": risk_amount,
            "stop_distance_pct": stop_distance_pct,
            "note": f"fixed_margin tier={tier}",
        }

    # risk_based（默认）
    risk_amount = equity * risk_pct
    per_unit_risk = entry_price - stop_price  # > 0
    quantity = risk_amount / per_unit_risk
    notional = quantity * entry_price
    margin = notional / leverage

    # 名义价值上限检查
    max_notional = equity * (config.TRADING_MAX_NOTIONAL_PCT / 100.0)
    if notional > max_notional:
        # 按上限缩
        scale = max_notional / notional
        quantity *= scale
        notional *= scale
        margin *= scale
        risk_amount *= scale
        note = f"risk_based tier={tier} 被名义上限压缩 ×{scale:.2f}"
    else:
        note = f"risk_based tier={tier}"

    # 最小名义价值检查
    if notional < config.TRADING_MIN_NOTIONAL:
        return {"quantity": 0, "note": f"名义价值 ${notional:.2f} < 下限 ${config.TRADING_MIN_NOTIONAL}"}

    # 可用余额检查
    if margin > account.available_balance:
        return {"quantity": 0, "note": f"可用余额 ${account.available_balance:.2f} 不足，需 ${margin:.2f}"}

    stop_distance_pct = (stop_price - entry_price) / entry_price * 100
    return {
        "quantity": quantity,
        "notional": notional,
        "margin": margin,
        "risk_amount": risk_amount,
        "stop_distance_pct": stop_distance_pct,
        "note": note,
    }


# ==========================================================================
#                           入场质量评分 / 分档
# ==========================================================================

def evaluate_entry_quality(
    snap: dict,
    realtime: dict,
    signal_score: float | None,
    analysis_verdict: str,
) -> dict:
    """
    评估入场质量：返回通过的核心条件数、tier、原因列表。

    7 项核心条件（每项都是可选通过，最后按通过数 + signal_score 分档）：
      1. analyzer verdict 包含 "健康"
      2. 15m 涨幅在允许区间 [回调下限, MAX_ENTRY_CHANGE_15M]
         —— 允许小幅回调入场（买回调比追急拉健康）
      3. 1h 涨幅 ∈ [0, MAX_ENTRY_CHANGE_1H]
      4. OI 15m 增加
      5. OI 1h 增加
      6. OI 4h 增加
      7. 主动买卖比 ∈ [MIN_TAKER, MAX_TAKER]（双边门槛）

    硬否决（任一命中直接 skip，不管其他）：
      ⚠️ 基于历史止损归档数据，以下情况是高频失败场景：
      - 4h 涨幅 > MAX_CHANGE_4H_PCT（追高 · 已有）
      - 24h 涨幅 > MAX_CHANGE_24H_PCT（追高 · 已有）
      - verdict 是 "过热预警"
      - funding_rate >= MAX_ENTRY_FUNDING_PCT（多头拥挤 · 对应 entry_funding_hot 标签）
      - long_short_ratio >= MAX_ENTRY_LSR（散户情绪过热 · 对应 entry_lsr_hot 标签）
      - taker_ratio >= MAX_ENTRY_TAKER_RATIO（买盘透支 · 对应 buy_pressure_faded 前兆）
    """
    from trade_logic import (
        MAX_ENTRY_CHANGE_15M, MAX_ENTRY_CHANGE_1H, MIN_ENTRY_TAKER_RATIO
    )

    reasons_pass = []
    reasons_fail = []
    hard_block = []

    def pct(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    verdict = analysis_verdict or ""
    ch15 = pct(snap.get("change_15m_pct"))
    ch1h = pct(snap.get("change_1h_pct"))
    ch4h = pct(snap.get("change_4h_pct"))
    ch24h = pct(snap.get("change_24h_pct"))
    oi15 = pct(snap.get("oi_change_15m_pct"))
    oi1h = pct(snap.get("oi_change_1h_pct"))
    oi4h = pct(snap.get("oi_change_4h_pct"))
    taker = pct(realtime.get("trade_buy_sell_ratio_60s") or snap.get("taker_buy_sell_ratio"))
    # v2.2 新增：funding / lsr 读取用于硬门槛
    fr_pct = pct(snap.get("funding_rate_pct"))
    lsr = pct(snap.get("long_short_ratio"))
    top_lsr = pct(snap.get("top_trader_ls_ratio"))
    # v2.4 新增：taker 趋势指标
    taker_trend = pct(snap.get("taker_trend_pct"))

    # ---- 硬否决：价格追高 ----
    if "过热" in verdict:
        hard_block.append(f"analyzer verdict 是过热预警")
    if ch4h is not None and ch4h > config.TRADING_MAX_CHANGE_4H_PCT:
        hard_block.append(f"4h 涨幅 {ch4h:+.1f}% 超过追高上限 {config.TRADING_MAX_CHANGE_4H_PCT}%")
    if ch24h is not None and ch24h > config.TRADING_MAX_CHANGE_24H_PCT:
        hard_block.append(f"24h 涨幅 {ch24h:+.1f}% 超过追高上限 {config.TRADING_MAX_CHANGE_24H_PCT}%")

    # ---- 硬否决：情绪过热（v2.2 新增，基于失败归档反哺）----
    max_fr = getattr(config, "TRADING_MAX_ENTRY_FUNDING_PCT", 0.05)
    max_lsr = getattr(config, "TRADING_MAX_ENTRY_LSR", 2.0)
    max_taker = getattr(config, "TRADING_MAX_ENTRY_TAKER_RATIO", 1.8)

    if fr_pct is not None and fr_pct >= max_fr:
        hard_block.append(f"资金费率 {fr_pct:.3f}%/8h 超 {max_fr}%，多头过度拥挤（历史高频失败模式）")
    if lsr is not None and lsr >= max_lsr:
        hard_block.append(f"散户多空比 {lsr:.2f} >= {max_lsr}，情绪过热（历史高频失败模式）")
    if taker is not None and taker >= max_taker:
        hard_block.append(f"主动买卖比 {taker:.2f} >= {max_taker}，买盘透支，容易消退")

    # ---- 硬否决：taker 趋势衰退（v2.4 新增，针对 buy_pressure_faded 失败模式）----
    # 即使当前 taker 在允许区间内，若最近 20m 里 taker_ratio 已明显下滑（买盘衰退），
    # 说明我们看到的是"派发顶"，而非"买盘启动"。此时入场极易被止损。
    max_taker_decay = getattr(config, "TRADING_MAX_TAKER_DECAY_PCT", -10.0)
    if taker_trend is not None and taker_trend <= max_taker_decay:
        hard_block.append(
            f"taker 趋势 {taker_trend:+.1f}% 衰退超阈值 {max_taker_decay:.0f}%，"
            f"买盘正在消退（历史 buy_pressure_faded 模式）"
        )

    # ---- 7 项核心条件 ----
    # 用 lambda 延迟格式化，避免 None:+.2f 在惰性分支外求值时报错
    def check(cond: bool, ok_fn, fail_fn):
        try:
            msg = ok_fn() if cond else fail_fn()
        except Exception:
            msg = "(格式化失败)"
        (reasons_pass if cond else reasons_fail).append(msg)
        return cond

    def _s(v, digits=2, sign=True):
        """安全格式化数字"""
        if v is None:
            return "-"
        fmt = f"{{:+.{digits}f}}" if sign else f"{{:.{digits}f}}"
        return fmt.format(v)

    # 15m 涨幅改为允许小回调到 MAX（默认 2%），下限为 PULLBACK（默认 -1.5%）
    ch15_lower = getattr(config, "TRADING_ALLOW_15M_PULLBACK_PCT", 0.0)
    ch15_upper = getattr(config, "TRADING_MAX_ENTRY_CHANGE_15M", MAX_ENTRY_CHANGE_15M)

    c1 = check("健康" in verdict,
               lambda: f"verdict 健康 ({verdict})",
               lambda: f"verdict 不健康 ({verdict or '-'})")
    c2 = check(ch15 is not None and ch15_lower <= ch15 <= ch15_upper,
               lambda: f"15m {_s(ch15)}% 在 [{ch15_lower:+.1f},{ch15_upper:+.1f}]%",
               lambda: f"15m {_s(ch15)}% 不在 [{ch15_lower:+.1f},{ch15_upper:+.1f}]%")
    c3 = check(ch1h is not None and 0 <= ch1h <= MAX_ENTRY_CHANGE_1H,
               lambda: f"1h 涨幅 {_s(ch1h)}% 在区间",
               lambda: f"1h 涨幅 {_s(ch1h)}% 不在 0-{MAX_ENTRY_CHANGE_1H}%")
    c4 = check(oi15 is not None and oi15 > 0,
               lambda: f"OI15m {_s(oi15, 1)}%",
               lambda: f"OI15m 未增加 ({_s(oi15, 1)}%)")
    c5 = check(oi1h is not None and oi1h > 0,
               lambda: f"OI1h {_s(oi1h, 1)}%",
               lambda: f"OI1h 未增加 ({_s(oi1h, 1)}%)")
    c6 = check(oi4h is not None and oi4h > 0,
               lambda: f"OI4h {_s(oi4h, 1)}%",
               lambda: f"OI4h 未增加 ({_s(oi4h, 1)}%)")
    # taker 改为双边门槛：既不能太弱（买盘不足）也不能太强（买盘透支）
    c7 = check(
        taker is not None and MIN_ENTRY_TAKER_RATIO < taker < max_taker,
        lambda: f"taker {_s(taker, 2, sign=False)} 在 ({MIN_ENTRY_TAKER_RATIO},{max_taker})",
        lambda: f"taker {_s(taker, 2, sign=False)} 不在 ({MIN_ENTRY_TAKER_RATIO},{max_taker})"
    )

    pass_count = sum([c1, c2, c3, c4, c5, c6, c7])
    all_pass = pass_count == 7

    # ---- "聪明钱分歧"加分机制：大户看多 + 散户看空，是经典的低风险入场信号 ----
    smart_money_bonus = False
    if getattr(config, "TRADING_PREFER_SMART_MONEY_DIVERGENCE", True):
        if top_lsr is not None and lsr is not None:
            if top_lsr > 1.5 and lsr < 0.7:
                smart_money_bonus = True
                reasons_pass.append(
                    f"聪明钱分歧: 大户LSR={top_lsr:.2f} > 1.5，散户LSR={lsr:.2f} < 0.7（升档）"
                )

    # ---- 决定 tier ----
    if hard_block:
        tier = "skip"
        reasons_fail = hard_block + reasons_fail
    elif config.TRADING_ENTRY_MODE == "strict":
        tier = "full" if all_pass else "skip"
    else:  # tiered
        sig = signal_score if signal_score is not None else 0
        if all_pass and sig >= config.TRADING_SIGNAL_FULL_THRESHOLD:
            tier = "full"
        elif pass_count >= config.TRADING_CORE_REQUIRED_PASS_COUNT and sig >= config.TRADING_SIGNAL_HALF_THRESHOLD:
            tier = "half"
        else:
            tier = "skip"

        # 聪明钱分歧升档：skip→half, half→full
        if smart_money_bonus:
            if tier == "skip" and pass_count >= 4 and sig >= (config.TRADING_SIGNAL_HALF_THRESHOLD - 5):
                tier = "half"
            elif tier == "half":
                tier = "full"

    return {
        "tier": tier,
        "pass_count": pass_count,
        "total_count": 7,
        "signal_score": signal_score,
        "hard_block": hard_block,
        "reasons_pass": reasons_pass,
        "reasons_fail": reasons_fail,
        "all_pass": all_pass,
        "smart_money_bonus": smart_money_bonus,
    }


# ==========================================================================
#                        组合/账户级风控检查
# ==========================================================================

def check_account_risk(
    account: AccountContext,
    token: str,
    now: datetime | None = None,
    bypass_max_concurrent: bool = False,
    bypass_sector_limit: bool = False,
    bypass_cooldown: bool = False,
) -> RiskDecision:
    """
    在开仓前检查账户级风险。不通过则返回 allowed=False。
    逻辑顺序：日亏损熔断 > 交易次数熔断 > 最大持仓数 > 冷却期 > 板块集中度。

    bypass_* 参数用于手动开仓（收藏触发）等用户强意愿场景，
    可以跳过部分限制。但日亏损熔断和日交易次数永不豁免。
    """
    now = now or datetime.now(timezone.utc)

    # 1. 日亏损熔断（永不豁免）
    if account.equity > 0:
        daily_loss = account.realized_pnl_today + account.unrealized_pnl
        daily_loss_pct = daily_loss / account.equity * 100
        if daily_loss_pct <= -config.TRADING_MAX_DAILY_LOSS_PCT:
            return RiskDecision(
                allowed=False,
                reason=f"日亏损熔断: 今日 {daily_loss_pct:.2f}% <= -{config.TRADING_MAX_DAILY_LOSS_PCT}%",
                details={"daily_loss_pct": daily_loss_pct},
            )

    # 2. 日交易次数上限（永不豁免）
    if account.trades_opened_today >= config.TRADING_MAX_DAILY_TRADES:
        return RiskDecision(
            allowed=False,
            reason=f"日交易次数已达上限 {config.TRADING_MAX_DAILY_TRADES}",
            details={"trades_today": account.trades_opened_today},
        )

    # 3. 最大并发持仓
    if not bypass_max_concurrent:
        # 0 = 不限制（靠余额自然约束）
        max_concurrent = config.TRADING_MAX_CONCURRENT_POSITIONS
        if max_concurrent > 0 and account.open_positions_count >= max_concurrent:
            return RiskDecision(
                allowed=False,
                reason=f"持仓数已达上限 {max_concurrent}",
                details={"open_count": account.open_positions_count},
            )

    # 4. 同 token 止损冷却期
    if not bypass_cooldown:
        last_stop = account.last_stop_loss_by_token.get(token.upper())
        if last_stop:
            # last_stop 可能是字符串，兼容一下
            if isinstance(last_stop, str):
                try:
                    last_stop = datetime.fromisoformat(last_stop.replace("Z", "+00:00"))
                except Exception:
                    last_stop = None
            if last_stop:
                if last_stop.tzinfo is None:
                    last_stop = last_stop.replace(tzinfo=timezone.utc)
                cooldown_min = config.TRADING_COOLDOWN_MINUTES_AFTER_LOSS
                if now - last_stop < timedelta(minutes=cooldown_min):
                    remain = cooldown_min - (now - last_stop).total_seconds() / 60
                    return RiskDecision(
                        allowed=False,
                        reason=f"{token} 止损冷却中，还剩 {remain:.1f} 分钟",
                        details={"cooldown_remaining_min": remain},
                    )

    # 5. 板块集中度
    if not bypass_sector_limit:
        sector = sector_of(token)
        sector_count = account.open_positions_by_sector.get(sector, 0)
        if sector != "other" and sector_count >= config.TRADING_CORRELATED_LIMIT:
            return RiskDecision(
                allowed=False,
                reason=f"板块 '{sector}' 已有 {sector_count} 个同向仓位（上限 {config.TRADING_CORRELATED_LIMIT}）",
                details={"sector": sector, "count": sector_count},
            )

    return RiskDecision(
        allowed=True,
        reason="账户级风控通过",
        details={"sector": sector_of(token)},
    )
