"""实盘仓位管理循环。

替代 trade_logic.update_paper_positions() 用于 live 模式。
每 2 秒由 auto_trader 调用，通过轮询交易所订单状态来管理仓位。

核心职责：
  1. 检测止损单/止盈单是否已成交
  2. TP1 成交后：移止损到保本 + 挂 TP2
  3. TP2 成交后：开始追踪止盈
  4. 定期对账：DB 状态 vs 交易所实际持仓
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import config
import storage
from executor import BinanceLiveExecutor
from market import get_mark_price

_last_reconcile_at = 0.0
_last_trailing_update: dict[int, float] = {}  # pos_id → timestamp
_last_stop_repair_at: dict[int, float] = {}   # pos_id → timestamp
_last_guardian_at = 0.0
_last_guard_repair_at: dict[tuple[int, str], float] = {}  # (pos_id, kind) -> timestamp
_last_status_unknown_log_at: dict[tuple[int, str], float] = {}  # (pos_id, kind) -> timestamp
_last_order_query_error_at: dict[tuple[str, str], float] = {}  # (symbol, order_id) -> timestamp
_stale_tp_unknown_counts: dict[tuple[int, str, str], int] = {}  # (pos_id, kind, order_id) -> count


def _log(msg: str):
    print(f"[live-mgr] {msg}", file=sys.stderr, flush=True)


def _rate_limited_log(cache: dict[tuple, float], key: tuple, msg: str, interval_s: float = 30.0):
    now = time.time()
    if now - cache.get(key, 0) < max(interval_s, 0.0):
        return
    cache[key] = now
    _log(msg)


def _log_status_unknown_retry(pos_id: int, kind: str, token: str):
    interval_s = float(getattr(config, "LIVE_STATUS_QUERY_RETRY_LOG_INTERVAL_S", 30) or 30)
    _rate_limited_log(
        _last_status_unknown_log_at,
        (int(pos_id), str(kind)),
        f"{token} {kind} 状态查询失败，下一轮重试",
        interval_s=interval_s,
    )


def _log_order_query_error(symbol: str, order_id: str, stage: str, err: Exception):
    interval_s = float(getattr(config, "LIVE_STATUS_QUERY_ERROR_LOG_INTERVAL_S", 30) or 30)
    _rate_limited_log(
        _last_order_query_error_at,
        (str(symbol).upper(), str(order_id)),
        f"{symbol} 订单状态查询失败({stage}) order_id={order_id}: {err}",
        interval_s=interval_s,
    )


def _order_is_filled(orders: list[dict], order_id: str) -> bool:
    """检查某个 order_id 是否不在活跃委托列表中（即已成交或已取消）"""
    if not order_id:
        return False
    oid = int(order_id)
    for o in orders:
        if o.get("orderId") == oid:
            return False  # 还在活跃列表 → 未成交
    return True  # 不在列表 → 已成交或已取消


def _get_order_status(executor: BinanceLiveExecutor, symbol: str, order_id: str) -> str:
    """查询订单状态：FILLED / CANCELED / NEW / EXPIRED 等"""
    if not order_id:
        return "UNKNOWN"
    symbol = (symbol or "").upper()
    oid = int(order_id)

    if executor.client.use_unified_account:
        try:
            resp = executor.client.get_conditional_order_status(symbol, oid)
            status = str(resp.get("status", "UNKNOWN") or "UNKNOWN").upper()
            if status != "UNKNOWN":
                return status
        except Exception as e:
            _log_order_query_error(symbol, str(order_id), "conditional", e)

        # 兜底：有些 unified 账号场景下条件单状态查询会返回空，回退到普通订单查单。
        try:
            resp = executor.client.get_order(symbol, oid)
            status = str(resp.get("status", "UNKNOWN") or "UNKNOWN").upper()
            if status != "UNKNOWN":
                return status
        except Exception as e:
            _log_order_query_error(symbol, str(order_id), "order_fallback", e)
        return "UNKNOWN"

    try:
        resp = executor.client.get_order(symbol, oid)
        return str(resp.get("status", "UNKNOWN") or "UNKNOWN").upper()
    except Exception as e:
        _log_order_query_error(symbol, str(order_id), "order", e)
        return "UNKNOWN"


def _order_exists_in_open_orders(open_orders: list[dict], order_id: str) -> bool:
    oid = str(order_id or "").strip()
    if not oid:
        return False
    for row in open_orders or []:
        rid = str(row.get("orderId") or "").strip()
        if rid and rid == oid:
            return True
    return False


def _clear_stale_tp_order_id_if_needed(
    pos: dict,
    kind: str,
    order_id: str,
    status: str,
    open_orders: list[dict],
) -> bool:
    """清理长期 UNKNOWN 且已不在活跃委托列表中的 TP 订单 ID。"""
    kind = (kind or "").lower()
    if kind not in {"tp1", "tp2"}:
        return False
    pos_id = int(pos.get("id") or 0)
    oid = str(order_id or "").strip()
    if pos_id <= 0 or not oid:
        return False

    # 状态恢复或仍在活跃委托中时，重置计数，避免误清理。
    if (status or "").upper() != "UNKNOWN" or _order_exists_in_open_orders(open_orders, oid):
        _stale_tp_unknown_counts.pop((pos_id, kind, oid), None)
        return False

    key = (pos_id, kind, oid)
    count = _stale_tp_unknown_counts.get(key, 0) + 1
    _stale_tp_unknown_counts[key] = count
    threshold = int(getattr(config, "LIVE_STALE_ORDER_UNKNOWN_THRESHOLD", 5) or 5)
    if count < max(1, threshold):
        return False

    field = "exchange_tp1_order_id" if kind == "tp1" else "exchange_tp2_order_id"
    token = pos.get("token") or pos.get("symbol") or "UNKNOWN"
    symbol = pos.get("symbol") or ""
    with storage.get_conn() as conn:
        storage.trade_position_update(conn, pos_id, {
            field: "",
            "advice": f"{kind.upper()} 订单号疑似失效，已自动清理等待补挂",
        })
    _log(
        f"{token} {kind.upper()} 订单号疑似失效并已清理: "
        f"symbol={symbol}, order_id={oid}, unknown_count={count}"
    )
    # 清掉同仓位同类型的历史计数，避免旧 key 残留。
    for k in list(_stale_tp_unknown_counts.keys()):
        if k[0] == pos_id and k[1] == kind:
            _stale_tp_unknown_counts.pop(k, None)
    return True


def update_live_positions(executor: BinanceLiveExecutor):
    """主管理循环入口，由 auto_trader 每 2 秒调用。"""
    with storage.get_conn() as conn:
        positions = storage.trade_live_open_positions(conn)

    if not positions:
        _maybe_reconcile(executor)
        return

    for pos in positions:
        try:
            _manage_one_position(pos, executor)
        except Exception as e:
            _log(f"管理仓位 {pos.get('token')} 出错: {e}")

    _maybe_guard_unprotected_orders(executor)
    _maybe_reconcile(executor)


def _guard_repair_allowed(pos_id: int, kind: str) -> bool:
    now = time.time()
    min_interval = getattr(config, "LIVE_GUARDIAN_REPAIR_MIN_INTERVAL_S", 120)
    key = (pos_id, kind)
    if now - _last_guard_repair_at.get(key, 0) < min_interval:
        return False
    _last_guard_repair_at[key] = now
    return True


def _maybe_guard_unprotected_orders(executor: BinanceLiveExecutor):
    global _last_guardian_at
    if not getattr(config, "LIVE_GUARDIAN_ENABLED", True):
        return

    now = time.time()
    interval = getattr(config, "LIVE_GUARDIAN_INTERVAL_S", 600)
    if now - _last_guardian_at < interval:
        return
    _last_guardian_at = now

    try:
        with storage.get_conn() as conn:
            positions = storage.trade_live_open_positions(conn)
    except Exception as e:
        _log(f"守护巡检读取仓位失败: {e}")
        return

    repaired = 0
    for pos in positions:
        try:
            repaired += _repair_missing_protection_orders(pos, executor)
        except Exception as e:
            _log(f"守护巡检处理 {pos.get('token')} 失败: {e}")

    if repaired > 0:
        _log(f"守护巡检完成：本轮补挂 {repaired} 个保护单")


def _repair_missing_protection_orders(
    pos: dict,
    executor: BinanceLiveExecutor,
    use_guard_interval: bool = True,
) -> int:
    pos_id = int(pos.get("id") or 0)
    if pos_id <= 0:
        return 0

    symbol = pos.get("symbol") or ""
    token = pos.get("token") or symbol
    status = pos.get("status") or "OPEN"
    side = (pos.get("side") or "LONG").upper()
    qty = float(pos.get("quantity") or 0)
    closed_qty = float(pos.get("closed_qty") or 0)
    open_qty = max(qty - closed_qty, 0)
    if not symbol or open_qty <= 0:
        return 0

    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
    stop_price = float(pos.get("stop_loss_price") or 0)
    tp1_price = float(pos.get("tp1_price") or 0)
    tp2_price = float(pos.get("tp2_price") or 0)
    stop_oid = pos.get("exchange_stop_order_id") or ""
    tp1_oid = pos.get("exchange_tp1_order_id") or ""
    tp2_oid = pos.get("exchange_tp2_order_id") or ""

    tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
    tp2_pct = config.TRADING_TP2_CLOSE_PCT / 100
    closed_ratio = closed_qty / qty if qty > 0 else 0
    tp1_done = closed_ratio >= tp1_pct - 1e-6
    tp2_done = closed_ratio >= (tp1_pct + tp2_pct) - 1e-6

    repaired = 0
    fields = {}
    notes = []
    allow_repair = (lambda kind: _guard_repair_allowed(pos_id, kind)) if use_guard_interval else (lambda kind: True)

    target_stop_price = stop_price
    trailing = float(pos.get("trailing_stop_price") or 0)
    if tp2_done:
        if trailing <= 0:
            extreme = float(pos.get("highest_price") or 0) or entry
            if extreme > 0:
                if side == "SHORT":
                    trailing = extreme * (1 + config.TRADING_TRAIL_CALLBACK_PCT / 100)
                else:
                    trailing = extreme * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100)
                if trailing > 0:
                    fields["trailing_stop_price"] = trailing
                    notes.append(f"补齐 trailing 止损价 @ ${trailing:.6g}")
        if trailing > 0:
            target_stop_price = trailing
            if stop_price <= 0 or abs(stop_price - trailing) / max(abs(trailing), 1e-9) > 1e-4:
                fields["stop_loss_price"] = trailing
                notes.append(f"止损同步至 trailing @ ${trailing:.6g}")

    if not stop_oid and target_stop_price > 0 and allow_repair("stop"):
        result = executor.update_stop_loss(symbol, "", target_stop_price, open_qty, side=side)
        if result.success and result.order_id:
            fields["exchange_stop_order_id"] = result.order_id
            repaired += 1
            notes.append(f"止损已补挂 @ ${target_stop_price:.6g}")
        else:
            _log(f"{token} 守护补挂止损失败: {result.error}")

    tp1_valid = (tp1_price > entry) if side == "LONG" else (tp1_price < entry)
    if (not tp1_done) and (not tp1_oid) and tp1_valid and allow_repair("tp1"):
        tp1_qty = min(qty * tp1_pct, open_qty)
        tp1_result = executor.place_take_profit(symbol, tp1_price, tp1_qty, side=side)
        if tp1_result.success and tp1_result.order_id:
            fields["exchange_tp1_order_id"] = tp1_result.order_id
            repaired += 1
            notes.append(f"TP1 已补挂 @ ${tp1_price:.6g}")
        else:
            _log(f"{token} 守护补挂 TP1 失败: {tp1_result.error}")

    tp2_valid = (tp2_price > entry) if side == "LONG" else (tp2_price < entry)
    if status == "PARTIAL" and tp1_done and (not tp2_done) and (not tp2_oid) and tp2_valid and allow_repair("tp2"):
        tp2_qty = min(qty * tp2_pct, open_qty)
        tp2_result = executor.place_take_profit(symbol, tp2_price, tp2_qty, side=side)
        if tp2_result.success and tp2_result.order_id:
            fields["exchange_tp2_order_id"] = tp2_result.order_id
            repaired += 1
            notes.append(f"TP2 已补挂 @ ${tp2_price:.6g}")
        else:
            _log(f"{token} 守护补挂 TP2 失败: {tp2_result.error}")

    if fields:
        fields["advice"] = "；".join(notes)
        with storage.get_conn() as conn:
            storage.trade_position_update(conn, pos_id, fields)

    return repaired


def _manage_one_position(pos: dict, executor: BinanceLiveExecutor):
    """管理单个实盘仓位"""
    symbol = pos["symbol"]
    pos_id = pos["id"]
    status = pos["status"]
    side = (pos.get("side") or "LONG").upper()

    # 获取该 symbol 所有活跃委托
    open_orders = executor.get_open_orders(symbol)

    stop_oid = pos.get("exchange_stop_order_id") or ""
    tp1_oid = pos.get("exchange_tp1_order_id") or ""
    tp2_oid = pos.get("exchange_tp2_order_id") or ""

    qty = float(pos.get("quantity") or 0)
    closed_qty = float(pos.get("closed_qty") or 0)
    open_qty = max(qty - closed_qty, 0)
    realized = float(pos.get("realized_pnl") or 0)
    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)

    if entry <= 0 or open_qty <= 0:
        return

    # 开仓后若止损因为网络抖动没挂上，持续补挂，避免靠紧急平仓消耗手续费。
    if not stop_oid:
        _repair_missing_stop(pos, executor, open_qty)

    tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
    tp2_pct = config.TRADING_TP2_CLOSE_PCT / 100
    closed_ratio = closed_qty / qty if qty > 0 else 0
    tp1_done = closed_ratio >= tp1_pct - 1e-6
    tp2_done = closed_ratio >= (tp1_pct + tp2_pct) - 1e-6

    # --- 检测止损单是否已成交 ---
    if stop_oid and _order_is_filled(open_orders, stop_oid):
        stop_status = _get_order_status(executor, symbol, stop_oid)
        if stop_status == "FILLED":
            _handle_stop_loss_filled(pos, executor)
            return
        elif stop_status in ("CANCELED", "EXPIRED"):
            # 止损单被取消了但仓位还在 → 重新挂止损
            _log(f"{pos['token']} 止损单 {stop_status}，重新挂止损")
            stop_price = float(pos.get("stop_loss_price") or 0)
            if stop_price > 0:
                result = executor.update_stop_loss(symbol, "", stop_price, open_qty, side=side)
                if result.success:
                    with storage.get_conn() as conn:
                        storage.trade_position_update_exchange_stop(
                            conn, pos_id, result.order_id)
        elif stop_status == "UNKNOWN":
            _log_status_unknown_retry(pos_id, "止损单", pos["token"])

    # --- 检测 TP1 是否已成交 ---
    if not tp1_done and tp1_oid and _order_is_filled(open_orders, tp1_oid):
        tp1_status = _get_order_status(executor, symbol, tp1_oid)
        if tp1_status == "FILLED":
            _handle_tp1_filled(pos, executor, open_orders)
            return
        elif tp1_status == "UNKNOWN":
            _log_status_unknown_retry(pos_id, "TP1", pos["token"])
            if _clear_stale_tp_order_id_if_needed(pos, "tp1", tp1_oid, tp1_status, open_orders):
                pos["exchange_tp1_order_id"] = ""
                _repair_missing_protection_orders(pos, executor, use_guard_interval=False)

    # --- 检测 TP2 是否已成交 ---
    if tp1_done and not tp2_done and tp2_oid and _order_is_filled(open_orders, tp2_oid):
        tp2_status = _get_order_status(executor, symbol, tp2_oid)
        if tp2_status == "FILLED":
            _handle_tp2_filled(pos, executor, open_orders)
            return
        elif tp2_status == "UNKNOWN":
            _log_status_unknown_retry(pos_id, "TP2", pos["token"])
            if _clear_stale_tp_order_id_if_needed(pos, "tp2", tp2_oid, tp2_status, open_orders):
                pos["exchange_tp2_order_id"] = ""
                _repair_missing_protection_orders(pos, executor, use_guard_interval=False)

    # --- 追踪止盈 ---
    if tp2_done and open_qty > 0:
        _update_trailing_stop(pos, executor, open_orders)

    # --- 更新当前价格和未实现盈亏 ---
    _update_price_and_pnl(pos, executor)


def _repair_missing_stop(pos: dict, executor: BinanceLiveExecutor, open_qty: float):
    if not getattr(config, "LIVE_AUTO_REPAIR_MISSING_STOP", False):
        return

    pos_id = pos["id"]
    now = time.time()
    min_interval = getattr(config, "LIVE_STOP_REPAIR_MIN_INTERVAL_S", 10)
    if now - _last_stop_repair_at.get(pos_id, 0) < min_interval:
        return
    _last_stop_repair_at[pos_id] = now

    if open_qty <= 0:
        return

    # 快速补挂模式：触发时不仅补止损，也同步检查 TP1 / TP2 / trailing 止损状态。
    repaired = _repair_missing_protection_orders(pos, executor, use_guard_interval=False)
    if repaired > 0:
        _log(f"{pos['token']} 快速补挂保护单完成，本轮修复 {repaired} 项")


def _handle_stop_loss_filled(pos: dict, executor: BinanceLiveExecutor):
    """止损已成交 → 关闭仓位"""
    symbol = pos["symbol"]
    stop_oid = pos.get("exchange_stop_order_id", "")
    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
    qty = float(pos.get("quantity") or 0)
    closed_qty = float(pos.get("closed_qty") or 0)
    open_qty = max(qty - closed_qty, 0)
    realized = float(pos.get("realized_pnl") or 0)
    side = (pos.get("side") or "LONG").upper()

    # 查询止损单的实际成交价
    exit_price = float(pos.get("stop_loss_price") or 0)
    try:
        if executor.client.use_unified_account:
            order_info = executor.client.get_conditional_order_status(symbol, int(stop_oid))
        else:
            order_info = executor.client.get_order(symbol, int(stop_oid))
        exit_price = float(order_info.get("avgPrice", exit_price))
    except Exception:
        pass

    if side == "SHORT":
        realized += (entry - exit_price) * open_qty
    else:
        realized += (exit_price - entry) * open_qty

    # 撤掉其他活跃委托（TP1/TP2）
    for oid in [pos.get("exchange_tp1_order_id"), pos.get("exchange_tp2_order_id")]:
        if oid:
            executor.cancel_order_safe(symbol, oid)

    with storage.get_conn() as conn:
        margin = float(pos.get("margin_amount") or 1)
        storage.trade_position_update(conn, pos["id"], {
            "status": "CLOSED",
            "current_price": exit_price,
            "closed_qty": qty,
            "realized_pnl": realized,
            "unrealized_pnl": 0,
            "pnl_pct": (realized / margin * 100) if margin else 0,
            "advice": f"实盘止损触发 @ ${exit_price:.6g}",
            "closed_at": "__CURRENT_TIMESTAMP__",
        })
        # 归档到 loss_archive
        _archive_live_stop(conn, pos, exit_price, realized)

    _last_trailing_update.pop(pos["id"], None)
    _log(f"{pos['token']} 止损成交 @ {exit_price:.6g}, PnL={realized:.2f}")


def _handle_tp1_filled(pos: dict, executor: BinanceLiveExecutor, open_orders: list):
    """TP1 成交 → 移止损到保本 + 挂 TP2"""
    symbol = pos["symbol"]
    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
    qty = float(pos.get("quantity") or 0)
    tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
    tp1_price = float(pos.get("tp1_price") or 0)
    side = (pos.get("side") or "LONG").upper()

    # 查询实际成交价
    tp1_oid = pos.get("exchange_tp1_order_id", "")
    if tp1_oid:
        try:
            if executor.client.use_unified_account:
                order_info = executor.client.get_conditional_order_status(symbol, int(tp1_oid))
            else:
                order_info = executor.client.get_order(symbol, int(tp1_oid))
            actual_fill = float(order_info.get("avgPrice", 0))
            if actual_fill > 0:
                tp1_price = actual_fill
        except Exception:
            pass

    close_qty = qty * tp1_pct
    realized = float(pos.get("realized_pnl") or 0)
    realized += (entry - tp1_price) * close_qty if side == "SHORT" else (tp1_price - entry) * close_qty
    closed_qty = float(pos.get("closed_qty") or 0) + close_qty
    open_qty = max(qty - closed_qty, 0)

    fields = {
        "status": "PARTIAL",
        "closed_qty": closed_qty,
        "realized_pnl": realized,
        "stop_loss_price": entry,  # 保本
        "advice": f"+{config.TRADING_TP1_R}R TP1 成交，止损移到保本",
    }

    # 撤旧止损，挂保本止损
    old_stop_oid = pos.get("exchange_stop_order_id", "")
    if open_qty > 0:
        result = executor.update_stop_loss(symbol, old_stop_oid, entry, open_qty, side=side)
        if result.success:
            fields["exchange_stop_order_id"] = result.order_id

        # 挂 TP2
        tp2_price = float(pos.get("tp2_price") or 0)
        if (tp2_price > entry and side == "LONG") or (tp2_price < entry and side == "SHORT"):
            tp2_qty = qty * (config.TRADING_TP2_CLOSE_PCT / 100)
            tp2_qty = min(tp2_qty, open_qty)
            tp2_result = executor.place_take_profit(symbol, tp2_price, tp2_qty, side=side)
            if tp2_result.success:
                fields["exchange_tp2_order_id"] = tp2_result.order_id

    with storage.get_conn() as conn:
        storage.trade_position_update(conn, pos["id"], fields)

    _log(f"{pos['token']} TP1 成交 @ {tp1_price:.6g}, 已移止损到保本")


def _handle_tp2_filled(pos: dict, executor: BinanceLiveExecutor, open_orders: list):
    """TP2 成交 → 开始追踪止盈"""
    symbol = pos["symbol"]
    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
    qty = float(pos.get("quantity") or 0)
    tp2_pct = config.TRADING_TP2_CLOSE_PCT / 100
    tp2_price = float(pos.get("tp2_price") or 0)
    side = (pos.get("side") or "LONG").upper()

    # 查询实际成交价
    tp2_oid = pos.get("exchange_tp2_order_id", "")
    if tp2_oid:
        try:
            if executor.client.use_unified_account:
                order_info = executor.client.get_conditional_order_status(symbol, int(tp2_oid))
            else:
                order_info = executor.client.get_order(symbol, int(tp2_oid))
            actual_fill = float(order_info.get("avgPrice", 0))
            if actual_fill > 0:
                tp2_price = actual_fill
        except Exception:
            pass

    close_qty = qty * tp2_pct
    realized = float(pos.get("realized_pnl") or 0)
    realized += (entry - tp2_price) * close_qty if side == "SHORT" else (tp2_price - entry) * close_qty
    closed_qty = float(pos.get("closed_qty") or 0) + close_qty
    open_qty = max(qty - closed_qty, 0)

    if side == "SHORT":
        highest = min(float(pos.get("highest_price") or entry), tp2_price)
        trailing = highest * (1 + config.TRADING_TRAIL_CALLBACK_PCT / 100)
    else:
        highest = max(float(pos.get("highest_price") or entry), tp2_price)
        trailing = highest * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100)

    fields = {
        "status": "PARTIAL",
        "closed_qty": closed_qty,
        "realized_pnl": realized,
        "highest_price": highest,
        "trailing_stop_price": trailing,
        "advice": f"+{config.TRADING_TP2_R}R TP2 成交，剩余追踪止盈",
    }

    # 撤旧止损，挂追踪止损
    old_stop_oid = pos.get("exchange_stop_order_id", "")
    if open_qty > 0 and trailing > 0:
        result = executor.update_stop_loss(symbol, old_stop_oid, trailing, open_qty, side=side)
        if result.success:
            fields["exchange_stop_order_id"] = result.order_id

    with storage.get_conn() as conn:
        storage.trade_position_update(conn, pos["id"], fields)

    _log(f"{pos['token']} TP2 成交 @ {tp2_price:.6g}, 开始追踪止盈")


def _update_trailing_stop(pos: dict, executor: BinanceLiveExecutor, open_orders: list):
    """追踪止盈：价格创新高时更新止损单"""
    pos_id = pos["id"]
    symbol = pos["symbol"]
    side = (pos.get("side") or "LONG").upper()

    # 节流：防止过于频繁更新
    min_interval = getattr(config, "LIVE_TRAILING_STOP_MIN_UPDATE_S", 30)
    now = time.time()
    last_update = _last_trailing_update.get(pos_id, 0)
    if now - last_update < min_interval:
        return

    # 获取当前价格
    from market import get_mark_price
    current_price = get_mark_price(pos["token"])
    if not current_price:
        return

    old_highest = float(pos.get("highest_price") or 0)
    if side == "SHORT":
        highest = current_price if old_highest <= 0 else min(old_highest, current_price)
        if old_highest > 0 and highest >= old_highest:
            return
        new_trailing = highest * (1 + config.TRADING_TRAIL_CALLBACK_PCT / 100)
    else:
        highest = max(old_highest, current_price)
        if highest <= old_highest:
            return  # 没有创新高
        new_trailing = highest * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100)
    old_trailing = float(pos.get("trailing_stop_price") or 0)

    # 改善幅度检查
    min_improvement = getattr(config, "LIVE_TRAILING_STOP_MIN_IMPROVEMENT_PCT", 0.3)
    if old_trailing > 0:
        if side == "SHORT":
            improvement = (old_trailing - new_trailing) / old_trailing * 100
            if improvement < min_improvement:
                return
        else:
            improvement = (new_trailing - old_trailing) / old_trailing * 100
            if improvement < min_improvement:
                return

    # 更新交易所止损单
    qty = float(pos.get("quantity") or 0)
    closed_qty = float(pos.get("closed_qty") or 0)
    open_qty = max(qty - closed_qty, 0)
    if open_qty <= 0:
        return

    old_stop_oid = pos.get("exchange_stop_order_id", "")
    result = executor.update_stop_loss(symbol, old_stop_oid, new_trailing, open_qty, side=side)
    if result.success:
        _last_trailing_update[pos_id] = now
        with storage.get_conn() as conn:
            storage.trade_position_update(conn, pos_id, {
                "highest_price": highest,
                "trailing_stop_price": new_trailing,
                "exchange_stop_order_id": result.order_id,
            })
        _log(f"{pos['token']} 追踪止盈更新: trailing={new_trailing:.6g}")


def _update_price_and_pnl(pos: dict, executor: BinanceLiveExecutor):
    """更新当前价格和未实现盈亏"""
    from market import get_mark_price
    price = get_mark_price(pos["token"])
    if not price:
        return

    entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
    qty = float(pos.get("quantity") or 0)
    closed_qty = float(pos.get("closed_qty") or 0)
    open_qty = max(qty - closed_qty, 0)
    realized = float(pos.get("realized_pnl") or 0)
    margin = float(pos.get("margin_amount") or 1)
    side = (pos.get("side") or "LONG").upper()

    if side == "SHORT":
        unrealized = (entry - price) * open_qty
        highest = min(float(pos.get("highest_price") or entry), price)
    else:
        unrealized = (price - entry) * open_qty
        highest = max(float(pos.get("highest_price") or entry), price)

    with storage.get_conn() as conn:
        storage.trade_position_update(conn, pos["id"], {
            "current_price": price,
            "highest_price": highest,
            "unrealized_pnl": unrealized,
            "pnl_pct": ((realized + unrealized) / margin * 100) if margin else 0,
        })


def _archive_live_stop(conn, pos: dict, exit_price: float, realized: float):
    """实盘止损归档"""
    storage.trade_loss_archive_add(conn, {
        "position_id": pos.get("id"),
        "token": pos.get("token"),
        "symbol": pos.get("symbol"),
        "entry_price": pos.get("actual_entry_price") or pos.get("entry_price"),
        "exit_price": exit_price,
        "realized_pnl": realized,
        "pnl_pct": (realized / float(pos.get("margin_amount") or 1)) * 100,
        "failed_reason": "live stop loss hit",
        "reason_tags": json.dumps(["live_stop"], ensure_ascii=False),
        "entry_snapshot": pos.get("signal_snapshot"),
        "exit_snapshot": json.dumps({
            "exit_price": exit_price,
            "mode": "live",
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, default=str, ensure_ascii=False),
    })


def _maybe_reconcile(executor: BinanceLiveExecutor):
    """定期对账：DB 仓位 vs 交易所实际持仓"""
    global _last_reconcile_at
    interval = getattr(config, "LIVE_RECONCILE_INTERVAL_S", 60)
    now = time.time()
    if now - _last_reconcile_at < interval:
        return
    _last_reconcile_at = now

    try:
        _do_reconcile(executor)
    except Exception as e:
        _log(f"对账出错: {e}")


def _do_reconcile(executor: BinanceLiveExecutor):
    """执行对账"""
    with storage.get_conn() as conn:
        db_positions = storage.trade_live_open_positions(conn)

    if not db_positions:
        return

    for pos in db_positions:
        symbol = pos["symbol"]
        try:
            exchange_positions = executor.client.get_position_risk(symbol)
        except Exception:
            continue

        # 在交易所查找对应持仓
        exchange_qty = 0.0
        for ep in exchange_positions:
            if ep.get("symbol") == symbol:
                exchange_qty = abs(float(ep.get("positionAmt", 0)))
                break

        db_open_qty = max(float(pos.get("quantity", 0)) - float(pos.get("closed_qty", 0)), 0)

        # 交易所已无持仓但 DB 显示有 → 可能止损在离线时成交
        if exchange_qty == 0 and db_open_qty > 0:
            _log(f"对账发现 {pos['token']} 交易所无持仓，DB 还有 {db_open_qty}，标记关闭并回填盈亏")
            exit_price, advice = _resolve_reconcile_exit_price(executor, pos, symbol)
            entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
            realized = float(pos.get("realized_pnl") or 0)
            if exit_price and entry > 0:
                realized += (float(exit_price) - entry) * db_open_qty
            current_price = float(exit_price) if exit_price else float(pos.get("current_price") or 0)
            with storage.get_conn() as conn:
                margin = float(pos.get("margin_amount") or 1)
                storage.trade_position_update(conn, pos["id"], {
                    "status": "CLOSED",
                    "current_price": current_price,
                    "closed_qty": float(pos.get("quantity", 0)),
                    "realized_pnl": realized,
                    "unrealized_pnl": 0,
                    "pnl_pct": (realized / margin * 100) if margin else 0,
                    "advice": advice,
                    "closed_at": "__CURRENT_TIMESTAMP__",
                })

    _cleanup_orphan_orders(executor, db_positions)


def _cleanup_orphan_orders(executor: BinanceLiveExecutor, db_positions: list[dict]):
    """清理孤儿委托：DB 无活跃仓位且交易所也无持仓的 symbol，撤掉其残留委托。"""
    db_symbols = {
        (p.get("symbol") or "").upper()
        for p in (db_positions or [])
        if (p.get("symbol") or "").strip()
    }
    try:
        open_orders = executor.get_open_orders("")
    except Exception as e:
        _log(f"孤儿委托清理：读取活跃委托失败: {e}")
        return

    if not open_orders:
        return

    orphan_symbols = sorted({
        str(o.get("symbol") or "").upper()
        for o in open_orders
        if str(o.get("symbol") or "").upper() and str(o.get("symbol") or "").upper() not in db_symbols
    })
    if not orphan_symbols:
        return

    cleaned = 0
    skipped_with_pos = 0
    for symbol in orphan_symbols:
        try:
            position_rows = executor.client.get_position_risk(symbol)
            has_exchange_position = any(
                abs(float(r.get("positionAmt") or 0)) > 1e-12
                for r in (position_rows or [])
                if str(r.get("symbol") or "").upper() == symbol
            )
        except Exception as e:
            _log(f"孤儿委托清理：读取持仓失败 {symbol}: {e}")
            continue

        if has_exchange_position:
            skipped_with_pos += 1
            continue

        try:
            executor.client.cancel_all_orders(symbol)
            # 统一账户下 allOpenOrders 可能不覆盖 algo 条件单；
            # 再按剩余活跃单逐单兜底撤单，避免日志显示已清理但交易所仍残留。
            try:
                remaining = executor.get_open_orders(symbol)
            except Exception:
                remaining = []
            for row in remaining:
                oid = str(row.get("orderId") or "").strip()
                if not oid:
                    continue
                try:
                    executor.cancel_order_safe(symbol, oid)
                except Exception as e:
                    _log(f"孤儿委托清理：逐单撤单失败 {symbol} #{oid}: {e}")
            try:
                verify_left = executor.get_open_orders(symbol)
            except Exception:
                verify_left = []
            if verify_left:
                _log(f"孤儿委托清理：{symbol} 仍有 {len(verify_left)} 笔活跃委托（可能权限/网络抖动）")
            else:
                cleaned += 1
                _log(f"孤儿委托清理：已撤销 {symbol} 的残留委托")
        except Exception as e:
            _log(f"孤儿委托清理：撤单失败 {symbol}: {e}")

    if cleaned > 0:
        _log(f"孤儿委托清理完成：撤销 {cleaned} 个 symbol 的残留委托")
    elif skipped_with_pos > 0:
        _log(f"孤儿委托清理跳过 {skipped_with_pos} 个 symbol（交易所仍有持仓）")


def _resolve_reconcile_exit_price(
    executor: BinanceLiveExecutor,
    pos: dict,
    symbol: str,
) -> tuple[float | None, str]:
    """Resolve best-effort exit price when reconcile finds position gone on exchange."""
    order_candidates = [
        ("止损", pos.get("exchange_stop_order_id")),
        ("TP2", pos.get("exchange_tp2_order_id")),
        ("TP1", pos.get("exchange_tp1_order_id")),
    ]
    for label, oid in order_candidates:
        if not oid:
            continue
        try:
            if executor.client.use_unified_account:
                info = executor.client.get_conditional_order_status(symbol, int(oid))
            else:
                info = executor.client.get_order(symbol, int(oid))
            status = str(info.get("status") or "").upper()
            avg_price = float(info.get("avgPrice") or 0)
            if status == "FILLED" and avg_price > 0:
                return avg_price, f"对账关闭：{label}单已成交（离线补记）"
        except Exception:
            continue

    mark = get_mark_price(pos.get("token") or "")
    if mark and mark > 0:
        return float(mark), "对账关闭：交易所已无持仓（按当前标记价估算）"

    cached = float(pos.get("current_price") or 0)
    if cached > 0:
        return cached, "对账关闭：交易所已无持仓（按最后缓存价估算）"

    return None, "对账关闭：交易所已无持仓（缺少成交价，PnL 未重算）"


def emergency_close_all(executor: BinanceLiveExecutor) -> dict:
    """紧急平仓：撤掉所有委托 + 市价全平"""
    results = {"closed": 0, "canceled_orders": 0, "errors": []}

    with storage.get_conn() as conn:
        positions = storage.trade_live_open_positions(conn)

    for pos in positions:
        symbol = pos["symbol"]
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)

        # 撤掉所有委托
        try:
            executor.client.cancel_all_orders(symbol)
            results["canceled_orders"] += 1
        except Exception as e:
            results["errors"].append(f"{symbol} 撤单失败: {e}")

        # 市价平仓
        if open_qty > 0:
            side = (pos.get("side") or "LONG").upper()
            close_result = executor.close_position(symbol, open_qty, side=side, reason="emergency")
            if close_result.success:
                exit_price = close_result.fill_price or 0
                entry = float(pos.get("actual_entry_price") or pos.get("entry_price") or 0)
                realized = float(pos.get("realized_pnl") or 0)
                if exit_price and entry:
                    realized += (entry - exit_price) * open_qty if side == "SHORT" else (exit_price - entry) * open_qty
                margin = float(pos.get("margin_amount") or 1)

                with storage.get_conn() as conn:
                    storage.trade_position_update(conn, pos["id"], {
                        "status": "CLOSED",
                        "current_price": exit_price,
                        "closed_qty": qty,
                        "realized_pnl": realized,
                        "unrealized_pnl": 0,
                        "pnl_pct": (realized / margin * 100) if margin else 0,
                        "advice": "紧急平仓",
                        "closed_at": "__CURRENT_TIMESTAMP__",
                    })
                results["closed"] += 1
            else:
                results["errors"].append(f"{symbol} 平仓失败: {close_result.error}")

    _log(f"紧急平仓完成: {results}")
    return results
