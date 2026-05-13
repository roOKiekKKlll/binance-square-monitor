"""Trading signal and paper-position helpers.

Default behavior is paper trading. Live order placement is intentionally not
implemented here; it should be added behind an explicit live switch later.

架构说明：
- 风控决策（仓位 sizing / 熔断 / 冷却 / 集中度）统一由 risk.py 提供
- 本模块只负责：组装数据 -> 调 risk 做决策 -> 落库
- 这样实盘接入时，风控逻辑可以 100% 复用，只需替换 open_paper_position
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import config
import storage
import risk
from analyzer import compute_short_scores, compute_composite_scores
from market import get_mark_price, get_klines_1h


MAX_ENTRY_CHANGE_15M = 5.0
MAX_ENTRY_CHANGE_1H = 20.0
MIN_ENTRY_TAKER_RATIO = 1.15
ARCHIVE_FUNDING_HOT_PCT = 0.05
ARCHIVE_LONG_SHORT_HOT = 2.0
ARCHIVE_TAKER_WEAK = 1.15
REALTIME_PRICE_MAX_AGE_SECONDS = 5
VERDICT_ORDER = {
    "✅ 看起来健康": 0,
    "🎯 值得留意": 1,
    "⚠️ 过热预警": 2,
    "📉 信号偏弱": 3,
    "⚪ 中性": 4,
    "数据不足": 5,
}


def _loads(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _load_market(conn, token: str) -> dict:
    row = storage.snapshot_get(conn, token)
    if not row:
        return {"snapshot": {}, "analysis": {}, "updated_at": None}
    return {
        "snapshot": _loads(row.get("snapshot"), {}),
        "analysis": _loads(row.get("analysis"), {}),
        "updated_at": row.get("updated_at"),
    }


def _load_realtime(conn, token: str) -> dict:
    row = storage.realtime_get(conn, token)
    if not row:
        return {}
    data = _loads(row.get("snapshot"), {})
    data["cache_updated_at"] = row.get("updated_at")
    return data


def _current_price(market: dict, realtime: dict) -> float | None:
    snap = market.get("snapshot") or {}
    for key in ("last_trade_price", "mark_price", "best_ask", "best_bid"):
        val = realtime.get(key)
        if val:
            return float(val)
    val = snap.get("mark_price")
    return float(val) if val else None


def _timestamp_age_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _position_price(token: str, market: dict, realtime: dict) -> float | None:
    age = _timestamp_age_seconds(realtime.get("cache_updated_at"))
    if age is not None and age <= REALTIME_PRICE_MAX_AGE_SECONDS:
        price = _current_price(market, realtime)
        if price:
            return price

    fresh_price = get_mark_price(token)
    if fresh_price:
        return fresh_price
    return _current_price(market, realtime)


def _entry_limit_price(realtime: dict, fallback_price: float) -> float:
    bid = realtime.get("best_bid")
    ask = realtime.get("best_ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return fallback_price


def _pct(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _is_benchmark_token_excluded(token: str) -> bool:
    benchmark = str(getattr(config, "TRADING_REGIME_BENCHMARK_TOKEN", "BTC") or "BTC").upper()
    exclude_benchmark = bool(getattr(config, "TRADING_REGIME_EXCLUDE_BENCHMARK_FROM_TRADING", True))
    return exclude_benchmark and token.upper() == benchmark


def _evaluate_market_regime(conn, settings: dict | None = None) -> dict:
    """市场状态过滤（可开关）。

    返回:
      {
        "enabled": bool,
        "state": "trend" | "neutral" | "risk_off" | "disabled",
        "allow_open": bool,
        "reason": str,
      }
    """
    bench = str(getattr(config, "TRADING_REGIME_BENCHMARK_TOKEN", "BTC") or "BTC").upper()
    if settings is None:
        try:
            settings = storage.trading_settings_get(conn)
        except Exception:
            settings = {}
    enabled = settings.get(
        "regime_filter_enabled",
        getattr(config, "TRADING_REGIME_FILTER_ENABLED", False),
    )
    if not enabled:
        return {
            "enabled": False,
            "benchmark_token": bench,
            "state": "disabled",
            "allow_open": True,
            "reason": "regime_filter_off",
        }
    market = _load_market(conn, bench)
    snap = market.get("snapshot") or {}
    ch1h = _pct(snap.get("change_1h_pct"))
    ch4h = _pct(snap.get("change_4h_pct"))
    oi1h = _pct(snap.get("oi_change_1h_pct"))

    if ch1h is None or ch4h is None or oi1h is None:
        return {
            "enabled": True,
            "benchmark_token": bench,
            "state": "neutral",
            "allow_open": False,
            "reason": f"{bench} 行情数据不足（1h/4h/OI1h）",
        }

    risk_off_1h = float(getattr(config, "TRADING_REGIME_RISK_OFF_1H_PCT", -1.5))
    risk_off_4h = float(getattr(config, "TRADING_REGIME_RISK_OFF_4H_PCT", -4.0))
    trend_min_1h = float(getattr(config, "TRADING_REGIME_TREND_MIN_1H_PCT", 0.2))
    trend_min_4h = float(getattr(config, "TRADING_REGIME_TREND_MIN_4H_PCT", 0.8))
    trend_min_oi_1h = float(getattr(config, "TRADING_REGIME_TREND_MIN_OI_1H_PCT", -1.0))

    if ch1h <= risk_off_1h or ch4h <= risk_off_4h:
        return {
            "enabled": True,
            "benchmark_token": bench,
            "state": "risk_off",
            "allow_open": False,
            "reason": (
                f"{bench} risk_off: 1h={ch1h:+.2f}% 4h={ch4h:+.2f}% "
                f"(阈值 {risk_off_1h:+.2f}%/{risk_off_4h:+.2f}%)"
            ),
        }

    is_trend = (
        ch1h >= trend_min_1h and
        ch4h >= trend_min_4h and
        oi1h >= trend_min_oi_1h
    )
    if is_trend:
        return {
            "enabled": True,
            "benchmark_token": bench,
            "state": "trend",
            "allow_open": True,
            "reason": (
                f"{bench} trend: 1h={ch1h:+.2f}% 4h={ch4h:+.2f}% OI1h={oi1h:+.2f}%"
            ),
        }

    return {
        "enabled": True,
        "benchmark_token": bench,
        "state": "neutral",
        "allow_open": False,
        "reason": (
            f"{bench} neutral: 1h={ch1h:+.2f}% 4h={ch4h:+.2f}% OI1h={oi1h:+.2f}% "
            f"(趋势阈值 {trend_min_1h:+.2f}%/{trend_min_4h:+.2f}%/{trend_min_oi_1h:+.2f}%)"
        ),
    }


def market_regime_status(conn, settings: dict | None = None) -> dict:
    """公开给 web/监控面板使用的当前市场状态。"""
    return _evaluate_market_regime(conn, settings=settings)


def _margin_pnl_pct(realized: float, unrealized: float, margin: float) -> float:
    return ((realized + unrealized) / (margin or 1)) * 100


def _realized_delta(side: str, entry: float, exit_price: float, qty: float) -> float:
    side = (side or "LONG").upper()
    if side == "SHORT":
        return (entry - exit_price) * qty
    return (exit_price - entry) * qty


def evaluate_candidate(
    conn,
    score_row: dict,
    rank: int,
    market: dict,
    realtime: dict,
    regime: dict | None = None,
) -> dict:
    """
    评估一个候选币是否可以开仓。

    新架构（v2）：调用 risk.evaluate_entry_quality 得到 tier（full/half/skip），
    而不是所有条件硬 AND。这样轻度不满足的信号可以半仓进场，同时保留追高硬否决。

    返回字典新增字段：
      tier:         "full" / "half" / "skip"
      pass_count:   7 项核心条件通过数
      hard_block:   硬否决原因列表（非空则必 skip）
    原有 passed 字段继续保留，语义 = tier != "skip"，用于兼容旧代码。
    """
    token = score_row["token"]
    snap = market.get("snapshot") or {}
    analysis = market.get("analysis") or {}
    verdict = analysis.get("verdict") or ""
    signal_score = analysis.get("score")

    quality = risk.evaluate_entry_quality(snap, realtime, signal_score, verdict)
    if regime is None:
        regime = _evaluate_market_regime(conn)
    side = "LONG"

    tier = quality["tier"]
    passed = tier != "skip"

    # 把 reasons 组装成旧格式，方便 UI 继续展示
    reasons = []
    for r in quality["reasons_pass"]:
        reasons.append("OK " + r)
    for r in quality["reasons_fail"]:
        reasons.append("NO " + r)
    for r in quality["hard_block"]:
        reasons.insert(0, "⛔ " + r)

    price = _current_price(market, realtime)
    if price is None or price <= 0:
        passed = False
        tier = "skip"
        reasons.append("NO 缺少可用价格")

    suggestion = {
        "full": "可开多（满仓）",
        "half": "可开多（半仓）",
        "skip": "观察",
    }[tier]
    if quality["hard_block"]:
        suggestion = "不追高"

    if regime.get("enabled"):
        state = regime.get("state")
        if state == "risk_off":
            # risk_off 下将榜单信号切到开空模式（仍要求有可用价格）
            side = "SHORT"
            if price is not None and price > 0:
                passed = True
                if tier == "skip":
                    tier = "full"
                suggestion = "可开空（Regime risk_off）"
                reasons.insert(0, f"OK 市场状态过滤: {regime.get('reason')} -> 切换开空")
            else:
                passed = False
                tier = "skip"
                suggestion = "观察（市场状态过滤）"
                reasons.insert(0, f"⛔ 市场状态过滤: {regime.get('reason')}")
        elif not regime.get("allow_open"):
            passed = False
            tier = "skip"
            suggestion = "观察（市场状态过滤）"
            reasons.insert(0, f"⛔ 市场状态过滤: {regime.get('reason')}")

    return {
        "token": token,
        "rank": rank,
        "passed": passed,
        "tier": tier,
        "pass_count": quality["pass_count"],
        "hard_block": quality["hard_block"],
        "suggestion": suggestion,
        "reasons": reasons,
        "price": price,
        "limit_price": _entry_limit_price(realtime, price) if price else None,
        "market": market,
        "realtime": realtime,
        "analysis_score": signal_score,
        "market_regime": regime,
        "side": side,
    }


def build_trade_candidates_from_leaderboard(
        conn, leaderboard_items: list[dict], limit: int | None = None,
        passed_only: bool = False) -> list[dict]:
    candidates = []
    items = leaderboard_items[:limit] if limit else leaderboard_items
    signal_key = storage.leaderboard_signal_key(conn)
    regime = _evaluate_market_regime(conn)
    for rank, item in enumerate(items, 1):
        token = item["token"]
        if _is_benchmark_token_excluded(token):
            continue
        market = item.get("market") or _load_market(conn, token)
        if not (market.get("snapshot") or {}).get("mark_price"):
            continue
        realtime = _load_realtime(conn, token)
        score_row = item.get("score_row") or item
        result = evaluate_candidate(conn, score_row, rank, market, realtime, regime=regime)
        result["score"] = score_row
        result["signal_key"] = signal_key
        result["has_active_position"] = storage.trade_has_active(conn, token)
        if passed_only and not result.get("passed"):
            continue
        candidates.append(result)
    return candidates


def build_trade_candidates(conn, limit: int = 20, passed_only: bool = False) -> list[dict]:
    raw_scores = compute_short_scores(conn)
    scores = compute_composite_scores(conn, raw_scores, config.COMPOSITE_HISTORY_WINDOW)
    signal_key = storage.leaderboard_signal_key(conn)
    sortable = []
    for score_row in scores:
        market = _load_market(conn, score_row["token"])
        if not (market.get("snapshot") or {}).get("mark_price"):
            continue
        verdict = (market.get("analysis") or {}).get("verdict", "")
        sortable.append((score_row, market, VERDICT_ORDER.get(verdict, 99)))
    sortable.sort(key=lambda item: (
        item[2],
        -(item[0].get("composite_score") or 0),
        -(item[0].get("score") or 0),
    ))
    candidates = []
    regime = _evaluate_market_regime(conn)
    for rank, (score_row, market, _) in enumerate(sortable[:limit], 1):
        if _is_benchmark_token_excluded(score_row["token"]):
            continue
        realtime = _load_realtime(conn, score_row["token"])
        result = evaluate_candidate(conn, score_row, rank, market, realtime, regime=regime)
        result["score"] = score_row
        result["signal_key"] = signal_key
        result["has_active_position"] = storage.trade_has_active(conn, score_row["token"])
        if passed_only and not result.get("passed"):
            continue
        candidates.append(result)
    return candidates


def position_remaining_ratio(pos: dict) -> float:
    """返回仓位剩余比例（0~1）。

    PARTIAL 状态下，已被 TP1/TP2 平掉的部分不再占用保证金，
    剩余比例 = (quantity - closed_qty) / quantity。

    PENDING / OPEN / 数量异常时返回 1.0（视为完整仓位）。
    """
    try:
        qty = float(pos.get("quantity") or 0)
        closed = float(pos.get("closed_qty") or 0)
    except (TypeError, ValueError):
        return 1.0
    if qty <= 0:
        return 1.0
    remaining = (qty - closed) / qty
    if remaining < 0:
        return 0.0
    if remaining > 1:
        return 1.0
    return remaining


def position_live_margin(pos: dict) -> float:
    """根据 status 和 closed_qty 返回当前真正占用的保证金。

    - PENDING：原始保证金（订单未成交但已预留）
    - OPEN / PARTIAL：margin_amount × 剩余比例
    - 其他（CLOSED / CANCELED）：0
    """
    status = pos.get("status")
    margin = float(pos.get("margin_amount") or 0)
    if status == "PENDING":
        return margin
    if status in {"OPEN", "PARTIAL"}:
        return margin * position_remaining_ratio(pos)
    return 0.0


def position_live_notional(pos: dict) -> float:
    """同 position_live_margin，但返回名义价值（仓位价值）。"""
    status = pos.get("status")
    notional = float(pos.get("notional") or 0)
    if status == "PENDING":
        return notional
    if status in {"OPEN", "PARTIAL"}:
        return notional * position_remaining_ratio(pos)
    return 0.0


def account_summary(conn) -> dict:
    settings = storage.trading_settings_get(conn)
    positions = storage.trade_positions_all(conn, limit=500)
    initial = float(settings.get("initial_balance") or 0)
    realized = sum(float(p.get("realized_pnl") or 0) for p in positions)
    unrealized = sum(float(p.get("unrealized_pnl") or 0)
                     for p in positions if p.get("status") in {"OPEN", "PARTIAL"})
    # 实时占用保证金：PARTIAL 仓位按剩余 qty 比例计算（已被 TP1/TP2 平掉的部分释放）
    locked = sum(position_live_margin(p) for p in positions)
    equity = initial + realized + unrealized
    available = initial + realized - locked
    return {
        "settings": settings,
        "initial_balance": round(initial, 4),
        "equity": round(equity, 4),
        "available_balance": round(available, 4),
        "locked_margin": round(locked, 4),
        "realized_pnl": round(realized, 4),
        "unrealized_pnl": round(unrealized, 4),
    }


def _build_account_context(conn, executor=None) -> risk.AccountContext:
    """组装风控决策需要的账户上下文。只读，不修改数据库。
    executor: 传入 live executor 时从交易所查真实余额。
    """
    from executor import BinanceLiveExecutor
    if executor and isinstance(executor, BinanceLiveExecutor):
        balance_info = executor.get_account_balance()
        equity = balance_info.get("balance", 0)
        available = balance_info.get("available", 0)
        unrealized = balance_info.get("unrealized_pnl", 0)
    else:
        summary = account_summary(conn)
        equity = summary["equity"]
        available = summary["available_balance"]
        unrealized = summary["unrealized_pnl"]

    open_positions = storage.trade_open_positions(conn)

    # 按板块聚合
    by_sector = {}
    for pos in open_positions:
        sec = risk.sector_of(pos["token"])
        by_sector[sec] = by_sector.get(sec, 0) + 1

    return risk.AccountContext(
        equity=equity,
        available_balance=available,
        realized_pnl_today=storage.trade_realized_pnl_today(conn),
        unrealized_pnl=unrealized,
        open_positions_count=len(open_positions),
        open_positions_by_sector=by_sector,
        trades_opened_today=storage.trade_count_today_opened(conn),
        last_stop_loss_by_token=storage.trade_last_stop_loss_map(
            conn, hours=max(2, config.TRADING_COOLDOWN_MINUTES_AFTER_LOSS // 30 + 1)),
    )


def _debug_reject(token: str, reason: str, candidate: dict = None):
    """TRADING_DEBUG 为 True 时打印开仓拒绝原因到 stderr，便于诊断"""
    if not getattr(config, "TRADING_DEBUG", False):
        return
    import sys
    extra = ""
    if candidate is not None:
        tier = candidate.get("tier", "?")
        score = candidate.get("analysis_score", "?")
        extra = f" | tier={tier} signal_score={score}"
    print(f"[trade-debug] REJECT {token}: {reason}{extra}", file=sys.stderr, flush=True)


def open_position(conn, candidate: dict, settings: dict, executor=None) -> bool | dict:
    """
    统一开仓接口：paper / live 共用同一套验证和 sizing 逻辑。

    决策流程：
      1. 基础去重（是否已有持仓 / signal lock）
      2. 账户级风控（日亏损熔断 / 持仓上限 / 冷却期 / 板块集中度）
      3. 计算 ATR 自适应止损
      4. 按 tier（full/half）和风险反推仓位
      5. 下单（paper: 直接写 DB / live: 调 executor）
      6. 落库

    返回：True 成功 / False 失败。
    失败原因在 TRADING_DEBUG=True 时会打印到 stderr。
    """
    from executor import PaperExecutor, BinanceLiveExecutor
    if executor is None:
        executor = PaperExecutor()

    is_live = isinstance(executor, BinanceLiveExecutor)
    mode = "live" if is_live else "paper"
    token = (candidate.get("token") or "").upper() or "?"
    if _is_benchmark_token_excluded(token):
        _debug_reject(token, "基准币仅用于市场状态判断，不参与开仓", candidate)
        return False

    if not candidate.get("passed"):
        _debug_reject(token, "candidate.passed=False（信号评估不通过）", candidate)
        return False
    if candidate.get("has_active_position"):
        _debug_reject(token, "已有活跃持仓", candidate)
        return False

    tier = candidate.get("tier", "full")
    side = (candidate.get("side") or "LONG").upper()
    if side not in {"LONG", "SHORT"}:
        _debug_reject(token, f"side 非法 ({side})", candidate)
        return False
    if tier == "skip":
        _debug_reject(token, "tier=skip", candidate)
        return False

    if storage.trade_has_active(conn, token):
        _debug_reject(token, "DB 中已有活跃仓位（并发保护）", candidate)
        return False

    regime = _evaluate_market_regime(conn, settings=settings)
    regime_blocks = regime.get("enabled") and not regime.get("allow_open")
    short_override = regime.get("state") == "risk_off" and side == "SHORT"
    if regime_blocks and not short_override:
        _debug_reject(token, f"市场状态过滤: {regime.get('reason')}", candidate)
        return False

    # 账户级风控
    account = _build_account_context(conn, executor)
    risk_decision = risk.check_account_risk(account, token)
    if not risk_decision.allowed:
        _debug_reject(token, f"账户风控: {risk_decision.reason}", candidate)
        return False

    # 获取当前价：实盘开仓前优先使用"新鲜价格"重估，避免候选里遗留的旧缓存价
    # 导致按低价算出过大数量（实际成交名义显著偏离目标名义）。
    raw_price = candidate.get("price")
    if is_live:
        market_ctx = candidate.get("market") or {}
        realtime_ctx = candidate.get("realtime") or {}
        # 仅在候选携带行情上下文时做 freshness 校验；避免无上下文时触发不必要的实时请求。
        has_price_context = bool((market_ctx.get("snapshot") or {})) or bool(realtime_ctx)
        if has_price_context:
            refreshed_price = _position_price(token, market_ctx, realtime_ctx)
            if refreshed_price and refreshed_price > 0:
                raw_price = refreshed_price
    if not raw_price or raw_price <= 0:
        _debug_reject(token, f"价格无效 ({raw_price})", candidate)
        return False

    # paper 模式加模拟滑点估算止损，live 模式用原始价格（实际成交价由交易所决定）
    estimated_entry = raw_price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)

    # 计算 ATR 止损（基于估算入场价）
    klines = get_klines_1h(token, limit=max(30, config.TRADING_ATR_PERIOD + 2))
    stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
    stop_distance_pct = abs(stop_pct)
    if side == "LONG":
        stop_loss_price = estimated_entry * (1 - stop_distance_pct / 100)
    else:
        stop_loss_price = estimated_entry * (1 + stop_distance_pct / 100)

    # 计算仓位（先算，sizing 失败不应该消耗 signal_lock）
    leverage = float(settings.get("leverage") or config.TRADING_LEVERAGE)
    sizing = risk.compute_position_size(account, estimated_entry, stop_loss_price, leverage, tier, side=side)
    if sizing.get("quantity", 0) <= 0:
        _debug_reject(token, f"仓位计算: {sizing.get('note')}", candidate)
        return False

    quantity = sizing["quantity"]
    margin = sizing["margin"]
    notional = sizing["notional"]
    risk_amount = sizing["risk_amount"]

    # 实盘硬限额检查（同样在抢锁前，避免被限额挡住却消耗 lock）
    if is_live:
        max_size = getattr(config, "LIVE_MAX_POSITION_SIZE_USD", 500)
        if notional > max_size:
            _debug_reject(token, f"名义价值 ${notional:.0f} 超过实盘单笔限额 ${max_size:.0f}", candidate)
            return False
        max_total = getattr(config, "LIVE_MAX_TOTAL_EXPOSURE_USD", 2000)
        # 注意：用 live_notional 而不是原始 notional，
        # PARTIAL 仓位已被 TP1/TP2 平掉的部分不应再算作敞口
        existing_notional = sum(
            position_live_notional(p) for p in storage.trade_open_positions(conn)
            if p.get("mode") == "live"
        )
        if existing_notional + notional > max_total:
            _debug_reject(
                token,
                f"总敞口 ${existing_notional + notional:.0f} 超过限额 ${max_total:.0f}",
                candidate,
            )
            return False

    # 止盈：基于 R 值
    risk_per_unit = abs(estimated_entry - stop_loss_price)
    if side == "LONG":
        tp1_price = estimated_entry + risk_per_unit * config.TRADING_TP1_R
        tp2_price = estimated_entry + risk_per_unit * config.TRADING_TP2_R
    else:
        tp1_price = estimated_entry - risk_per_unit * config.TRADING_TP1_R
        tp2_price = estimated_entry - risk_per_unit * config.TRADING_TP2_R

    snapshot = {
        **candidate,
        "_risk_meta": {
            "tier": tier,
            "stop_mode": stop_mode,
            "stop_distance_pct": sizing.get("stop_distance_pct"),
            "risk_amount": risk_amount,
            "risk_pct_of_equity": (risk_amount / account.equity * 100) if account.equity else None,
            "sector": risk.sector_of(token),
            "account_equity_at_open": account.equity,
            "mode": mode,
        },
    }

    # === 抢 signal lock（已经走到这一步，所有前置检查全部通过）===
    # 只在真正要下单的最后一刻抢锁。这样 sizing/限额失败不会浪费 lock，
    # 同时下单失败也会主动释放，不会让本轮 heat round 内的同一 token 再也开不了仓。
    signal_key = candidate.get("signal_key") or storage.leaderboard_signal_key(conn)
    if not storage.trade_signal_lock_acquire(conn, token, signal_key):
        _debug_reject(token, f"signal_lock 已占用 (signal_key={signal_key})", candidate)
        return False

    # === 下单（paper 失败释放 lock；live 失败保留 lock，避免同信号循环刷手续费）===
    symbol = f"{token}USDT"
    tp1_qty = quantity * (config.TRADING_TP1_CLOSE_PCT / 100)
    try:
        order_result = executor.open_long(
            symbol=symbol,
            quantity=quantity,
            entry_price=estimated_entry,
            stop_loss_price=stop_loss_price,
            tp1_price=tp1_price,
            tp1_qty=tp1_qty,
            leverage=int(leverage),
        ) if side == "LONG" else executor.open_short(
            symbol=symbol,
            quantity=quantity,
            entry_price=estimated_entry,
            stop_loss_price=stop_loss_price,
            tp1_price=tp1_price,
            tp1_qty=tp1_qty,
            leverage=int(leverage),
        )
    except Exception as e:
        if not is_live:
            storage.trade_signal_lock_release(conn, token, signal_key)
        _debug_reject(token, f"下单异常: {e}", candidate)
        raise

    if not order_result.success:
        if not is_live:
            storage.trade_signal_lock_release(conn, token, signal_key)
        _debug_reject(token, f"下单失败: {order_result.error}", candidate)
        return False

    # 使用实际成交价（live）或模拟滑点价（paper）
    entry_price = order_result.fill_price or estimated_entry
    actual_qty = order_result.fill_qty or quantity

    # 用实际成交价重新计算止盈止损
    if is_live and entry_price != estimated_entry:
        if side == "LONG":
            stop_loss_price = entry_price * (1 - stop_distance_pct / 100)
        else:
            stop_loss_price = entry_price * (1 + stop_distance_pct / 100)
        risk_per_unit = abs(entry_price - stop_loss_price)
        if side == "LONG":
            tp1_price = entry_price + risk_per_unit * config.TRADING_TP1_R
            tp2_price = entry_price + risk_per_unit * config.TRADING_TP2_R
        else:
            tp1_price = entry_price - risk_per_unit * config.TRADING_TP1_R
            tp2_price = entry_price - risk_per_unit * config.TRADING_TP2_R
        notional = entry_price * actual_qty
        margin = notional / leverage
    placed_stop = (order_result.extra or {}).get("placed_stop_price")
    if is_live and placed_stop:
        stop_loss_price = float(placed_stop)

    mode_label = "实盘" if is_live else "模拟"
    stop_pending = bool(order_result.extra.get("stop_order_pending")) if order_result.extra else False
    position = {
        "token": token,
        "symbol": symbol,
        "side": side,
        "status": "OPEN",
        "mode": mode,
        "margin_amount": margin,
        "leverage": leverage,
        "notional": notional,
        "quantity": actual_qty,
        "entry_price": entry_price,
        "limit_price": entry_price,
        "current_price": entry_price,
        "stop_loss_price": stop_loss_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "highest_price": entry_price,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps(snapshot, default=str, ensure_ascii=False),
        "open_reason": (
            f"{mode_label}开仓 tier={tier} | "
            f"方向={side} | "
            f"信号分={candidate.get('analysis_score')} | "
            f"通过 {candidate.get('pass_count')}/7 | "
            f"止损 {stop_mode} {sizing.get('stop_distance_pct', 0):.2f}% | "
            f"风险 ${risk_amount:.2f} ({(risk_amount/account.equity*100) if account.equity else 0:.2f}% equity)"
        ),
        "advice": (
            f"⚠️ 保护单未挂上，请人工检查交易所；原因: {order_result.extra.get('stop_error', '')}"
            if stop_pending else
            f"{'满仓' if tier == 'full' else '半仓'}持有：等待 +{config.TRADING_TP1_R}R 止盈 / "
            f"{sizing.get('stop_distance_pct', 0):.2f}% 止损"
        ),
    }
    pos_id = storage.trade_position_insert(conn, position)
    if not pos_id:
        # DB 写入失败：lock 释放避免阻塞重试。
        # 实盘场景特别危险 —— 交易所已有真实仓位但本地无记录，无法管理止盈止损。
        # 紧急平仓兜底，避免出现"漂在交易所的孤儿仓位"。
        storage.trade_signal_lock_release(conn, token, signal_key)
        if is_live:
            import sys
            print(
                f"[trade-logic] ⚠️ 实盘已开仓但 DB 写入失败 {token}！尝试紧急平仓 qty={actual_qty}",
                file=sys.stderr, flush=True,
            )
            try:
                close_result = executor.close_position(symbol, actual_qty, side=side, reason="db_insert_failed")
                if close_result.success:
                    print(
                        f"[trade-logic] 紧急平仓成功 {token}",
                        file=sys.stderr, flush=True,
                    )
                else:
                    print(
                        f"[trade-logic] ⚠️⚠️ 紧急平仓也失败 {token}: {close_result.error}！请手动检查交易所",
                        file=sys.stderr, flush=True,
                    )
            except Exception as e:
                print(
                    f"[trade-logic] ⚠️⚠️ 紧急平仓异常 {token}: {e}！请手动检查交易所",
                    file=sys.stderr, flush=True,
                )
        _debug_reject(token, "DB insert 失败（唯一索引冲突？）", candidate)
        return False

    # live 模式：记录交易所订单 ID
    if is_live and order_result.extra and isinstance(pos_id, int):
        storage.trade_position_update_order_ids(conn, pos_id, {
            "exchange_entry_order_id": order_result.extra.get("entry_order_id") or order_result.order_id,
            "exchange_stop_order_id": order_result.extra.get("stop_order_id"),
            "exchange_tp1_order_id": order_result.extra.get("tp1_order_id"),
            "actual_entry_price": entry_price,
        })
    return True


def open_paper_position(conn, candidate: dict, settings: dict) -> bool | dict:
    """兼容别名：调用统一的 open_position，使用 PaperExecutor。"""
    from executor import PaperExecutor
    return open_position(conn, candidate, settings, PaperExecutor())


def manual_open_on_watch(conn, token: str, settings: dict, executor=None) -> dict:
    """
    收藏时按设置金额和倍数开多（paper / live 均支持）。

    v2 改动：
    - 仓位用风险反推（和自动交易一致）
    - 止损用 ATR 自适应
    - 收藏是用户强意愿，豁免部分账户级风控（持仓上限/板块集中度），
      但保留最关键的熔断和止损冷却（通过 config 开关控制）
    - 返回详细 reason，前端 toast 能直接展示
    """
    from executor import PaperExecutor, BinanceLiveExecutor
    if executor is None:
        executor = PaperExecutor()

    is_live = isinstance(executor, BinanceLiveExecutor)
    mode = "live" if is_live else "paper"
    token = token.upper()
    if _is_benchmark_token_excluded(token):
        return {"ok": False, "reason": f"{token} 仅作为 Regime 基准，不参与开仓"}

    if storage.trade_has_active(conn, token):
        return {"ok": False, "reason": f"{token} 已有持仓或挂单"}

    market = _load_market(conn, token)
    realtime = _load_realtime(conn, token)
    # 手动开仓同样优先用新鲜价格，避免旧 realtime 缓存导致估算入场价失真。
    raw_price = _position_price(token, market, realtime)
    if not raw_price or raw_price <= 0:
        return {"ok": False, "reason": f"{token} 缺少可用市价（可能没有永续合约或接口超时）"}

    # 账户级风控（收藏豁免部分）
    account = _build_account_context(conn, executor)
    risk_decision = risk.check_account_risk(
        account, token,
        bypass_max_concurrent=config.MANUAL_BYPASS_MAX_CONCURRENT,
        bypass_sector_limit=config.MANUAL_BYPASS_SECTOR_LIMIT,
        bypass_cooldown=config.MANUAL_BYPASS_COOLDOWN,
    )
    if not risk_decision.allowed:
        return {"ok": False, "reason": f"风控拦截：{risk_decision.reason}"}

    estimated_entry = raw_price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)
    klines = get_klines_1h(token, limit=max(30, config.TRADING_ATR_PERIOD + 2))
    stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
    stop_loss_price = estimated_entry * (1 + stop_pct / 100)

    leverage = float(settings.get("leverage") or config.TRADING_LEVERAGE)
    # 手动开仓默认满仓档
    sizing = risk.compute_position_size(account, estimated_entry, stop_loss_price, leverage, "full")
    if sizing.get("quantity", 0) <= 0:
        note = sizing.get("note", "未知")
        if "余额" in note:
            return {"ok": False, "reason": (
                f"可用余额不足：{note}。"
                f" 当前 equity=${account.equity:.2f}，已锁定保证金=${account.equity - account.available_balance:.2f}"
            )}
        if "名义" in note:
            return {"ok": False, "reason": (
                f"按风险反推的仓位太小：{note}。"
                f" 可尝试：1) 增大账户余额 2) 把 TRADING_SIZING_MODE 改成 'fixed_margin'"
            )}
        return {"ok": False, "reason": f"仓位计算失败：{note}"}

    quantity = sizing["quantity"]
    margin = sizing["margin"]
    notional = sizing["notional"]
    risk_amount = sizing["risk_amount"]

    # 实盘硬限额
    if is_live:
        max_size = getattr(config, "LIVE_MAX_POSITION_SIZE_USD", 500)
        if notional > max_size:
            return {"ok": False, "reason": f"名义价值 ${notional:.0f} 超过实盘单笔限额 ${max_size:.0f}"}
        max_total = getattr(config, "LIVE_MAX_TOTAL_EXPOSURE_USD", 2000)
        existing_notional = sum(
            position_live_notional(p) for p in storage.trade_open_positions(conn)
            if p.get("mode") == "live"
        )
        if existing_notional + notional > max_total:
            return {"ok": False, "reason": f"总敞口 ${existing_notional + notional:.0f} 超过限额 ${max_total:.0f}"}

    risk_per_unit = estimated_entry - stop_loss_price
    tp1_price = estimated_entry + risk_per_unit * config.TRADING_TP1_R
    tp2_price = estimated_entry + risk_per_unit * config.TRADING_TP2_R

    # 下单
    symbol = f"{token}USDT"
    tp1_qty = quantity * (config.TRADING_TP1_CLOSE_PCT / 100)
    order_result = executor.open_long(
        symbol=symbol,
        quantity=quantity,
        entry_price=estimated_entry,
        stop_loss_price=stop_loss_price,
        tp1_price=tp1_price,
        tp1_qty=tp1_qty,
        leverage=int(leverage),
    )
    if not order_result.success:
        return {"ok": False, "reason": f"下单失败: {order_result.error}"}

    entry_price = order_result.fill_price or estimated_entry
    actual_qty = order_result.fill_qty or quantity

    if is_live and entry_price != estimated_entry:
        stop_loss_price = entry_price * (1 + stop_pct / 100)
        risk_per_unit = entry_price - stop_loss_price
        tp1_price = entry_price + risk_per_unit * config.TRADING_TP1_R
        tp2_price = entry_price + risk_per_unit * config.TRADING_TP2_R
        notional = entry_price * actual_qty
        margin = notional / leverage
    placed_stop = (order_result.extra or {}).get("placed_stop_price")
    if is_live and placed_stop:
        stop_loss_price = float(placed_stop)

    snapshot = {
        "manual": True,
        "trigger": "watchlist_add",
        "market": market,
        "realtime": realtime,
        "settings": settings,
        "_risk_meta": {
            "tier": "manual_full",
            "stop_mode": stop_mode,
            "stop_distance_pct": sizing.get("stop_distance_pct"),
            "risk_amount": risk_amount,
            "sector": risk.sector_of(token),
            "mode": mode,
        },
    }
    mode_label = "实盘" if is_live else "模拟"
    position = {
        "token": token,
        "symbol": symbol,
        "side": "LONG",
        "status": "OPEN",
        "mode": mode,
        "margin_amount": margin,
        "leverage": leverage,
        "notional": notional,
        "quantity": actual_qty,
        "entry_price": entry_price,
        "limit_price": entry_price,
        "current_price": entry_price,
        "stop_loss_price": stop_loss_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "highest_price": entry_price,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps(snapshot, default=str, ensure_ascii=False),
        "open_reason": (
            f"手动{mode_label}开仓 | 止损 {stop_mode} {sizing.get('stop_distance_pct', 0):.2f}% | "
            f"风险 ${risk_amount:.2f}"
        ),
        "advice": f"手动开仓持有：等待止盈或 {sizing.get('stop_distance_pct', 0):.2f}% 止损",
    }
    pos_id = storage.trade_position_insert(conn, position)
    if not pos_id:
        # DB 写入失败：实盘场景必须兜底，避免交易所出现孤儿仓位。
        if is_live:
            import sys
            print(
                f"[trade-logic] ⚠️ 实盘手动开仓已成交但 DB 写入失败 {token}，尝试紧急平仓 qty={actual_qty}",
                file=sys.stderr, flush=True,
            )
            try:
                close_result = executor.close_position(symbol, actual_qty, side="LONG", reason="db_insert_failed")
                if close_result.success:
                    print(
                        f"[trade-logic] 手动开仓 DB 失败后紧急平仓成功 {token}",
                        file=sys.stderr, flush=True,
                    )
                else:
                    print(
                        f"[trade-logic] ⚠️⚠️ 手动开仓 DB 失败后紧急平仓失败 {token}: {close_result.error}，请手动检查交易所",
                        file=sys.stderr, flush=True,
                    )
            except Exception as e:
                print(
                    f"[trade-logic] ⚠️⚠️ 手动开仓 DB 失败后紧急平仓异常 {token}: {e}，请手动检查交易所",
                    file=sys.stderr, flush=True,
                )
        return {"ok": False, "reason": "DB 写入失败（可能并发冲突）"}

    # live 模式：记录交易所订单 ID
    if is_live and order_result.extra and isinstance(pos_id, int):
        storage.trade_position_update_order_ids(conn, pos_id, {
            "exchange_entry_order_id": order_result.extra.get("entry_order_id") or order_result.order_id,
            "exchange_stop_order_id": order_result.extra.get("stop_order_id"),
            "exchange_tp1_order_id": order_result.extra.get("tp1_order_id"),
            "actual_entry_price": entry_price,
        })

    return {
        "ok": True, "token": token,
        "entry_price": entry_price,
        "quantity": actual_qty,
        "stop_loss_price": stop_loss_price,
        "risk_amount": risk_amount,
        "note": f"按风险 ${risk_amount:.2f} {mode_label}开仓，止损 @ ${stop_loss_price:.6g}",
    }


def manual_close_on_unwatch(conn, token: str, executor=None) -> dict:
    """取消收藏时平仓。paper 模式模拟市价，live 模式真实平仓+撤单。"""
    from executor import PaperExecutor, BinanceLiveExecutor
    if executor is None:
        executor = PaperExecutor()
    is_live = isinstance(executor, BinanceLiveExecutor)

    token = token.upper()
    market = _load_market(conn, token)
    realtime = _load_realtime(conn, token)
    price = _position_price(token, market, realtime)
    positions = [p for p in storage.trade_open_positions(conn) if p["token"].upper() == token]
    closed = 0
    canceled = 0
    realized_delta = 0.0

    for pos in positions:
        symbol = pos.get("symbol", f"{token}USDT")
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        realized = float(pos.get("realized_pnl") or 0)

        if pos["status"] == "PENDING":
            # live 模式：撤掉交易所挂单
            if is_live:
                executor.client.cancel_all_orders(symbol)
            storage.trade_position_update(conn, pos["id"], {
                "status": "CANCELED",
                "advice": "取消收藏触发：未成交挂单已取消",
                "closed_at": "__CURRENT_TIMESTAMP__",
            })
            canceled += 1
            continue

        if open_qty <= 0:
            continue

        # live 模式：先撤所有委托再市价平仓
        if is_live and pos.get("mode") == "live":
            try:
                executor.client.cancel_all_orders(symbol)
            except Exception:
                pass
            close_result = executor.close_position(
                symbol, open_qty, side=(pos.get("side") or "LONG"), reason="unwatch"
            )
            if close_result.success:
                exit_price = close_result.fill_price or price or 0
            else:
                # 平仓失败，跳过这个仓位
                continue
        else:
            if not price or price <= 0:
                continue
            exit_price = price

        entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or pos.get("limit_price") or exit_price)
        pnl = (exit_price - entry) * open_qty
        realized += pnl
        realized_delta += pnl
        storage.trade_position_update(conn, pos["id"], {
            "status": "CLOSED",
            "current_price": exit_price,
            "closed_qty": qty,
            "realized_pnl": realized,
            "unrealized_pnl": 0,
            "pnl_pct": (realized / float(pos.get("margin_amount") or 1)) * 100,
            "advice": f"取消收藏触发：{'实盘' if is_live else '模拟'}市价平仓",
            "closed_at": "__CURRENT_TIMESTAMP__",
        })
        closed += 1

    return {
        "ok": closed > 0 or canceled > 0,
        "token": token,
        "closed": closed,
        "canceled": canceled,
        "price": price,
        "realized_pnl": realized_delta,
        "reason": None if (closed or canceled) else "没有可平仓位或缺少市价",
    }


def _failure_tags(entry_snapshot: dict, exit_market: dict, exit_realtime: dict) -> list[str]:
    tags = []
    market = (entry_snapshot or {}).get("market") or {}
    snap = market.get("snapshot") or {}
    analysis = market.get("analysis") or {}
    exit_snap = (exit_market or {}).get("snapshot") or {}

    if "健康" not in (analysis.get("verdict") or ""):
        tags.append("entry_not_healthy")

    ch15 = _pct(snap.get("change_15m_pct"))
    ch1h = _pct(snap.get("change_1h_pct"))
    funding = _pct(snap.get("funding_rate_pct"))
    lsr = _pct(snap.get("long_short_ratio"))
    if ch15 is not None and ch15 > MAX_ENTRY_CHANGE_15M:
        tags.append("entry_15m_hot")
    if ch1h is not None and ch1h > MAX_ENTRY_CHANGE_1H:
        tags.append("entry_1h_hot")
    if funding is not None and funding >= ARCHIVE_FUNDING_HOT_PCT:
        tags.append("entry_funding_hot")
    if lsr is not None and lsr >= ARCHIVE_LONG_SHORT_HOT:
        tags.append("entry_lsr_hot")

    if (_pct(exit_snap.get("oi_change_15m_pct")) or 0) <= 0:
        tags.append("oi15_reversed")
    if (_pct(exit_snap.get("oi_change_1h_pct")) or 0) <= 0:
        tags.append("oi1h_reversed")
    if (_pct(exit_snap.get("oi_change_4h_pct")) or 0) <= 0:
        tags.append("oi4h_reversed")

    taker_exit = _pct((exit_realtime or {}).get("trade_buy_sell_ratio_60s")
                      or exit_snap.get("taker_buy_sell_ratio"))
    if taker_exit is not None and taker_exit < ARCHIVE_TAKER_WEAK:
        tags.append("buy_pressure_faded")

    return tags or ["price_hit_stop"]


def _archive_stop_loss(conn, pos: dict, exit_price: float, realized: float,
                       market: dict, realtime: dict):
    entry_snapshot = _loads(pos.get("signal_snapshot"), {})
    tags = _failure_tags(entry_snapshot, market, realtime)
    storage.trade_loss_archive_add(conn, {
        "position_id": pos.get("id"),
        "token": pos.get("token"),
        "symbol": pos.get("symbol"),
        "entry_price": pos.get("entry_price") or pos.get("limit_price"),
        "exit_price": exit_price,
        "realized_pnl": realized,
        "pnl_pct": (realized / float(pos.get("margin_amount") or 1)) * 100,
        "failed_reason": "-2% stop loss hit",
        "reason_tags": json.dumps(tags, ensure_ascii=False),
        "entry_snapshot": pos.get("signal_snapshot"),
        "exit_snapshot": json.dumps({
            "market": market,
            "realtime": realtime,
            "exit_price": exit_price,
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, default=str, ensure_ascii=False),
    })


def update_paper_positions(conn):
    positions = [p for p in storage.trade_open_positions(conn) if p.get("mode") != "live"]
    for pos in positions:
        market = _load_market(conn, pos["token"])
        realtime = _load_realtime(conn, pos["token"])
        price = _position_price(pos["token"], market, realtime)
        if not price:
            continue

        status = pos["status"]
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        realized = float(pos.get("realized_pnl") or 0)
        fields = {"current_price": price}

        if status == "PENDING":
            limit_price = float(pos.get("limit_price") or 0)
            created_at = datetime.fromisoformat(str(pos["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            if price <= limit_price:
                fields.update({
                    "status": "OPEN",
                    "entry_price": limit_price,
                    "current_price": price,
                    "highest_price": price,
                    "advice": "持仓中：等待止盈或止损",
                })
            elif age >= config.TRADING_LIMIT_ORDER_TIMEOUT_SECONDS:
                fields.update({
                    "status": "CANCELED",
                    "advice": "限价单超时未成交，已取消",
                    "closed_at": "__CURRENT_TIMESTAMP__",
                })
            storage.trade_position_update(conn, pos["id"], fields)
            continue

        entry = float(pos.get("entry_price") or pos.get("limit_price") or 0)
        side = (pos.get("side") or "LONG").upper()
        if entry <= 0 or open_qty <= 0:
            continue

        extreme = float(pos.get("highest_price") or entry)
        if side == "SHORT":
            extreme = min(extreme, price)
        else:
            extreme = max(extreme, price)
        fields["highest_price"] = extreme
        tp1 = float(pos.get("tp1_price") or 0)
        tp2 = float(pos.get("tp2_price") or 0)
        stop = float(pos.get("stop_loss_price") or 0)

        # ---- 止损：考虑滑点（比 stop 价更差的价格成交）----
        stop_hit = (stop > 0 and price <= stop) if side == "LONG" else (stop > 0 and price >= stop)
        if stop_hit:
            # 真实场景止损触发时常有滑点；paper 交易模拟更保守的成交价
            slip_pct = config.TRADING_STOP_SLIPPAGE_PCT / 100
            if side == "LONG":
                fill_price = min(price, stop * (1 - slip_pct))
            else:
                fill_price = max(price, stop * (1 + slip_pct))
            realized += _realized_delta(side, entry, fill_price, open_qty)
            _archive_stop_loss(conn, pos, fill_price, realized, market, realtime)
            fields.update({
                "status": "CLOSED",
                "current_price": fill_price,
                "closed_qty": qty,
                "realized_pnl": realized,
                "unrealized_pnl": 0,
                "pnl_pct": _margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                "advice": f"止损触发 @ ${fill_price:.6g}（含假设滑点），已平仓",
                "closed_at": "__CURRENT_TIMESTAMP__",
            })
            storage.trade_position_update(conn, pos["id"], fields)
            continue

        # ---- 止盈 TP1：达到 +1R，平 TP1_CLOSE_PCT%，止损移到保本 ----
        tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
        tp2_pct = config.TRADING_TP2_CLOSE_PCT / 100
        # 用"是否已触发过某档"而不是脆弱的数量比较
        closed_ratio = closed_qty / qty if qty > 0 else 0
        tp1_done = closed_ratio >= tp1_pct - 1e-6
        tp2_done = closed_ratio >= (tp1_pct + tp2_pct) - 1e-6

        tp1_hit = (tp1 > 0 and price >= tp1) if side == "LONG" else (tp1 > 0 and price <= tp1)
        if not tp1_done and tp1_hit:
            close_qty = qty * tp1_pct
            realized += _realized_delta(side, entry, tp1, close_qty)
            closed_qty += close_qty
            open_qty = qty - closed_qty
            fields.update({
                "status": "PARTIAL",
                "closed_qty": closed_qty,
                "realized_pnl": realized,
                "stop_loss_price": entry,  # 保本
                "advice": f"+{config.TRADING_TP1_R}R 已平 {config.TRADING_TP1_CLOSE_PCT:.0f}%，止损移到保本",
            })
            tp1_done = True

        # ---- 止盈 TP2：只在 TP1 已触发且 TP2 未触发时考虑 ----
        tp2_hit = (tp2 > 0 and price >= tp2) if side == "LONG" else (tp2 > 0 and price <= tp2)
        if tp1_done and not tp2_done and tp2_hit:
            close_qty = qty * tp2_pct
            realized += _realized_delta(side, entry, tp2, close_qty)
            closed_qty += close_qty
            open_qty = qty - closed_qty
            if side == "LONG":
                trailing = extreme * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100)
            else:
                trailing = extreme * (1 + config.TRADING_TRAIL_CALLBACK_PCT / 100)
            fields.update({
                "status": "PARTIAL",
                "closed_qty": closed_qty,
                "realized_pnl": realized,
                "trailing_stop_price": trailing,
                "advice": f"+{config.TRADING_TP2_R}R 已再平 {config.TRADING_TP2_CLOSE_PCT:.0f}%，剩余跟踪止盈",
            })
            tp2_done = True

        # ---- 剩余仓位：跟踪止盈 ----
        if tp2_done and open_qty > 0:
            old_trailing = float(pos.get("trailing_stop_price") or 0)
            if side == "LONG":
                trailing = max(old_trailing, extreme * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100))
            else:
                candidate_trailing = extreme * (1 + config.TRADING_TRAIL_CALLBACK_PCT / 100)
                trailing = candidate_trailing if old_trailing <= 0 else min(old_trailing, candidate_trailing)
            fields["trailing_stop_price"] = trailing
            trailing_hit = (price <= trailing) if side == "LONG" else (price >= trailing)
            if trailing_hit:
                # 跟踪止盈触发，也假设一点滑点
                slip_pct = config.TRADING_STOP_SLIPPAGE_PCT / 100
                if side == "LONG":
                    fill_price = min(price, trailing * (1 - slip_pct))
                else:
                    fill_price = max(price, trailing * (1 + slip_pct))
                realized += _realized_delta(side, entry, fill_price, open_qty)
                fields.update({
                    "status": "CLOSED",
                    "current_price": fill_price,
                    "closed_qty": qty,
                    "realized_pnl": realized,
                    "unrealized_pnl": 0,
                    "pnl_pct": _margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                    "advice": f"跟踪止盈触发 @ ${fill_price:.6g}，已平仓",
                    "closed_at": "__CURRENT_TIMESTAMP__",
                })
                storage.trade_position_update(conn, pos["id"], fields)
                continue

        unrealized = _realized_delta(side, entry, price, open_qty)
        fields.update({
            "unrealized_pnl": unrealized,
            "pnl_pct": _margin_pnl_pct(realized, unrealized, float(pos.get("margin_amount") or 1)),
        })
        storage.trade_position_update(conn, pos["id"], fields)
