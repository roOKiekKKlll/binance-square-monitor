"""
Web 仪表盘服务
运行：python web.py
访问：http://localhost:8000
"""
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import config
import storage
import trade_logic
from analyzer import compute_short_scores, compute_composite_scores
from market import has_perpetual, get_market_snapshot, get_futures_symbols
from signals import analyze as analyze_signals


app = FastAPI(title="Binance Square Monitor")


# ============================================================
# 轻量内存缓存：给高频只读 API 加 2 秒 TTL
# 目的：worker 正在跑重活时，前端刷新不会每次都挤进 SQLite 排队
# ============================================================
_cache = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl_seconds: float, fn):
    """非常简单的内存缓存：<ttl 秒内返回缓存，过期重新计算"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl_seconds:
            return hit[1]
    # 缓存过期或未命中：重新计算（在锁外算，避免一个慢请求阻塞其他）
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _cache_invalidate(*keys):
    """用户写操作（收藏/取消/改设置）后调用，让缓存立即失效"""
    with _cache_lock:
        if not keys:
            _cache.clear()
        else:
            for k in keys:
                _cache.pop(k, None)


class TokenBody(BaseModel):
    token: str


class TradingSettingsBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    initial_balance: float | None = None
    leverage: int | None = None
    order_amount: float | None = None


class TradingResetBody(BaseModel):
    confirm: bool = False                    # 必须为 True 才执行，防误触
    new_initial_balance: float | None = None  # 可选：顺便改初始金额


_REALTIME_OVERRIDE_KEYS = (
    "mark_price",
    "last_trade_price",
    "best_bid",
    "best_bid_qty",
    "best_ask",
    "best_ask_qty",
    "bid_ask_spread_pct",
    "depth_bid_top_usd",
    "depth_ask_top_usd",
    "depth_imbalance_pct",
    "trade_buy_usd_60s",
    "trade_sell_usd_60s",
    "trade_buy_sell_ratio_60s",
    "trade_count_60s",
)


def _load_snapshot(conn, token: str) -> dict | None:
    """合并 worker 写的低频快照 + market_realtime.py 写的高频 ws 缓存。

    - market_snapshots（worker，~5min/次）：funding/oi/lsr/taker/k 线动量等慢变量
    - market_realtime_cache（ws，~1s/次）：mark_price/盘口/60s 主动买卖等快变量

    返回 dict 中 snapshot 字段会用 ws 缓存的同名字段覆盖 worker 的同名字段，
    并附带 realtime_updated_at 让前端能看出新鲜度。
    """
    row = storage.snapshot_get(conn, token)
    if not row:
        return None
    snapshot = json.loads(row["snapshot"]) if row["snapshot"] else {}
    analysis = json.loads(row["analysis"]) if row["analysis"] else {}

    realtime_updated_at = None
    rt_row = storage.realtime_get(conn, token)
    if rt_row and rt_row.get("snapshot"):
        try:
            rt_data = json.loads(rt_row["snapshot"]) or {}
        except Exception:
            rt_data = {}
        for key in _REALTIME_OVERRIDE_KEYS:
            val = rt_data.get(key)
            if val is not None:
                snapshot[key] = val
        realtime_updated_at = rt_row.get("updated_at")

    return {
        "token": row["token"],
        "snapshot": snapshot,
        "analysis": analysis,
        "updated_at": row["updated_at"],
        "realtime_updated_at": realtime_updated_at,
    }


def _snapshot_is_stale(snap_row: dict | None, ttl_seconds: int) -> bool:
    if not snap_row or not snap_row.get("updated_at"):
        return True
    raw = str(snap_row["updated_at"]).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() >= ttl_seconds


def _refresh_watchlist_tokens(tokens: list[str]) -> dict:
    """刷新观察列表合约快照，并同步更新锚定后的浮盈浮亏追踪。"""
    if not tokens:
        return {"refreshed": 0, "skipped_no_contract": 0, "tokens": []}

    with storage.get_conn() as conn:
        short_scores = compute_short_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    futures_set = get_futures_symbols()
    refreshed = 0
    skipped = 0
    for token in tokens:
        up = token.upper()
        if up not in futures_set:
            skipped += 1
            continue
        try:
            snap = get_market_snapshot(up)
        except Exception:
            continue
        if not snap:
            continue

        analysis = analyze_signals(snap, social_map.get(up, 0.0))
        snap_json = json.dumps(snap, default=str, ensure_ascii=False)
        ana_json = json.dumps(analysis, default=str, ensure_ascii=False)
        price = snap.get("mark_price") or 0

        with storage.get_conn() as conn:
            storage.snapshot_upsert(conn, up, snap_json, ana_json)
            entry = storage.entry_get(conn, up)
            if entry is None and price > 0:
                storage.entry_upsert(conn, up, price, snap_json, ana_json)
            elif entry is not None:
                anchor = entry.get("anchor_price") or 0
                if price > 0 and anchor > 0:
                    pnl = (price - anchor) / anchor * 100
                    storage.followup_add(
                        conn, up, price, pnl, snap_json, ana_json,
                        min_seconds=config.WATCHLIST_FOLLOWUP_MIN_INTERVAL_SECONDS,
                    )
                    storage.entry_update_extremes(conn, up, pnl)
                    if pnl <= config.LOSS_ARCHIVE_THRESHOLD_PCT and not entry.get("archived"):
                        storage.archive_loss_sample(conn, up, price, pnl)
        refreshed += 1
        time.sleep(0.3)

    return {"refreshed": refreshed, "skipped_no_contract": skipped, "tokens": tokens}


# verdict 的显示优先级（越靠前越靠上）
# 来源：signals.py 里生成的字符串，带 emoji
VERDICT_ORDER = {
    "✅ 看起来健康": 0,
    "🎯 值得留意": 1,
    "⚠ 过热预警": 2,
    "📉 信号偏弱": 3,
    "⚪ 中性": 4,
    "数据不足": 5,
}


def _verdict_rank(verdict: str) -> int:
    """未知 verdict 排到最后"""
    return VERDICT_ORDER.get(verdict, 99)


def _build_leaderboard_items(conn) -> tuple[list[dict], int]:
    raw_scores = compute_short_scores(conn)
    # 综合热度增强：加 composite_score, trend, prev_score 等
    scored = compute_composite_scores(conn, raw_scores, config.COMPOSITE_HISTORY_WINDOW)

    watchlist = set(storage.watchlist_get_all(conn))
    pool = []
    skipped_no_contract = 0
    for s in scored:
        snap_row = _load_snapshot(conn, s["token"])
        if not snap_row or not (snap_row.get("snapshot") or {}).get("mark_price"):
            skipped_no_contract += 1
            continue
        pool.append({
            "token": s["token"],
            "score": round(s["score"], 1),
            "composite_score": s["composite_score"],
            "trend": s["trend"],
            "prev_score": s["prev_score"],
            "avg_history_score": s["avg_history_score"],
            "peak_history_score": s["peak_history_score"],
            "appeared_rounds": s["appeared_rounds"],
            "mentions": s["mentions"],
            "unique_posts": s["unique_posts"],
            "unique_authors": s.get("unique_authors", 0),
            "raw_score": s.get("raw_score", round(s["score"], 1)),
            "author_capped_posts": s.get("author_capped_posts", 0),
            "similar_posts": s.get("similar_posts", 0),
            "total_likes": s["total_likes"],
            "total_comments": s["total_comments"],
            "total_shares": s["total_shares"],
            "in_watchlist": s["token"] in watchlist,
            "market": snap_row,
            "score_row": s,
        })

    # 排序：verdict 优先 → 综合热度降序 → 当前热度降序
    def sort_key(item):
        ana = (item["market"].get("analysis") or {})
        verdict = ana.get("verdict", "")
        return (
            _verdict_rank(verdict),
            -item["composite_score"],
            -item["score"],
        )

    pool.sort(key=sort_key)
    return pool[:config.COMPOSITE_HEAT_TOP_N], skipped_no_contract


@app.get("/api/leaderboard")
def api_leaderboard():
    """15 分钟综合热度榜
    - 基于历史若干轮热度的加权综合分排序
    - 只保留有合约快照的代币
    - 同档位 verdict 优先级排序
    - 每个代币带趋势标记（↑↑/↑/—/↓/↓↓/🆕）

    性能：2 秒缓存，避免前端频繁刷新时每次都重算
    """
    def compute():
        with storage.get_conn() as conn:
            result, skipped_no_contract = _build_leaderboard_items(conn)
        return {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": result,
            "skipped_no_contract": skipped_no_contract,
        }
    return _cached("leaderboard", 2.0, compute)


@app.get("/api/watchlist")
def api_watchlist():
    """观察列表 + 每个代币的合约数据 + 锚定信息 + 浮盈浮亏。

    数据口径：
    - 慢变量（funding/oi/lsr/taker 等）：worker 每轮（~5min）写入 market_snapshots。
    - 快变量（mark_price/盘口/60s 主动买卖）：market_realtime.py 持续写入
      market_realtime_cache，由 _load_snapshot 自动合并到 snapshot 上。
    - PnL 走合并后的最新价，因此本接口不再在请求里同步刷新合约 REST。
    - 用户想强制刷新慢变量请走 POST /api/watchlist/refresh。
    """
    with storage.get_conn() as conn:
        tokens = storage.watchlist_get_all(conn)
        scores = compute_short_scores(conn)
        score_map = {s["token"]: s for s in scores}

        items = []
        for token in tokens:
            snap_row = _load_snapshot(conn, token)
            social = score_map.get(token)
            entry = storage.entry_get(conn, token)

            cur_price = None
            if snap_row and snap_row.get("snapshot"):
                cur_price = (snap_row["snapshot"] or {}).get("mark_price")

            pnl_pct = None
            anchor_price = None
            if entry:
                anchor_price = entry.get("anchor_price")
                if cur_price and anchor_price and anchor_price > 0:
                    pnl_pct = round((cur_price - anchor_price) / anchor_price * 100, 2)

            items.append({
                "token": token,
                "social": social,
                "market": snap_row,
                "anchor_price": anchor_price,
                "anchored_at": entry.get("anchored_at") if entry else None,
                "current_price": cur_price,
                "pnl_pct": pnl_pct,
                "max_drawdown": entry.get("max_drawdown") if entry else None,
                "peak_profit": entry.get("peak_profit") if entry else None,
                "archived": bool(entry.get("archived")) if entry else False,
            })
    return {"items": items}


@app.post("/api/watchlist/add")
def api_watchlist_add(body: TokenBody):
    """收藏代币 + 用当前最新合约快照作为锚定"""
    token = body.token.strip().upper()
    if not token:
        raise HTTPException(400, "token required")
    with storage.get_conn() as conn:
        storage.watchlist_add(conn, token)
        # 尝试用缓存里最新的快照建锚定
        snap_row = _load_snapshot(conn, token)
        if snap_row and snap_row.get("snapshot"):
            snap = snap_row["snapshot"]
            price = snap.get("mark_price") if isinstance(snap, dict) else None
            if price and price > 0:
                storage.entry_upsert(
                    conn, token, price,
                    json.dumps(snap_row.get("snapshot"), default=str, ensure_ascii=False),
                    json.dumps(snap_row.get("analysis"), default=str, ensure_ascii=False),
                )
        settings = storage.trading_settings_get(conn)
        mode = settings.get("mode", "paper")
        from executor import get_executor
        try:
            executor = get_executor(mode)
        except Exception:
            executor = get_executor("paper")
        trade = trade_logic.manual_open_on_watch(conn, token, settings, executor)
    _cache_invalidate()  # 收藏后所有缓存都失效
    return {"ok": True, "token": token, "trade": trade}


@app.post("/api/watchlist/remove")
def api_watchlist_remove(body: TokenBody):
    """取消收藏，同时删掉锚定和追踪记录"""
    token = body.token.strip().upper()
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
        mode = settings.get("mode", "paper")
        from executor import get_executor
        try:
            executor = get_executor(mode)
        except Exception:
            executor = get_executor("paper")
        trade = trade_logic.manual_close_on_unwatch(conn, token, executor)
        storage.watchlist_remove(conn, token)
        storage.entry_delete(conn, token)
    _cache_invalidate()
    return {"ok": True, "token": token, "trade": trade}


@app.post("/api/watchlist/refresh")
def api_watchlist_refresh():
    """同步刷新观察列表所有代币的合约数据（直接调币安公开 API）
    这和 worker 写入的是同一张表，刷新后前端拿到的是最新数据
    """
    with storage.get_conn() as conn:
        tokens = storage.watchlist_get_all(conn)
        short_scores = compute_short_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    if not tokens:
        return {"ok": True, "refreshed": 0, "skipped_no_contract": 0, "tokens": []}

    try:
        result = _refresh_watchlist_tokens(tokens)
    except Exception as e:
        raise HTTPException(503, f"刷新观察列表合约数据失败: {e}")
    return {"ok": True, **result}


@app.get("/api/loss_samples")
def api_loss_samples():
    """已归档的负面样本统计（供学习参考）"""
    with storage.get_conn() as conn:
        stats = storage.loss_samples_stats(conn)
    return stats


@app.get("/api/status")
def api_status():
    """Worker 的当前状态（供前端进度面板显示）"""
    with storage.get_conn() as conn:
        s = storage.status_get(conn)
    if not s:
        return {
            "stage": "unknown",
            "detail": "Worker 尚未运行，请在另一个终端运行 python worker.py",
            "running": False,
        }
    # 判断心跳是否近期（>60s 视为掉线）
    last = s.get("last_heartbeat")
    running = False
    if last:
        try:
            # SQLite CURRENT_TIMESTAMP 是 UTC，格式 "YYYY-MM-DD HH:MM:SS"
            last_dt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            running = age < 60
            s["heartbeat_age_seconds"] = round(age)
        except Exception:
            pass
    s["running"] = running
    s["round_duration_seconds"] = config.SCRAPE_ROUND_SECONDS
    return s


@app.get("/api/trading")
def api_trading():
    """交易面板：账户、持仓、候选信号。默认模拟交易。

    性能：2 秒缓存，前端频繁轮询不会每次都重算 candidates
    """
    def compute():
        with storage.get_conn() as conn:
            account = trade_logic.account_summary(conn)
            positions = storage.trade_positions_all(conn, limit=30)
            leaderboard_items, _ = _build_leaderboard_items(conn)
            candidates = trade_logic.build_trade_candidates_from_leaderboard(
                conn, leaderboard_items, passed_only=True)
            loss_archive = storage.trade_loss_archive_stats(conn)
        return {
            "account": account,
            "positions": positions,
            "candidates": candidates,
            "loss_archive": loss_archive,
        }
    return _cached("trading", 2.0, compute)


@app.post("/api/trading/settings")
def api_trading_settings(body: TradingSettingsBody):
    fields = {}
    for key in ("enabled", "mode", "initial_balance", "leverage", "order_amount"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value
    if "mode" in fields and fields["mode"] not in {"paper", "live"}:
        raise HTTPException(400, "mode must be paper or live")
    if "mode" in fields and fields["mode"] == "live":
        import os
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        if not os.getenv("BINANCE_API_KEY") or not os.getenv("BINANCE_API_SECRET"):
            raise HTTPException(400, "切换到 live 模式需要先在 .env 文件中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET")
        try:
            from executor import get_executor, BinanceLiveExecutor
            executor = get_executor("live")
            if isinstance(executor, BinanceLiveExecutor):
                executor.get_account_balance()
        except Exception as e:
            raise HTTPException(400, f"API 验证失败: {e}")
    if "mode" in fields and fields["mode"] == "paper":
        with storage.get_conn() as conn:
            live_positions = storage.trade_live_open_positions(conn)
        if live_positions:
            tokens = [p["token"] for p in live_positions]
            raise HTTPException(400,
                f"还有 {len(live_positions)} 个实盘仓位未平仓（{', '.join(tokens)}），"
                "请先平仓或使用紧急平仓后再切换到 paper 模式"
            )
    if "leverage" in fields and fields["leverage"] <= 0:
        raise HTTPException(400, "leverage must be positive")
    if "order_amount" in fields and fields["order_amount"] <= 0:
        raise HTTPException(400, "order_amount must be positive")
    if "initial_balance" in fields and fields["initial_balance"] <= 0:
        raise HTTPException(400, "initial_balance must be positive")
    with storage.get_conn() as conn:
        storage.trading_settings_update(conn, fields)
        settings = storage.trading_settings_get(conn)
    _cache_invalidate("trading")
    return {"ok": True, "settings": settings}


@app.post("/api/trading/reset")
def api_trading_reset(body: TradingResetBody):
    """
    一键重置交易数据：清空所有持仓、信号锁、止损归档。
    可选地同时更新初始金额。配置（enabled/mode/leverage 等）保留。

    安全：前端必须显式传 confirm=true 才会执行。
    """
    if not body.confirm:
        raise HTTPException(400, "需要 confirm=true 以确认重置")
    if body.new_initial_balance is not None and body.new_initial_balance <= 0:
        raise HTTPException(400, "new_initial_balance 必须为正数")

    with storage.get_conn() as conn:
        result = storage.trade_reset_all(conn, body.new_initial_balance)
    _cache_invalidate()  # 全清，立即看到空状态
    return {"ok": True, **result}


@app.post("/api/trading/emergency_close_all")
def api_emergency_close_all():
    """紧急平仓：撤掉所有实盘委托 + 市价全平所有实盘仓位"""
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
    if settings.get("mode") != "live":
        raise HTTPException(400, "仅在 live 模式下可用")
    from executor import get_executor, BinanceLiveExecutor
    import live_manager
    executor = get_executor("live")
    if not isinstance(executor, BinanceLiveExecutor):
        raise HTTPException(500, "无法获取实盘执行器")
    result = live_manager.emergency_close_all(executor)
    _cache_invalidate()
    return {"ok": True, **result}


@app.get("/api/trading/live_status")
def api_live_status():
    """实盘连接状态 + 真实余额"""
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
    if settings.get("mode") != "live":
        return {"connected": False, "mode": "paper", "reason": "当前为模拟模式"}
    try:
        from executor import get_executor, BinanceLiveExecutor
        executor = get_executor("live")
        if isinstance(executor, BinanceLiveExecutor):
            balance = executor.get_account_balance()
            return {
                "connected": True,
                "mode": "live",
                "balance": balance.get("balance", 0),
                "available": balance.get("available", 0),
                "unrealized_pnl": balance.get("unrealized_pnl", 0),
            }
    except Exception as e:
        import sys
        print(f"[live-status] API 连接失败: {e}", file=sys.stderr, flush=True)
        return {"connected": False, "mode": "live", "reason": "API 连接失败，请检查配置"}
    return {"connected": False, "mode": "live", "reason": "未知错误"}


# === 前端页面 ===

HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Binance Square Monitor</title>
<style>
:root {
  --bg: #0f1419;
  --panel: #1a1f2e;
  --border: #2a3142;
  --text: #e6e8eb;
  --muted: #8b92a5;
  --accent: #f0b90b;
  --green: #52c41a;
  --red: #ff4d4f;
  --yellow: #faad14;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text);
}
h1, h2 { margin: 0 0 12px; }
h1 { font-size: 22px; color: var(--accent); }
h2 { font-size: 16px; color: var(--accent); margin-top: 24px; }
.updated { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); }
td { padding: 8px 10px; border-bottom: 1px solid #222836; }
tr:hover { background: #1f2536; }
.star { cursor: pointer; color: var(--muted); font-size: 18px; user-select: none; }
.star.active { color: var(--accent); }
.token { font-weight: bold; color: var(--accent); }
.token-link {
  cursor: pointer;
  border-bottom: 1px dashed transparent;
  transition: border-color 0.15s;
}
.token-link:hover {
  border-bottom-color: var(--accent);
  text-decoration: none;
}

/* 跳转后目标卡片高亮 */
@keyframes target-highlight {
  0%   { box-shadow: 0 0 0 3px var(--accent); background: rgba(240, 185, 11, 0.08); }
  100% { box-shadow: 0 0 0 0 transparent; background: transparent; }
}
.deep-card.target-focus {
  animation: target-highlight 2.5s ease-out;
}
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.muted { color: var(--muted); }
.right { text-align: right; }
.verdict { font-size: 12px; white-space: nowrap; }
.tag-list { font-size: 11px; color: var(--muted); margin-top: 4px; }
.notes { font-size: 12px; color: var(--muted); margin-top: 6px; padding-left: 16px; }
.notes li { margin-bottom: 3px; }
.disclaimer {
  background: #3a1f1f; border: 1px solid #5a2f2f; color: #ffb4b4;
  padding: 10px 14px; border-radius: 6px; font-size: 12px; margin-bottom: 16px;
}
.empty { color: var(--muted); font-style: italic; padding: 20px; text-align: center; }
.refresh-btn {
  background: var(--accent); color: #000; border: none;
  padding: 6px 14px; border-radius: 4px; cursor: pointer; font-weight: 500;
}
.refresh-btn:hover { opacity: 0.85; }
.refresh-btn:disabled { opacity: 0.5; cursor: wait; }
.refresh-btn.danger-btn {
  background: #c0392b; color: #fff;
}
.refresh-btn.danger-btn:hover { background: #e74c3c; }

/* 顶部进度条 */
.progress-bar {
  position: fixed; top: 0; left: 0; right: 0; height: 3px;
  background: transparent; z-index: 1000;
}
.progress-bar .fill {
  height: 100%; background: var(--accent);
  transition: width 0.5s linear;
}
.progress-bar.refreshing .fill {
  background: var(--green);
  animation: refreshing-pulse 0.8s ease-in-out infinite;
}
@keyframes refreshing-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* Toast 提示 */
.toast {
  position: fixed; top: 20px; right: 20px; z-index: 1001;
  background: var(--panel); border: 1px solid var(--accent);
  padding: 10px 16px; border-radius: 6px; font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  opacity: 0; transform: translateX(20px);
  transition: opacity 0.3s, transform 0.3s;
  pointer-events: none;
}
.toast.show { opacity: 1; transform: translateX(0); }
.toast.ok { border-color: var(--green); }
.toast.err { border-color: var(--red); }

/* 变化的行闪烁高亮 */
@keyframes row-flash {
  0%   { background: rgba(240, 185, 11, 0.3); }
  100% { background: transparent; }
}
tr.flash { animation: row-flash 1.5s ease-out; }
.deep-card.flash { animation: card-flash 1.5s ease-out; }
@keyframes card-flash {
  0%   { box-shadow: 0 0 0 2px var(--accent); }
  100% { box-shadow: 0 0 0 0 transparent; }
}

/* Worker 状态面板 */
.worker-panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 16px;
}
.worker-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  flex-wrap: wrap;
}
.worker-dot {
  width: 10px; height: 10px; border-radius: 50%;
  display: inline-block; background: var(--muted);
}
.worker-dot.running { background: var(--green); animation: pulse-dot 1.5s ease-in-out infinite; }
.worker-dot.stopped { background: var(--red); }
@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(82, 196, 26, 0.7); }
  50%      { box-shadow: 0 0 0 6px rgba(82, 196, 26, 0); }
}
.worker-title { font-size: 14px; font-weight: 500; }
.worker-stage-badge {
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
  background: #2a3142; color: var(--text);
}
.worker-stage-badge.scraping { background: #1e3a5f; color: #7eb3ff; }
.worker-stage-badge.saving   { background: #3a5f1e; color: #9eff7e; }
.worker-stage-badge.market   { background: #5f1e3a; color: #ff7eb3; }
.worker-stage-badge.idle     { background: #2a3142; color: var(--muted); }
.worker-detail { color: var(--text); font-size: 13px; margin-bottom: 8px; }
.worker-progress {
  height: 6px; background: #0a0e15; border-radius: 3px; overflow: hidden;
  margin-bottom: 8px;
}
.worker-progress-fill {
  height: 100%; background: linear-gradient(90deg, var(--accent), #52c41a);
  transition: width 0.5s ease;
}
.worker-stats {
  display: flex; gap: 16px; font-size: 12px; color: var(--muted); flex-wrap: wrap;
}
.worker-stats span strong { color: var(--text); }

/* 趋势箭头 */
.badge-new {
  background: #3a1f5f; color: #c4a0ff;
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
}

/* OI 背离小徽章（表格内）*/
.divergence-badge {
  display: inline-block;
  background: #2a3142; color: var(--text);
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
  cursor: help;
  border-left: 2px solid var(--accent);
}

/* OI 背离大横幅（深度解读卡片内）*/
.divergence-banner {
  display: flex; align-items: center; gap: 12px;
  background: #1e3a5f; border-left: 4px solid #7eb3ff;
  padding: 10px 14px; border-radius: 4px;
  margin: 10px 0;
}
.divergence-banner.oi_distribution {
  background: #3a2f1e; border-left-color: #ffcc7e;
}
.divergence-icon { font-size: 20px; }
.divergence-title { font-weight: bold; font-size: 13px; margin-bottom: 3px; }
.divergence-detail { font-size: 12px; color: var(--muted); }

/* 归档徽章 */
.archived-badge {
  display: inline-block; margin-left: 6px;
  background: #5a1f1f; color: #ffb4b4;
  padding: 1px 6px; border-radius: 3px; font-size: 10px;
}
.watch-info { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; }
.watch-info div { padding: 4px 8px; background: #141824; border-radius: 4px; }
/* 深度解读卡片 */
.deep-card {
  background: #141824; border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 16px; margin-bottom: 12px;
}
.deep-header {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 10px;
}
.deep-header .token-big { font-size: 18px; font-weight: bold; color: var(--accent); }
.deep-header .verdict-big { font-size: 14px; padding: 3px 10px; background: #0a0e15; border-radius: 4px; }
.deep-header .score-big { font-size: 16px; font-weight: bold; }
.deep-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 10px;
}
.metric { background: #0a0e15; padding: 8px 10px; border-radius: 4px; }
.metric .label { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.metric .value { font-size: 14px; font-weight: 500; }
.deep-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.tag-chip {
  background: #2a3142; color: var(--text); padding: 2px 8px;
  border-radius: 3px; font-size: 11px;
}
.deep-notes {
  background: #0a0e15; border-left: 3px solid var(--accent);
  padding: 10px 14px; font-size: 13px; line-height: 1.6;
}
.deep-notes ul { margin: 0; padding-left: 20px; }
.deep-notes li { margin-bottom: 4px; }
.deep-notes .no-notes { color: var(--muted); font-style: italic; }
.trade-controls {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 12px;
}
.trade-controls label { color: var(--muted); font-size: 11px; display: block; margin-bottom: 4px; }
.trade-controls input, .trade-controls select {
  width: 100%; background: #0a0e15; color: var(--text);
  border: 1px solid var(--border); border-radius: 4px; padding: 7px 8px;
}
.trade-summary {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px; margin: 12px 0;
}
.trade-summary .metric { min-height: 54px; }
.trade-position-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px; align-items: start; margin-top: 12px;
}
.trade-window {
  background: #0a0e15; border: 1px solid var(--border); border-radius: 6px;
  padding: 12px; min-width: 0;
}
.trade-window h3 {
  margin: 0 0 10px; color: var(--accent); font-size: 14px;
}
.closed-summary {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px; margin-bottom: 10px;
}
.closed-summary .metric {
  background: #111722; border: 1px solid var(--border); border-radius: 4px;
  padding: 8px; min-height: 48px;
}
.closed-positions-scroll {
  max-height: 260px;
  overflow-y: auto;
  border-top: 1px solid var(--border);
}
.closed-positions-scroll table { margin-top: 0; }
.closed-positions-scroll thead th {
  position: sticky; top: 0; z-index: 1;
  background: #0a0e15;
}
.compact-table { font-size: 12px; }
.compact-table th, .compact-table td { padding: 7px 6px; }
@media (max-width: 1100px) {
  .trade-position-grid { grid-template-columns: 1fr; }
}
.candidate-list { display: grid; gap: 8px; margin-top: 10px; }
.candidate-item {
  background: #0a0e15; border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; font-size: 12px;
}
.candidate-item.pass { border-left: 3px solid var(--green); }
.candidate-item.wait { border-left: 3px solid var(--yellow); }
</style>
</head>
<body>

<div class="progress-bar" id="progress-bar"><div class="fill" id="progress-fill" style="width:0%"></div></div>
<div class="toast" id="toast"></div>

<h1>🔥 币安广场热度监控</h1>
<div class="updated" id="updated">加载中...</div>

<div class="disclaimer">
⚠ 本页所有数据和标签仅为客观数据呈现，<strong>不是投资建议</strong>。综合分高 ≠ 一定上涨，
市场永远可能反向走，加密货币合约是高风险产品，请独立判断并谨慎决策。
</div>

<div class="worker-panel" id="worker-panel">
  <div class="worker-header">
    <span class="worker-dot" id="worker-dot"></span>
    <span class="worker-title">采集 Worker</span>
    <span class="worker-stage-badge" id="worker-stage">加载中</span>
    <span class="muted" style="font-size:12px; margin-left:auto;" id="worker-round">—</span>
  </div>
  <div class="worker-detail" id="worker-detail">等待状态...</div>
  <div class="worker-progress"><div class="worker-progress-fill" id="worker-progress-fill" style="width:0%"></div></div>
  <div class="worker-stats" id="worker-stats"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">自动交易面板</h2>
  <div class="muted" style="font-size:12px;margin-bottom:10px;">
    默认模拟交易。自动开仓规则：判断栏为看起来健康，15m 涨幅 0%-5%，1h 涨幅 0%-20%，OI 15m/1h/4h 都增加，主动买卖比 > 1.15，有可用价格后按市价开多。同一代币同一轮榜单只开一次。止损固定 -2%，止盈为 +1R 平 50%、+2R 平 30%、剩余跟踪。
  </div>
  <div class="trade-controls">
    <div>
      <label>自动交易</label>
      <select id="trade-enabled">
        <option value="false">关闭</option>
        <option value="true">开启</option>
      </select>
    </div>
    <div>
      <label>模式</label>
      <select id="trade-mode">
        <option value="paper">模拟</option>
        <option value="live">实盘（暂未启用）</option>
      </select>
    </div>
    <div>
      <label>账户初始金额 USDT</label>
      <input id="trade-initial" type="number" min="1" step="1">
    </div>
    <div>
      <label>交易倍数</label>
      <input id="trade-leverage" type="number" min="1" max="125" step="1">
    </div>
    <div>
      <label>开仓金额 USDT</label>
      <input id="trade-order-amount" type="number" min="1" step="1">
    </div>
    <div style="display:flex;align-items:end;gap:8px;">
      <button class="refresh-btn" onclick="saveTradingSettings()">保存交易设置</button>
      <button class="refresh-btn danger-btn" onclick="resetTradingAccount()"
              title="清空所有持仓和历史记录，把账户恢复到初始金额">重置账户</button>
    </div>
  </div>
  <div class="trade-summary" id="trade-summary"></div>
  <div class="trade-position-grid">
    <div class="trade-window">
      <h3>持仓代币</h3>
      <div id="trade-positions"><div class="empty">暂无持仓</div></div>
    </div>
    <div class="trade-window">
      <h3>已平仓代币</h3>
      <div id="trade-closed-positions"><div class="empty">暂无已平仓记录</div></div>
    </div>
  </div>
  <h2>合约扫描与操作建议</h2>
  <div id="trade-candidates"><div class="empty">等待扫描数据...</div></div>
  <h2>止损失败归档</h2>
  <div id="trade-loss-archive"><div class="empty">暂无止损样本</div></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">
    ⭐ 观察列表
    <button class="refresh-btn" onclick="refreshWatchlistMarket()">拉取最新合约数据</button>
    <button class="refresh-btn" style="background:#2a3142;color:var(--text);" onclick="manualRefresh()">重载页面</button>
  </h2>
  <div class="muted" style="font-size:12px;margin-bottom:10px;">
    收藏时自动锚定当前价格 · 每 5 分钟追踪浮盈浮亏 · 浮亏超过阈值自动归档为学习样本
  </div>
  <div id="watchlist"><div class="empty">暂无观察代币。去下方榜单点击 ⭐ 加入。</div></div>
  <div id="loss-samples-stats" class="loss-samples-stats muted" style="margin-top:12px;font-size:12px;"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">📊 15 分钟热度榜</h2>
  <table>
    <thead>
      <tr>
        <th width="50"></th>
        <th>代币</th>
        <th class="right">综合热度</th>
        <th>趋势</th>
        <th class="right">当前</th>
        <th class="right">帖子</th>
        <th class="right">价格</th>
        <th class="right">15m</th>
        <th class="right">1h</th>
        <th class="right">4h</th>
        <th class="right">费率/8h</th>
        <th class="right">OI 15m</th>
        <th class="right">OI 1h</th>
        <th class="right">OI 4h</th>
        <th class="right">综合</th>
        <th>判断</th>
        <th>走向</th>
        <th>OI 背离</th>
      </tr>
    </thead>
    <tbody id="leaderboard"></tbody>
  </table>
  <div id="leaderboard-note" class="muted" style="font-size:12px;margin-top:10px;"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">🔍 上榜代币合约深度解读</h2>
  <div class="muted" style="font-size:12px;margin-bottom:12px;">
    与榜单同序：看起来健康 → 值得留意 → 过热预警 → 信号偏弱 → 中性 → 数据不足。每个代币展开显示价格走势、合约持仓、多空结构，并给出基于数据的客观观察（不是投资建议）。
  </div>
  <div id="deep-analysis"><div class="empty">等待榜单数据...</div></div>
</div>

<script>
// === 工具：toast 提示 ===
function showToast(msg, kind = 'ok', duration = 2500) {
  const el = document.getElementById('toast');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), duration);
}

// === 进度条：显示下次自动刷新倒计时 ===
const REFRESH_INTERVAL_MS = 30000;
let lastRefreshAt = Date.now();
function tickProgress() {
  const bar = document.getElementById('progress-bar');
  if (bar.classList.contains('refreshing')) return;
  const elapsed = Date.now() - lastRefreshAt;
  const pct = Math.min(100, (elapsed / REFRESH_INTERVAL_MS) * 100);
  document.getElementById('progress-fill').style.width = pct + '%';
}
setInterval(tickProgress, 500);

// === 上一轮快照（用于 diff 出变化，做闪烁动画）===
let prevLeaderboard = {};  // token -> {score, price, in_watchlist}

function buildSnapshotMap(items) {
  const m = {};
  items.forEach(it => {
    m[it.token] = {
      score: it.score,
      price: (it.market && it.market.snapshot && it.market.snapshot.mark_price) || null,
      inWatch: it.in_watchlist,
      verdictScore: (it.market && it.market.analysis && it.market.analysis.score) || null,
    };
  });
  return m;
}

function diffTokens(oldMap, newMap) {
  const changed = new Set();
  const added = new Set();
  Object.keys(newMap).forEach(t => {
    if (!oldMap[t]) {
      added.add(t);
      return;
    }
    const a = oldMap[t], b = newMap[t];
    if (a.score !== b.score || a.price !== b.price || a.verdictScore !== b.verdictScore) {
      changed.add(t);
    }
  });
  return { added, changed };
}

function flashRows(tokens) {
  tokens.forEach(t => {
    document.querySelectorAll(`tr[data-token="${t}"]`).forEach(row => {
      row.classList.remove('flash');
      // 触发 reflow 让动画重新跑
      void row.offsetWidth;
      row.classList.add('flash');
    });
    document.querySelectorAll(`.deep-card[data-token="${t}"]`).forEach(el => {
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    });
  });
}

const fmtPct = (v, invert=false) => {
  if (v === null || v === undefined) return '<span class="muted">-</span>';
  const good = invert ? v < 0 : v > 0;
  const cls = good ? 'green' : (v === 0 ? 'muted' : 'red');
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
};
const fmtFR = (v) => {
  if (v === null || v === undefined) return '<span class="muted">-</span>';
  let cls = '';
  if (v >= 0.05) cls = 'red';
  else if (v <= -0.01) cls = 'yellow';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(3)}%</span>`;
};
const fmtPrice = (v) => v ? v.toPrecision(5) : '-';
const fmtNum = (v, d = 2) => v !== null && v !== undefined ? v.toFixed(d) : '-';

function fmtDirection(d) {
  if (!d) return '<span class="muted">-</span>';
  if (d.indexOf('偏多') >= 0 || d.indexOf('↑') >= 0) return `<span class="green">${d}</span>`;
  if (d.indexOf('偏空') >= 0 || d.indexOf('↓') >= 0) return `<span class="red">${d}</span>`;
  if (d === '震荡') return `<span class="yellow">${d}</span>`;
  return `<span class="muted">${d}</span>`;
}

function fmtTrend(t) {
  if (!t || t === '—') return `<span class="muted">${t || '—'}</span>`;
  if (t === '🆕') return `<span class="badge-new">🆕 新</span>`;
  if (t.indexOf('↑') >= 0) return `<span class="green" style="font-weight:bold;">${t}</span>`;
  if (t.indexOf('↓') >= 0) return `<span class="red" style="font-weight:bold;">${t}</span>`;
  return t;
}

function fmtDivergence(div) {
  if (!div) return '<span class="muted">-</span>';
  const icon = div.type === 'oi_accumulation' ? '🟢' : '🟡';
  return `<span class="divergence-badge" title="${div.note}">${icon} ${div.oi_pct > 0 ? '+' : ''}${div.oi_pct}% / ${div.price_pct > 0 ? '+' : ''}${div.price_pct}%</span>`;
}

function rowFromMarket(item) {
  const m = item.market;
  if (!m) {
    return {
      price: '<span class="muted">无合约</span>',
      ch15m: '-', ch1h: '-', ch4h: '-',
      fr: '-', oi: '-', lsr: '-',
      score: '-', verdict: '<span class="muted">-</span>',
      direction: '<span class="muted">-</span>',
      divergence: '<span class="muted">-</span>',
      divergenceData: null,
      notes: null, tags: null,
    };
  }
  const s = m.snapshot || {};
  const a = m.analysis || {};
  return {
    price: fmtPrice(s.mark_price),
    ch15m: fmtPct(s.change_15m_pct),
    ch1h: fmtPct(s.change_1h_pct),
    ch4h: fmtPct(s.change_4h_pct),
    ch48h: fmtPct(s.change_48h_pct),
    fr: fmtFR(s.funding_rate_pct),
    oi15m: fmtPct(s.oi_change_15m_pct),
    oi: fmtPct(s.oi_change_1h_pct),
    oi4h: fmtPct(s.oi_change_4h_pct),
    oi48: fmtPct(s.oi_change_48h_pct),
    taker: fmtNum(s.taker_buy_sell_ratio),
    spread: fmtPct(s.bid_ask_spread_pct),
    lsr: fmtNum(s.long_short_ratio),
    score: a.score !== undefined ? a.score : '-',
    verdict: a.verdict || '<span class="muted">-</span>',
    direction: fmtDirection(a.direction),
    divergence: fmtDivergence(a.oi_divergence),
    divergenceData: a.oi_divergence || null,
    notes: a.notes || [],
    tags: a.tags || [],
  };
}

function renderDeepAnalysis(items) {
  const el = document.getElementById('deep-analysis');
  if (!items || !items.length) {
    el.innerHTML = '<div class="empty">等待榜单数据...</div>';
    return;
  }
  // 直接沿用榜单的顺序（API 已按 verdict 档位 → 综合分 → 热度排序）
  el.innerHTML = items.map(item => {
    const m = item.market || {};
    const s = m.snapshot || {};
    const a = m.analysis || {};

    const fmtFR2 = (v) => {
      if (v === null || v === undefined) return '-';
      const sign = v > 0 ? '+' : '';
      return sign + v.toFixed(3) + '%';
    };
    const fmtPct2 = (v) => {
      if (v === null || v === undefined) return '-';
      const sign = v > 0 ? '+' : '';
      return sign + v.toFixed(2) + '%';
    };
    const fmtUsd = (v) => {
      if (!v) return '-';
      if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
      if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
      if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
      return '$' + v.toFixed(2);
    };
    const tagsHtml = (a.tags || []).map(t => `<span class="tag-chip">${t}</span>`).join('');
    const notesHtml = (a.notes && a.notes.length)
      ? `<ul>${a.notes.map(n => `<li>${n}</li>`).join('')}</ul>`
      : '<div class="no-notes">（暂无需特别提示的数据特征）</div>';

    return `
      <div class="deep-card" id="card-${item.token}" data-token="${item.token}">
        <div class="deep-header">
          <span class="token-big">${item.token}</span>
          <span class="verdict-big">${a.verdict || '-'}</span>
          <span class="verdict-big">${fmtDirection(a.direction)}</span>
          <span class="score-big">综合 ${a.score !== undefined ? a.score : '-'}</span>
          <span class="muted" style="font-size:12px;">社交热度 ${item.score.toFixed(1)} · ${item.unique_posts} 条帖子</span>
          <span style="margin-left:auto;" class="muted" style="font-size:11px;">
            更新于 ${m.updated_at || '-'}
          </span>
        </div>
        <div class="deep-metrics">
          <div class="metric"><div class="label">标记价</div><div class="value">${s.mark_price ? s.mark_price.toPrecision(5) : '-'}</div></div>
          <div class="metric"><div class="label">15m 涨跌</div><div class="value">${fmtPct2(s.change_15m_pct)}</div></div>
          <div class="metric"><div class="label">1h 涨跌</div><div class="value">${fmtPct2(s.change_1h_pct)}</div></div>
          <div class="metric"><div class="label">4h 涨跌</div><div class="value">${fmtPct2(s.change_4h_pct)}</div></div>
          <div class="metric"><div class="label">24h 涨跌</div><div class="value">${fmtPct2(s.change_24h_pct)}</div></div>
          <div class="metric"><div class="label">资金费率/8h</div><div class="value">${fmtFR2(s.funding_rate_pct)}</div></div>
          <div class="metric"><div class="label">未平仓(USD)</div><div class="value">${fmtUsd(s.oi_usd)}</div></div>
          <div class="metric"><div class="label">OI 15m 变化</div><div class="value">${fmtPct2(s.oi_change_15m_pct)}</div></div>
          <div class="metric"><div class="label">OI 1h 变化</div><div class="value">${fmtPct2(s.oi_change_1h_pct)}</div></div>
          <div class="metric"><div class="label">OI 4h 变化</div><div class="value">${fmtPct2(s.oi_change_4h_pct)}</div></div>
          <div class="metric"><div class="label">OI 48h 变化</div><div class="value">${fmtPct2(s.oi_change_48h_pct)}</div></div>
          <div class="metric"><div class="label">48h 涨跌</div><div class="value">${fmtPct2(s.change_48h_pct)}</div></div>
          <div class="metric"><div class="label">主动买/卖比</div><div class="value">${s.taker_buy_sell_ratio ? s.taker_buy_sell_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">盘口价差</div><div class="value">${fmtPct2(s.bid_ask_spread_pct)}</div></div>
          <div class="metric"><div class="label">1% 买盘深度</div><div class="value">${fmtUsd(s.depth_bid_1pct_usd)}</div></div>
          <div class="metric"><div class="label">1% 卖盘深度</div><div class="value">${fmtUsd(s.depth_ask_1pct_usd)}</div></div>
          <div class="metric"><div class="label">多空比(散户)</div><div class="value">${s.long_short_ratio ? s.long_short_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">多空比(大户)</div><div class="value">${s.top_trader_ls_ratio ? s.top_trader_ls_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">24h 成交额</div><div class="value">${fmtUsd(s.volume_24h_usd)}</div></div>
        </div>
        ${a.oi_divergence ? `
          <div class="divergence-banner ${a.oi_divergence.type}">
            <span class="divergence-icon">${a.oi_divergence.type === 'oi_accumulation' ? '🟢' : '🟡'}</span>
            <div>
              <div class="divergence-title">OI 背离 · ${a.oi_divergence.direction}</div>
              <div class="divergence-detail">${a.oi_divergence.note}</div>
            </div>
          </div>
        ` : ''}
        ${tagsHtml ? `<div class="deep-tags">${tagsHtml}</div>` : ''}
        <div class="deep-notes">
          <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">数据观察（非投资建议）</div>
          ${notesHtml}
        </div>
      </div>
    `;
  }).join('');
}

async function loadWatchlist() {
  const resp = await fetch('/api/watchlist');
  const data = await resp.json();
  const el = document.getElementById('watchlist');
  if (!data.items.length) {
    el.innerHTML = '<div class="empty">暂无观察代币。去下方榜单点击 ⭐ 加入。</div>';
    return;
  }
  el.innerHTML = '<table><thead><tr>' +
    '<th width="50"></th>' +
    '<th>代币</th>' +
    '<th class="right">锚定价</th>' +
    '<th class="right">当前价</th>' +
    '<th class="right">浮盈/亏</th>' +
    '<th class="right">峰值/回撤</th>' +
    '<th class="right">15m</th>' +
    '<th class="right">1h</th>' +
    '<th class="right">4h</th>' +
    '<th class="right">费率/8h</th>' +
    '<th class="right">OI 1h</th>' +
    '<th>判断</th>' +
    '<th>走向</th>' +
    '<th>OI 背离</th>' +
    '</tr></thead><tbody>' +
    data.items.map(item => {
      const m = rowFromMarket(item);
      const notesHtml = m.notes && m.notes.length
        ? `<ul class="notes">${m.notes.map(n => `<li>${n}</li>`).join('')}</ul>` : '';
      const anchorDisp = item.anchor_price ? fmtPrice(item.anchor_price) : '<span class="muted">-</span>';
      const curDisp = item.current_price ? fmtPrice(item.current_price) : '<span class="muted">-</span>';
      const pnlDisp = item.pnl_pct !== null && item.pnl_pct !== undefined
        ? fmtPct(item.pnl_pct)
        : '<span class="muted">-</span>';
      const peakDisp = (item.peak_profit !== null && item.peak_profit !== undefined)
        ? `<span class="green">+${item.peak_profit.toFixed(1)}%</span> / <span class="red">${item.max_drawdown.toFixed(1)}%</span>`
        : '<span class="muted">-</span>';
      const archivedBadge = item.archived
        ? '<span class="archived-badge" title="已触发负面样本归档">已归档</span>' : '';
      return `
        <tr data-token="${item.token}">
          <td><span class="star active" onclick="toggleWatch('${item.token}', true)" title="移除">★</span></td>
          <td>
            <span class="token token-link" onclick="jumpToCard('${item.token}')">${item.token}</span>
            ${archivedBadge}
            ${notesHtml}
          </td>
          <td class="right">${anchorDisp}</td>
          <td class="right">${curDisp}</td>
          <td class="right"><strong>${pnlDisp}</strong></td>
          <td class="right">${peakDisp}</td>
          <td class="right">${m.ch15m}</td>
          <td class="right">${m.ch1h}</td>
          <td class="right">${m.ch4h}</td>
          <td class="right">${m.fr}</td>
          <td class="right">${m.oi}</td>
          <td class="verdict">${m.verdict}</td>
          <td>${m.direction}</td>
          <td>${m.divergence}</td>
        </tr>
      `;
    }).join('') + '</tbody></table>';
}

// === 点击代币名跳转到对应的深度解读卡片 ===
function jumpToCard(token) {
  const card = document.getElementById('card-' + token);
  if (!card) {
    showToast(`未找到 ${token} 的解读卡片`, 'err', 1500);
    return;
  }
  // 平滑滚动到卡片顶部上方 20px
  const top = card.getBoundingClientRect().top + window.pageYOffset - 20;
  window.scrollTo({ top, behavior: 'smooth' });
  // 触发高亮动画
  card.classList.remove('target-focus');
  void card.offsetWidth;  // 强制重新计算以重启动画
  card.classList.add('target-focus');
}

async function toggleWatch(token, currentlyActive) {
  const url = currentlyActive ? '/api/watchlist/remove' : '/api/watchlist/add';
  // 乐观 UI：先改界面上的星星状态，给用户即时反馈
  document.querySelectorAll(`tr[data-token="${token}"] .star`).forEach(s => {
    s.classList.toggle('active', !currentlyActive);
    s.setAttribute('onclick', `toggleWatch('${token}', ${!currentlyActive})`);
  });
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    const trade = data.trade || {};
    if (currentlyActive) {
      if (trade.closed || trade.canceled) {
        showToast(`已移除 ${token}，模拟平仓 ${trade.closed || 0} 笔`, 'ok');
      } else {
        showToast(`已移除 ${token}：${trade.reason || '没有可平仓位'}`, 'ok');
      }
    } else {
      if (trade.ok) {
        showToast(`已收藏 ${token}，模拟开仓价 ${fmtPrice(trade.entry_price)}`, 'ok');
      } else {
        showToast(`已收藏 ${token}：${trade.reason || '未开仓'}`, 'ok');
      }
    }
  } catch (e) {
    showToast('操作失败：' + e.message, 'err');
  }
  await refreshAll();
}

async function refreshAll(opts = {}) {
  const { silent = false, manual = false } = opts;
  const bar = document.getElementById('progress-bar');
  const btns = document.querySelectorAll('.refresh-btn');
  bar.classList.add('refreshing');
  document.getElementById('progress-fill').style.width = '100%';
  btns.forEach(b => b.disabled = true);

  try {
    const [lb, _, __] = await Promise.all([
      fetch('/api/leaderboard').then(r => r.json()),
      loadWatchlist(),
      loadLossSamples(),
    ]);

    // 渲染榜单前，先算出哪些代币变化了
    const newMap = buildSnapshotMap(lb.items || []);
    const diff = diffTokens(prevLeaderboard, newMap);

    renderLeaderboard(lb);
    await loadTradingPanel();

    // 触发闪烁（仅对已出现过、这次数值有变的 token）
    const toFlash = new Set([...diff.changed]);
    if (toFlash.size) flashRows(toFlash);

    // toast 提示
    if (manual) {
      showToast('已刷新', 'ok');
    } else if (!silent && Object.keys(prevLeaderboard).length) {
      const addedCount = diff.added.size;
      const changedCount = diff.changed.size;
      if (addedCount || changedCount) {
        const parts = [];
        if (addedCount) parts.push(`${addedCount} 个新上榜`);
        if (changedCount) parts.push(`${changedCount} 个数据更新`);
        showToast(parts.join('，'), 'ok', 2000);
      }
    }

    prevLeaderboard = newMap;
    lastRefreshAt = Date.now();
  } catch (e) {
    showToast('刷新失败：' + e.message, 'err');
  } finally {
    setTimeout(() => {
      bar.classList.remove('refreshing');
      document.getElementById('progress-fill').style.width = '0%';
    }, 400);
    btns.forEach(b => b.disabled = false);
  }
}

// 把 loadLeaderboard 拆成两步：fetch 由 refreshAll 做，渲染单独提出来
function renderLeaderboard(data) {
  document.getElementById('updated').textContent = '最后刷新: ' + data.updated_at;
  const tbody = document.getElementById('leaderboard');
  const noteEl = document.getElementById('leaderboard-note');
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="18" class="empty">榜单数据为空。worker 还没抓到，或榜单代币都没有永续合约。等下一轮...</td></tr>';
    noteEl.textContent = '';
    renderDeepAnalysis([]);
    return;
  }
  if (data.skipped_no_contract) {
    noteEl.textContent = `已过滤 ${data.skipped_no_contract} 个无永续合约的代币。`;
  } else {
    noteEl.textContent = '';
  }
  tbody.innerHTML = data.items.map(item => {
    const m = rowFromMarket(item);
    const starCls = item.in_watchlist ? 'star active' : 'star';
    const watchFlag = item.in_watchlist ? 'true' : 'false';
    return `
      <tr data-token="${item.token}">
        <td><span class="${starCls}" onclick="toggleWatch('${item.token}', ${watchFlag})">★</span></td>
        <td><span class="token token-link" onclick="jumpToCard('${item.token}')">${item.token}</span></td>
        <td class="right"><strong>${item.composite_score.toFixed(1)}</strong></td>
        <td>${fmtTrend(item.trend)}</td>
        <td class="right">${item.score.toFixed(1)}</td>
        <td class="right">${item.unique_posts}</td>
        <td class="right">${m.price}</td>
        <td class="right">${m.ch15m}</td>
        <td class="right">${m.ch1h}</td>
        <td class="right">${m.ch4h}</td>
        <td class="right">${m.fr}</td>
        <td class="right">${m.oi15m}</td>
        <td class="right">${m.oi}</td>
        <td class="right">${m.oi4h}</td>
        <td class="right"><strong>${m.score}</strong></td>
        <td class="verdict">${m.verdict}</td>
        <td>${m.direction}</td>
        <td>${m.divergence}</td>
      </tr>
    `;
  }).join('');
  renderDeepAnalysis(data.items);
}

// 刷新按钮走 manual 分支，提示不同
function manualRefresh() {
  refreshAll({ manual: true });
}

// === 观察列表同步拉取合约数据（后端会直接调币安 API）===
async function refreshWatchlistMarket() {
  const btns = document.querySelectorAll('.refresh-btn');
  btns.forEach(b => b.disabled = true);
  showToast('正在拉取最新合约数据...', 'ok', 1500);
  try {
    const resp = await fetch('/api/watchlist/refresh', { method: 'POST' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (!data.tokens || !data.tokens.length) {
      showToast('观察列表为空', 'ok');
    } else {
      showToast(`已刷新 ${data.refreshed} 个代币`
        + (data.skipped_no_contract ? `（${data.skipped_no_contract} 个无合约）` : ''),
        'ok', 3000);
      // 触发观察列表涉及代币闪烁
      flashRows(new Set(data.tokens));
    }
    await refreshAll({ silent: true });
  } catch (e) {
    showToast('刷新失败：' + e.message, 'err');
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}

// === 负面样本统计 ===
async function loadLossSamples() {
  try {
    const resp = await fetch('/api/loss_samples');
    const s = await resp.json();
    const el = document.getElementById('loss-samples-stats');
    if (!el) return;
    if (!s.count) {
      el.innerHTML = '📚 尚无已归档的学习样本（浮亏超过 ' +
        '<span style="color:var(--text);">10%</span> 的收藏会自动归档供参考）';
      return;
    }
    // 把 verdict 分布格式化
    const vd = s.anchor_verdict_distribution || {};
    const vdParts = Object.entries(vd).map(([k, v]) => `${k}: ${v}`).join(' · ');
    el.innerHTML = `📚 已累积 <strong style="color:var(--text);">${s.count}</strong> 个负面样本` +
      ` · 平均浮亏 <span class="red">${s.avg_drawdown_pct}%</span>` +
      (vdParts ? ` · 入场判断分布: ${vdParts}` : '');
  } catch (e) {
    // 静默
  }
}

// === 自动交易面板 ===
const fmtUsdGlobal = (v) => {
  if (v === null || v === undefined || isNaN(Number(v))) return '-';
  return '$' + Number(v).toFixed(2);
};

function renderTradingPanel(data) {
  const acc = data.account || {};
  const settings = acc.settings || {};
  const active = document.activeElement;
  const editingSettings = active && active.closest && active.closest('.trade-controls');
  if (!editingSettings) {
    document.getElementById('trade-enabled').value = settings.enabled ? 'true' : 'false';
    document.getElementById('trade-mode').value = settings.mode || 'paper';
    document.getElementById('trade-initial').value = settings.initial_balance ?? '';
    document.getElementById('trade-leverage').value = settings.leverage ?? '';
    document.getElementById('trade-order-amount').value = settings.order_amount ?? '';
  }

  document.getElementById('trade-summary').innerHTML = `
    <div class="metric"><div class="label">初始金额</div><div class="value">${fmtUsdGlobal(acc.initial_balance)}</div></div>
    <div class="metric"><div class="label">账户权益</div><div class="value">${fmtUsdGlobal(acc.equity)}</div></div>
    <div class="metric"><div class="label">剩余金额</div><div class="value">${fmtUsdGlobal(acc.available_balance)}</div></div>
    <div class="metric"><div class="label">占用保证金</div><div class="value">${fmtUsdGlobal(acc.locked_margin)}</div></div>
    <div class="metric"><div class="label">已实现盈亏</div><div class="value">${fmtUsdGlobal(acc.realized_pnl)}</div></div>
    <div class="metric"><div class="label">浮动盈亏</div><div class="value">${fmtUsdGlobal(acc.unrealized_pnl)}</div></div>
  `;

  renderTradePositions(data.positions || []);
  renderTradeCandidates(data.candidates || []);
  renderTradeLossArchive(data.loss_archive || {});
}

function renderTradePositions(items) {
  const activeStatuses = new Set(['PENDING', 'OPEN', 'PARTIAL']);
  const activeItems = items.filter(p => activeStatuses.has(p.status));
  const closedItems = items.filter(p => !activeStatuses.has(p.status));
  renderOpenPositions(activeItems);
  renderClosedPositions(closedItems);
}

function renderOpenPositions(items) {
  const el = document.getElementById('trade-positions');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无持仓</div>';
    return;
  }
  el.innerHTML = '<table class="compact-table"><thead><tr>' +
    '<th>代币</th><th>状态</th><th class="right">倍数</th><th class="right">金额</th>' +
    '<th class="right">入场</th><th class="right">现价</th><th class="right">止损</th>' +
    '<th class="right">盈亏</th><th>操作建议</th>' +
    '</tr></thead><tbody>' +
    items.map(p => {
      const pnl = Number(p.pnl_pct || 0);
      const pnlCls = pnl >= 0 ? 'green' : 'red';
      return `<tr data-token="${p.token}">
        <td class="token">${p.token}</td>
        <td>${p.status}</td>
        <td class="right">${Number(p.leverage || 0).toFixed(0)}x</td>
        <td class="right">${fmtUsdGlobal(p.margin_amount)}</td>
        <td class="right">${fmtPrice(p.entry_price || p.limit_price)}</td>
        <td class="right">${fmtPrice(p.current_price)}</td>
        <td class="right">${fmtPrice(p.stop_loss_price)}</td>
        <td class="right ${pnlCls}">${pnl.toFixed(2)}%</td>
        <td>${p.advice || '-'}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderClosedPositions(items) {
  const el = document.getElementById('trade-closed-positions');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无已平仓记录</div>';
    return;
  }
  const closed = items.filter(p => p.status === 'CLOSED');
  const totalPnl = closed.reduce((sum, p) => sum + Number(p.realized_pnl || 0), 0);
  const wins = closed.filter(p => Number(p.realized_pnl || 0) > 0).length;
  const losses = closed.filter(p => Number(p.realized_pnl || 0) < 0).length;
  const winRate = closed.length ? (wins / closed.length * 100) : 0;
  const totalCls = totalPnl >= 0 ? 'green' : 'red';

  el.innerHTML = `
    <div class="closed-summary">
      <div class="metric"><div class="label">已平仓</div><div class="value">${closed.length}</div></div>
      <div class="metric"><div class="label">总盈亏</div><div class="value ${totalCls}">${fmtUsdGlobal(totalPnl)}</div></div>
      <div class="metric"><div class="label">胜率</div><div class="value">${winRate.toFixed(1)}%</div></div>
    </div>
    <div class="closed-positions-scroll">
      <table class="compact-table"><thead><tr>
        <th>代币</th><th>状态</th><th class="right">入场</th><th class="right">平仓价</th>
        <th class="right">盈亏</th><th class="right">盈亏率</th><th>操作建议</th>
      </tr></thead><tbody>
        ${items.map(p => {
          const realized = Number(p.realized_pnl || 0);
          let pnl = Number(p.pnl_pct || 0);
          if (!pnl && realized && Number(p.margin_amount || 0)) {
            pnl = realized / Number(p.margin_amount) * 100;
          }
          const pnlCls = realized >= 0 ? 'green' : 'red';
          return `<tr data-token="${p.token}">
            <td class="token">${p.token}</td>
            <td>${p.status}</td>
            <td class="right">${fmtPrice(p.entry_price || p.limit_price)}</td>
            <td class="right">${fmtPrice(p.current_price)}</td>
            <td class="right ${pnlCls}">${fmtUsdGlobal(realized)}</td>
            <td class="right ${pnlCls}">${pnl.toFixed(2)}%</td>
            <td>${p.advice || '-'}</td>
          </tr>`;
        }).join('')}
      </tbody></table>
    </div>
  `;
}

function renderTradeCandidates(items) {
  const el = document.getElementById('trade-candidates');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无符合自动开仓要求的代币</div>';
    return;
  }
  el.innerHTML = '<div class="candidate-list">' + items.map(c => {
    const cls = c.passed ? 'pass' : 'wait';
    const action = c.has_active_position ? '已有持仓/挂单' : c.suggestion;
    const reasons = (c.reasons || []).slice(0, 6).join(' · ');
    return `<div class="candidate-item ${cls}">
      <div><span class="token">${c.token}</span> #${c.rank} · ${action} · 市价 ${fmtPrice(c.price)}</div>
      <div class="muted" style="margin-top:5px;">${reasons}</div>
    </div>`;
  }).join('') + '</div>';
}

function renderTradeLossArchive(archive) {
  const el = document.getElementById('trade-loss-archive');
  if (!archive.count) {
    el.innerHTML = '<div class="empty">暂无止损样本</div>';
    return;
  }
  const tags = Object.entries(archive.tag_counts || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="tag-chip">${k}: ${v}</span>`)
    .join('');
  const recent = (archive.recent || []).slice(0, 5).map(r => {
    const pnl = Number(r.pnl_pct || 0);
    return `<tr>
      <td class="token">${r.token}</td>
      <td>${r.failed_reason || '-'}</td>
      <td class="right red">${pnl.toFixed(2)}%</td>
      <td>${r.reason_tags || '[]'}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <div class="deep-tags">${tags}</div>
    <table><thead><tr><th>代币</th><th>失败原因</th><th class="right">亏损</th><th>标签</th></tr></thead>
    <tbody>${recent}</tbody></table>
  `;
}

async function loadTradingPanel() {
  try {
    const resp = await fetch('/api/trading');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    renderTradingPanel(await resp.json());
  } catch (e) {
    document.getElementById('trade-summary').innerHTML =
      `<div class="empty">交易面板加载失败：${e.message}</div>`;
  }
}

async function saveTradingSettings() {
  const body = {
    enabled: document.getElementById('trade-enabled').value === 'true',
    mode: document.getElementById('trade-mode').value,
    initial_balance: Number(document.getElementById('trade-initial').value),
    leverage: Number(document.getElementById('trade-leverage').value),
    order_amount: Number(document.getElementById('trade-order-amount').value),
  };
  try {
    const resp = await fetch('/api/trading/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    showToast('交易设置已保存', 'ok');
    await loadTradingPanel();
  } catch (e) {
    showToast('保存交易设置失败：' + e.message, 'err');
  }
}

async function resetTradingAccount() {
  // 拿到当前初始金额作为默认值
  const initialInput = document.getElementById('trade-initial');
  const currentInitial = Number(initialInput.value) || 1000;

  // 第一次确认：告知后果（全 ASCII 文本防编码问题）
  const confirm1 = window.confirm(
    '[警告] 重置账户将会清空:\\n\\n' +
    '  - 所有持仓 (含挂单和已平仓历史)\\n' +
    '  - 已实现盈亏 / 浮动盈亏\\n' +
    '  - 占用保证金\\n' +
    '  - 止损学习归档\\n' +
    '  - signal_lock 去重表\\n\\n' +
    '配置 (倍数/开仓金额/自动交易开关) 会保留。\\n\\n' +
    '此操作不可撤销！确定继续吗？'
  );
  if (!confirm1) return;

  // 第二次确认：让用户输入初始金额（顺便当作二次确认）
  const newBalance = window.prompt(
    '请输入重置后的账户初始金额 USDT (回车保持 ' + currentInitial + '):',
    String(currentInitial)
  );
  if (newBalance === null) return;  // 用户点了取消
  const parsed = Number(newBalance);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    showToast('金额必须为正数', 'err');
    return;
  }

  try {
    const resp = await fetch('/api/trading/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        confirm: true,
        new_initial_balance: parsed,
      }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    showToast(
      '账户已重置：清除 ' + data.positions_deleted + ' 条持仓 / ' +
      data.locks_deleted + ' 条锁 / ' + data.loss_archive_deleted + ' 条归档',
      'ok'
    );
    await loadTradingPanel();
    await refreshAll({ silent: true });
  } catch (e) {
    showToast('重置失败：' + e.message, 'err');
  }
}

// === Worker 状态轮询 ===
async function pollWorkerStatus() {
  try {
    const resp = await fetch('/api/status');
    const s = await resp.json();
    renderWorkerPanel(s);
  } catch (e) {
    renderWorkerPanel({ stage: 'unknown', detail: '状态接口不可用', running: false });
  }
}

function renderWorkerPanel(s) {
  const dot = document.getElementById('worker-dot');
  const stageEl = document.getElementById('worker-stage');
  const detailEl = document.getElementById('worker-detail');
  const fillEl = document.getElementById('worker-progress-fill');
  const statsEl = document.getElementById('worker-stats');
  const roundEl = document.getElementById('worker-round');

  // 圆点状态
  dot.className = 'worker-dot';
  if (s.running) dot.classList.add('running');
  else if (s.stage !== 'unknown') dot.classList.add('stopped');

  // 阶段徽章
  const stage = s.stage || 'unknown';
  stageEl.className = 'worker-stage-badge ' + stage;
  const stageLabels = {
    scraping: '抓取中', saving: '入库中', market: '查询合约',
    idle: '空闲（准备下一轮）', unknown: '未知',
  };
  stageEl.textContent = stageLabels[stage] || stage;

  detailEl.textContent = s.detail || '—';

  // 进度条：抓取阶段按 round_start 算，其他阶段满格
  let pct = 100;
  if (stage === 'scraping' && s.round_start && s.round_duration_seconds) {
    try {
      const startMs = new Date(s.round_start).getTime();
      const elapsed = (Date.now() - startMs) / 1000;
      pct = Math.min(100, (elapsed / s.round_duration_seconds) * 100);
    } catch (e) { pct = 50; }
  } else if (stage === 'idle') {
    pct = 100;
  } else if (stage === 'saving') {
    pct = 100;
  }
  fillEl.style.width = pct.toFixed(0) + '%';

  // 轮次
  if (s.round_number) {
    roundEl.textContent = `第 ${s.round_number} 轮`
      + (s.heartbeat_age_seconds !== undefined ? ` · ${s.heartbeat_age_seconds}s 前更新` : '');
  } else {
    roundEl.textContent = '—';
  }

  // 统计
  const stats = [];
  if (s.posts_this_round !== undefined)
    stats.push(`<span>本轮抓到 <strong>${s.posts_this_round}</strong> 条</span>`);
  if (s.saved_this_round !== undefined)
    stats.push(`<span>入库 <strong>${s.saved_this_round}</strong> 条</span>`);
  if (s.total_posts !== undefined)
    stats.push(`<span>累计帖子 <strong>${s.total_posts}</strong></span>`);
  if (s.total_authors !== undefined)
    stats.push(`<span>累计作者 <strong>${s.total_authors}</strong></span>`);
  statsEl.innerHTML = stats.join('');
}

let watchlistPollBusy = false;
async function pollWatchlistRealtime() {
  if (watchlistPollBusy) return;
  watchlistPollBusy = true;
  try {
    await loadWatchlist();
  } finally {
    watchlistPollBusy = false;
  }
}

refreshAll({ silent: true });
pollWorkerStatus();
setInterval(pollWatchlistRealtime, 1000);
setInterval(loadTradingPanel, 3000);
setInterval(() => refreshAll(), 30000);
setInterval(pollWorkerStatus, 2000);  // worker 状态高频刷新
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        content=HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


if __name__ == "__main__":
    storage.init_db()  # 保证表存在，即使 worker 没先跑
    print(f"=> Web 仪表盘启动：http://{config.WEB_HOST}:{config.WEB_PORT}")
    print(f"=> 记得另开一个终端运行 python worker.py 采数据")
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")
