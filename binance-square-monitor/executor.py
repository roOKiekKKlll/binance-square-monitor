"""订单执行抽象层：paper / live 共用同一套交易决策逻辑。

架构：
  OrderExecutor (抽象)
    ├─ PaperExecutor   — 模拟成交，直接写 DB（现有行为）
    └─ BinanceLiveExecutor — 真实下单到币安合约
"""
from __future__ import annotations

import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import URLError

import config
from binance_client import BinanceFuturesClient, BinanceAPIError
from market import get_mark_price


def _log(msg: str):
    print(f"[executor] {msg}", file=sys.stderr, flush=True)


# === 数据结构 ===

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    status: str = ""
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class StopPlacementResult:
    order_id: str | None = None
    trigger_price: float | None = None
    transient_failure: bool = False
    error: str = ""


# === 抽象基类 ===

class OrderExecutor(ABC):

    @abstractmethod
    def open_long(self, symbol: str, quantity: float, entry_price: float,
                  stop_loss_price: float, tp1_price: float, tp1_qty: float,
                  leverage: int) -> OrderResult:
        """开多：下买单 + 挂止损 + 挂 TP1。返回实际成交结果。"""
        ...

    @abstractmethod
    def open_short(self, symbol: str, quantity: float, entry_price: float,
                   stop_loss_price: float, tp1_price: float, tp1_qty: float,
                   leverage: int) -> OrderResult:
        """开空：下卖单 + 挂止损 + 挂 TP1。返回实际成交结果。"""
        ...

    @abstractmethod
    def close_position(self, symbol: str, quantity: float, side: str = "LONG",
                       reason: str = "") -> OrderResult:
        """市价平仓（部分或全部）"""
        ...

    @abstractmethod
    def update_stop_loss(self, symbol: str, old_order_id: str,
                         new_stop_price: float, quantity: float, side: str = "LONG") -> OrderResult:
        """撤旧止损挂新止损"""
        ...

    @abstractmethod
    def place_take_profit(self, symbol: str, price: float, quantity: float,
                          side: str = "LONG") -> OrderResult:
        """挂止盈单"""
        ...

    @abstractmethod
    def cancel_order_safe(self, symbol: str, order_id: str) -> bool:
        """安全撤单（忽略已不存在的订单）"""
        ...

    @abstractmethod
    def get_account_balance(self) -> dict:
        """返回 {balance, available, unrealized_pnl}"""
        ...

    @abstractmethod
    def get_open_orders(self, symbol: str) -> list[dict]:
        """获取该 symbol 的活跃委托"""
        ...


# === 模拟执行器 ===

class PaperExecutor(OrderExecutor):
    """模拟交易：不调用任何 API，所有"成交"由程序模拟。"""

    _counter = 0

    def _next_id(self) -> str:
        PaperExecutor._counter += 1
        return f"paper_{PaperExecutor._counter}"

    def open_long(self, symbol, quantity, entry_price, stop_loss_price,
                  tp1_price, tp1_qty, leverage) -> OrderResult:
        slippage = entry_price * config.TRADING_ASSUMED_SLIPPAGE_PCT / 100
        fill_price = entry_price + slippage
        return OrderResult(
            success=True,
            order_id=self._next_id(),
            fill_price=fill_price,
            fill_qty=quantity,
            status="FILLED",
        )

    def open_short(self, symbol, quantity, entry_price, stop_loss_price,
                   tp1_price, tp1_qty, leverage) -> OrderResult:
        slippage = entry_price * config.TRADING_ASSUMED_SLIPPAGE_PCT / 100
        fill_price = max(entry_price - slippage, 0)
        return OrderResult(
            success=True,
            order_id=self._next_id(),
            fill_price=fill_price,
            fill_qty=quantity,
            status="FILLED",
        )

    def close_position(self, symbol, quantity, side="LONG", reason="") -> OrderResult:
        return OrderResult(
            success=True, order_id=self._next_id(),
            fill_qty=quantity, status="FILLED",
        )

    def update_stop_loss(self, symbol, old_order_id, new_stop_price, quantity, side="LONG") -> OrderResult:
        return OrderResult(success=True, order_id=self._next_id(), status="NEW")

    def place_take_profit(self, symbol, price, quantity, side="LONG") -> OrderResult:
        return OrderResult(success=True, order_id=self._next_id(), status="NEW")

    def cancel_order_safe(self, symbol, order_id) -> bool:
        return True

    def get_account_balance(self) -> dict:
        return {"balance": 0, "available": 0, "unrealized_pnl": 0}

    def get_open_orders(self, symbol) -> list[dict]:
        return []


# === 实盘执行器 ===

class BinanceLiveExecutor(OrderExecutor):
    """真实下单到币安 USDT-M 合约。"""

    def __init__(self, client: BinanceFuturesClient | None = None):
        self.client = client or BinanceFuturesClient()

    @staticmethod
    def _cap_stop_price_by_open_loss(entry_price: float, stop_price: float,
                                     side: str = "LONG") -> float:
        """Clamp stop so initial OPEN risk never exceeds TRADING_OPEN_MAX_LOSS_PCT."""
        if entry_price <= 0 or stop_price <= 0:
            return stop_price
        side = (side or "LONG").upper()
        max_loss_pct = float(getattr(config, "TRADING_OPEN_MAX_LOSS_PCT", 0) or 0)
        if max_loss_pct <= 0:
            return stop_price
        if side == "SHORT":
            ceiling = entry_price * (1 + max_loss_pct / 100.0)
            if stop_price > ceiling:
                _log(
                    f"止损价 {stop_price:.8g} 超过 OPEN 最大亏损 {max_loss_pct:.2f}% 限制，"
                    f"下调到 {ceiling:.8g}"
                )
                return ceiling
            return stop_price
        floor = entry_price * (1 - max_loss_pct / 100.0)
        if stop_price < floor:
            _log(
                f"止损价 {stop_price:.8g} 超过 OPEN 最大亏损 {max_loss_pct:.2f}% 限制，"
                f"上调到 {floor:.8g}"
            )
            return floor
        return stop_price

    def open_long(self, symbol: str, quantity: float, entry_price: float,
                  stop_loss_price: float, tp1_price: float, tp1_qty: float,
                  leverage: int) -> OrderResult:
        """
        开多完整流程：
        1. 设置杠杆
        2. 市价买入
        3. 确认成交后挂止损单
        4. 挂 TP1 止盈单
        若止损单挂失败 → 短重试 → 仍失败则返回 STOP_PENDING 让上层记录提醒
        """
        symbol = symbol.upper()

        # 1. 设置全仓模式和杠杆（幂等操作）
        try:
            self.client.set_margin_type(symbol, "CROSSED")
        except BinanceAPIError as e:
            # -4046 表示已经是目标模式，忽略
            if e.code != -4046:
                _log(f"设置保证金模式失败 {symbol}: {e}")
                return OrderResult(success=False, error=f"设置保证金模式失败: {e.msg}")
        try:
            self.client.set_leverage(symbol, leverage)
        except BinanceAPIError as e:
            _log(f"设置杠杆失败 {symbol}: {e}")
            return OrderResult(success=False, error=f"设置杠杆失败: {e.msg}")

        # 2. 市价买入
        try:
            entry_resp = self.client.market_buy(symbol, quantity)
        except BinanceAPIError as e:
            _log(f"市价买入失败 {symbol}: {e}")
            return OrderResult(success=False, error=f"买入失败: {e.msg}")

        order_id = str(entry_resp.get("orderId", ""))
        fill_price = float(entry_resp.get("avgPrice", 0) or 0)
        fill_qty = float(entry_resp.get("executedQty", 0) or 0)
        status = entry_resp.get("status", "")

        # 关键：币安市价单的同步响应里 status 不保证是 FILLED，可能是
        # NEW / PARTIALLY_FILLED（撮合还没追上）。如果直接根据 status 判断失败，
        # 就会让一个真实存在于交易所的多头仓位漂在外面、没有止损保护。
        # 因此这里对所有非终态的响应都轮询订单到终态再判断。
        TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
        if status not in TERMINAL and order_id:
            final = self._poll_order_terminal(symbol, order_id)
            if final:
                status = final.get("status", status)
                final_fill_price = float(final.get("avgPrice", 0) or 0)
                final_fill_qty = float(final.get("executedQty", 0) or 0)
                if final_fill_price > 0:
                    fill_price = final_fill_price
                if final_fill_qty > 0:
                    fill_qty = final_fill_qty

        # avgPrice 可能为 0（罕见），通过查单兜底获取实际成交价
        if fill_price <= 0 and fill_qty > 0 and order_id:
            try:
                detail = self.client.get_order(symbol, int(order_id))
                fill_price = float(detail.get("avgPrice", 0) or 0)
            except Exception:
                pass

        # 关键判断：以"是否真的成交了"（fill_qty > 0）为准，而不是 status 字符串。
        # 这样 PARTIALLY_FILLED 也能继续走流程为已成交部分挂止损保护。
        if fill_qty <= 0:
            # 真的没成交：尝试撤掉可能还挂在簿上的订单
            if order_id and status in ("NEW", "PARTIALLY_FILLED"):
                try:
                    self.client.cancel_order(symbol, int(order_id))
                    _log(f"未成交订单已撤 {symbol} #{order_id}")
                except BinanceAPIError as e:
                    _log(f"撤未成交订单失败 {symbol} #{order_id}: {e}")
            _log(f"买入未成交 {symbol}: status={status} fill_qty={fill_qty}")
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty, status=status,
                error=f"买入未成交: {status}",
            )

        # 部分成交：警告但继续走止损流程，避免裸多
        if status != "FILLED":
            _log(
                f"买入仅部分成交 {symbol}: 已成交 {fill_qty}/{quantity} status={status} "
                f"— 继续为已成交部分挂止损保护"
            )

        # 用实际成交价重新锚定止损（保持同一止损百分比），并强制遵守 OPEN 最大亏损上限。
        # 避免“DB 显示 5%，交易所真实挂单 >5%”的偏差。
        extra = {"entry_order_id": order_id}
        effective_stop_price = stop_loss_price
        if entry_price > 0 and stop_loss_price > 0 and fill_price > 0:
            stop_pct = (stop_loss_price - entry_price) / entry_price * 100.0
            effective_stop_price = fill_price * (1 + stop_pct / 100.0)
        effective_stop_price = self._cap_stop_price_by_open_loss(fill_price, effective_stop_price, side="LONG")

        # 3. 挂止损单（最关键 — 必须成功）
        stop_result = self._place_stop_with_retry(
            symbol=symbol,
            quantity=fill_qty,
            stop_price=effective_stop_price,
            entry_price=fill_price,
            side="LONG",
        )
        stop_order_id = stop_result.order_id
        if not stop_order_id:
            # 保护单失败后不反复开平仓制造手续费；把真实仓位落库并提醒人工检查。
            if not getattr(config, "LIVE_EMERGENCY_CLOSE_ON_STOP_TRANSIENT_FAILURE", False):
                _log(f"止损单暂未挂上，保留仓位并记录提醒 {symbol}: {stop_result.error}")
                extra["stop_order_pending"] = True
                extra["stop_error"] = stop_result.error
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    fill_price=fill_price,
                    fill_qty=fill_qty,
                    status="STOP_PENDING",
                    extra=extra,
                )

            # 用户显式打开保命平仓时，才在保护单失败后立即平仓。
            _log(f"止损单挂失败，紧急平仓 {symbol}")
            try:
                self.client.market_sell(symbol, fill_qty, reduce_only=True, position_side=self.client._long_position_side())
            except Exception as e2:
                _log(f"紧急平仓也失败了！{symbol}: {e2}")
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty,
                error="止损单挂失败，已紧急平仓",
            )
        extra["stop_order_id"] = stop_order_id
        if stop_result.trigger_price and stop_result.trigger_price > 0:
            extra["placed_stop_price"] = stop_result.trigger_price

        # 4. 挂 TP1 止盈单（非关键，失败不阻塞）
        tp1_order_id = None
        if tp1_qty > 0 and tp1_price > fill_price:
            tp1_result = self._place_take_profit_with_retry(
                symbol=symbol,
                price=tp1_price,
                quantity=tp1_qty,
                retries=getattr(config, "LIVE_TP_ORDER_RETRY_COUNT", 2),
                side="LONG",
            )
            if tp1_result.success and tp1_result.order_id:
                tp1_order_id = tp1_result.order_id
                extra["tp1_order_id"] = tp1_order_id
            else:
                _log(f"TP1 止盈单挂失败（非致命）{symbol}: {tp1_result.error}")

        return OrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            status="FILLED",
            extra=extra,
        )

    def open_short(self, symbol: str, quantity: float, entry_price: float,
                   stop_loss_price: float, tp1_price: float, tp1_qty: float,
                   leverage: int) -> OrderResult:
        symbol = symbol.upper()
        try:
            self.client.set_margin_type(symbol, "CROSSED")
        except BinanceAPIError as e:
            if e.code != -4046:
                _log(f"设置保证金模式失败 {symbol}: {e}")
                return OrderResult(success=False, error=f"设置保证金模式失败: {e.msg}")
        try:
            self.client.set_leverage(symbol, leverage)
        except BinanceAPIError as e:
            _log(f"设置杠杆失败 {symbol}: {e}")
            return OrderResult(success=False, error=f"设置杠杆失败: {e.msg}")

        try:
            entry_resp = self.client.market_sell(
                symbol, quantity, reduce_only=False, position_side=self.client._short_position_side()
            )
        except BinanceAPIError as e:
            _log(f"市价卖出开空失败 {symbol}: {e}")
            return OrderResult(success=False, error=f"开空失败: {e.msg}")

        order_id = str(entry_resp.get("orderId", ""))
        fill_price = float(entry_resp.get("avgPrice", 0) or 0)
        fill_qty = float(entry_resp.get("executedQty", 0) or 0)
        status = entry_resp.get("status", "")
        TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
        if status not in TERMINAL and order_id:
            final = self._poll_order_terminal(symbol, order_id)
            if final:
                status = final.get("status", status)
                final_fill_price = float(final.get("avgPrice", 0) or 0)
                final_fill_qty = float(final.get("executedQty", 0) or 0)
                if final_fill_price > 0:
                    fill_price = final_fill_price
                if final_fill_qty > 0:
                    fill_qty = final_fill_qty
        if fill_price <= 0 and fill_qty > 0 and order_id:
            try:
                detail = self.client.get_order(symbol, int(order_id))
                fill_price = float(detail.get("avgPrice", 0) or 0)
            except Exception:
                pass
        if fill_qty <= 0:
            if order_id and status in ("NEW", "PARTIALLY_FILLED"):
                try:
                    self.client.cancel_order(symbol, int(order_id))
                except Exception:
                    pass
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty, status=status,
                error=f"开空未成交: {status}",
            )

        extra = {"entry_order_id": order_id}
        effective_stop_price = stop_loss_price
        if entry_price > 0 and stop_loss_price > 0 and fill_price > 0:
            stop_pct = (stop_loss_price - entry_price) / entry_price * 100.0
            effective_stop_price = fill_price * (1 + stop_pct / 100.0)
        effective_stop_price = self._cap_stop_price_by_open_loss(fill_price, effective_stop_price, side="SHORT")

        stop_result = self._place_stop_with_retry(
            symbol=symbol,
            quantity=fill_qty,
            stop_price=effective_stop_price,
            entry_price=fill_price,
            side="SHORT",
        )
        stop_order_id = stop_result.order_id
        if not stop_order_id:
            if not getattr(config, "LIVE_EMERGENCY_CLOSE_ON_STOP_TRANSIENT_FAILURE", False):
                extra["stop_order_pending"] = True
                extra["stop_error"] = stop_result.error
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    fill_price=fill_price,
                    fill_qty=fill_qty,
                    status="STOP_PENDING",
                    extra=extra,
                )
            try:
                self.client.market_buy(
                    symbol, fill_qty, reduce_only=True, position_side=self.client._short_position_side()
                )
            except Exception:
                pass
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty,
                error="止损单挂失败，已紧急平仓",
            )
        extra["stop_order_id"] = stop_order_id
        if stop_result.trigger_price and stop_result.trigger_price > 0:
            extra["placed_stop_price"] = stop_result.trigger_price

        if tp1_qty > 0 and tp1_price < fill_price:
            tp1_result = self._place_take_profit_with_retry(
                symbol=symbol,
                price=tp1_price,
                quantity=tp1_qty,
                retries=getattr(config, "LIVE_TP_ORDER_RETRY_COUNT", 2),
                side="SHORT",
            )
            if tp1_result.success and tp1_result.order_id:
                extra["tp1_order_id"] = tp1_result.order_id

        return OrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            status="FILLED",
            extra=extra,
        )

    @staticmethod
    def _is_transient_stop_error(err: Exception) -> bool:
        if isinstance(err, (URLError, OSError, TimeoutError)):
            return True
        if isinstance(err, BinanceAPIError):
            if err.code == 429 or err.code >= 500:
                return True
            return err.code in {-1000, -1001, -1006, -1007}
        return False

    def _place_stop_with_retry(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        entry_price: float | None = None,
        side: str = "LONG",
    ) -> StopPlacementResult:
        """挂止损单，失败重试。网络类错误返回 pending，硬错误返回失败。"""
        retries = getattr(
            config, "LIVE_STOP_ORDER_RETRY_COUNT",
            getattr(config, "LIVE_ORDER_RETRY_COUNT", 3),
        )
        delay = getattr(
            config, "LIVE_STOP_ORDER_RETRY_DELAY_S",
            getattr(config, "LIVE_ORDER_RETRY_DELAY_S", 1.0),
        )
        last_error = ""
        saw_transient = False
        side = (side or "LONG").upper()
        for attempt in range(retries):
            if side == "SHORT":
                trigger_price = self._adjust_stop_price_above_mark(
                    symbol=symbol,
                    stop_price=stop_price,
                    attempt=attempt,
                    entry_price=entry_price,
                )
            else:
                trigger_price = self._adjust_stop_price_below_mark(
                    symbol=symbol,
                    stop_price=stop_price,
                    attempt=attempt,
                    entry_price=entry_price,
                )
            try:
                if side == "SHORT":
                    resp = self.client.stop_market_buy(symbol, quantity, trigger_price)
                else:
                    resp = self.client.stop_market_sell(symbol, quantity, trigger_price)
                oid = str(resp.get("orderId") or resp.get("algoId") or resp.get("strategyId") or "")
                if oid:
                    return StopPlacementResult(order_id=oid, trigger_price=trigger_price)
            except BinanceAPIError as e:
                last_error = str(e)
                if e.code == -2021 and attempt < retries - 1:
                    saw_transient = True
                    _log(f"止损单触发价过近，刷新标记价后重试 {attempt+1}/{retries}: {e}")
                    if attempt < retries - 1:
                        time.sleep(delay)
                    continue
                if self._is_transient_stop_error(e):
                    saw_transient = True
                else:
                    _log(f"止损单硬失败 {attempt+1}/{retries}: {e}")
                    return StopPlacementResult(
                        transient_failure=False,
                        error=last_error,
                    )
                _log(f"止损单重试 {attempt+1}/{retries}: {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
            except (URLError, OSError, TimeoutError) as e:
                last_error = str(e)
                saw_transient = True
                _log(f"止损单网络重试 {attempt+1}/{retries}: {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
        return StopPlacementResult(
            transient_failure=saw_transient,
            error=last_error or "未返回止损订单号",
        )

    @staticmethod
    def _adjust_stop_price_below_mark(
        symbol: str,
        stop_price: float,
        attempt: int = 0,
        entry_price: float | None = None,
    ) -> float:
        """Avoid Binance -2021 by keeping long stop triggers below current mark price."""
        try:
            token = symbol.upper()
            if token.endswith("USDT"):
                token = token[:-4]
            stop_price = BinanceLiveExecutor._cap_stop_price_by_open_loss(
                float(entry_price or 0),
                stop_price,
                side="LONG",
            )
            mark = get_mark_price(token)
            if not mark or mark <= 0:
                return stop_price
            if stop_price < mark:
                return stop_price
            buffer_pct = getattr(config, "LIVE_STOP_MARK_PRICE_BUFFER_PCT", 0.2)
            buffer_pct *= (attempt + 1)
            adjusted = mark * (1 - buffer_pct / 100)
            adjusted = BinanceLiveExecutor._cap_stop_price_by_open_loss(
                float(entry_price or 0),
                adjusted,
                side="LONG",
            )
            _log(
                f"止损价 {stop_price:.8g} 已不低于标记价 {mark:.8g}，"
                f"下移到 {adjusted:.8g}"
            )
            return adjusted
        except Exception as e:
            _log(f"止损价标记价校验失败，使用原止损价: {e}")
            return stop_price

    @staticmethod
    def _adjust_stop_price_above_mark(
        symbol: str,
        stop_price: float,
        attempt: int = 0,
        entry_price: float | None = None,
    ) -> float:
        """Avoid Binance -2021 by keeping short stop triggers above current mark price."""
        try:
            token = symbol.upper()
            if token.endswith("USDT"):
                token = token[:-4]
            stop_price = BinanceLiveExecutor._cap_stop_price_by_open_loss(
                float(entry_price or 0),
                stop_price,
                side="SHORT",
            )
            mark = get_mark_price(token)
            if not mark or mark <= 0:
                return stop_price
            if stop_price > mark:
                return stop_price
            buffer_pct = getattr(config, "LIVE_STOP_MARK_PRICE_BUFFER_PCT", 0.2)
            buffer_pct *= (attempt + 1)
            adjusted = mark * (1 + buffer_pct / 100)
            adjusted = BinanceLiveExecutor._cap_stop_price_by_open_loss(
                float(entry_price or 0),
                adjusted,
                side="SHORT",
            )
            _log(
                f"止损价 {stop_price:.8g} 已不高于标记价 {mark:.8g}，"
                f"上移到 {adjusted:.8g}"
            )
            return adjusted
        except Exception as e:
            _log(f"止损价标记价校验失败，使用原止损价: {e}")
            return stop_price

    @staticmethod
    def _adjust_take_profit_price_above_mark(symbol: str, take_profit_price: float, attempt: int = 0) -> float:
        """Avoid immediate-trigger TP orders by keeping sell TP above current mark price."""
        try:
            token = symbol.upper()
            if token.endswith("USDT"):
                token = token[:-4]
            mark = get_mark_price(token)
            if not mark or mark <= 0:
                return take_profit_price
            if take_profit_price > mark:
                return take_profit_price
            buffer_pct = getattr(config, "LIVE_TAKE_PROFIT_MARK_PRICE_BUFFER_PCT", 0.2)
            buffer_pct *= (attempt + 1)
            adjusted = mark * (1 + buffer_pct / 100)
            _log(
                f"止盈价 {take_profit_price:.8g} 已不高于标记价 {mark:.8g}，"
                f"上移到 {adjusted:.8g}"
            )
            return adjusted
        except Exception as e:
            _log(f"止盈价标记价校验失败，使用原止盈价: {e}")
            return take_profit_price

    def _place_take_profit_with_retry(
        self,
        symbol: str,
        price: float,
        quantity: float,
        retries: int = 1,
        side: str = "LONG",
    ) -> OrderResult:
        rounded_qty = self.client.round_quantity(symbol, quantity)
        if rounded_qty <= 0:
            return OrderResult(success=False, error=f"止盈数量过小（round 后为 {rounded_qty}）")

        retries = max(1, int(retries))
        side = (side or "LONG").upper()
        for attempt in range(retries):
            if side == "SHORT":
                trigger_price = self._adjust_take_profit_price_below_mark(symbol, price, attempt)
            else:
                trigger_price = self._adjust_take_profit_price_above_mark(symbol, price, attempt)
            try:
                if side == "SHORT":
                    resp = self.client.take_profit_market_buy(symbol, rounded_qty, trigger_price)
                else:
                    resp = self.client.take_profit_market_sell(symbol, rounded_qty, trigger_price)
                return OrderResult(
                    success=True,
                    order_id=str(resp.get("orderId") or resp.get("algoId") or resp.get("strategyId") or ""),
                    status=resp.get("status", "NEW"),
                )
            except BinanceAPIError as e:
                if e.code == -2021 and attempt < retries - 1:
                    _log(f"止盈单触发价过近，重试 {attempt+1}/{retries}: {e}")
                    continue
                return OrderResult(success=False, error=f"止盈单失败: {e.msg}")
            except Exception as e:
                return OrderResult(success=False, error=f"止盈单失败: {e}")

        return OrderResult(success=False, error="止盈单失败: 重试后仍失败")

    @staticmethod
    def _adjust_take_profit_price_below_mark(symbol: str, take_profit_price: float, attempt: int = 0) -> float:
        """Avoid immediate-trigger TP orders by keeping short TP below current mark price."""
        try:
            token = symbol.upper()
            if token.endswith("USDT"):
                token = token[:-4]
            mark = get_mark_price(token)
            if not mark or mark <= 0:
                return take_profit_price
            if take_profit_price < mark:
                return take_profit_price
            buffer_pct = getattr(config, "LIVE_TAKE_PROFIT_MARK_PRICE_BUFFER_PCT", 0.2)
            buffer_pct *= (attempt + 1)
            adjusted = mark * (1 - buffer_pct / 100)
            _log(
                f"止盈价 {take_profit_price:.8g} 已不低于标记价 {mark:.8g}，"
                f"下移到 {adjusted:.8g}"
            )
            return adjusted
        except Exception as e:
            _log(f"止盈价标记价校验失败，使用原止盈价: {e}")
            return take_profit_price

    def _poll_order_terminal(self, symbol: str, order_id: str,
                              timeout_s: float = 5.0,
                              interval_s: float = 0.5) -> dict | None:
        """轮询订单直到达到终态(FILLED/CANCELED/EXPIRED/REJECTED)或超时。

        用途：处理市价单同步响应里 status 暂未变成 FILLED 的情况，
        避免根据中间态错误地判定订单失败（会导致裸多漂在交易所）。

        返回：终态的订单详情；超时则返回最后一次拿到的快照（可能仍是非终态）；
              全程查询失败则返回 None。
        """
        if not order_id:
            return None
        TERMINAL = ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
        deadline = time.time() + timeout_s
        last_detail = None
        while time.time() < deadline:
            try:
                detail = self.client.get_order(symbol, int(order_id))
                last_detail = detail
                if detail.get("status", "") in TERMINAL:
                    return detail
            except BinanceAPIError as e:
                _log(f"查询订单失败 {symbol} #{order_id}: {e}")
            except Exception as e:
                _log(f"查询订单异常 {symbol} #{order_id}: {e}")
            time.sleep(interval_s)
        return last_detail

    def close_position(self, symbol: str, quantity: float, side: str = "LONG",
                       reason: str = "") -> OrderResult:
        try:
            side = (side or "LONG").upper()
            if side == "SHORT":
                resp = self.client.market_buy(
                    symbol, quantity, reduce_only=True, position_side=self.client._short_position_side()
                )
            else:
                resp = self.client.market_sell(
                    symbol, quantity, reduce_only=True, position_side=self.client._long_position_side()
                )
            return OrderResult(
                success=True,
                order_id=str(resp.get("orderId", "")),
                fill_price=float(resp.get("avgPrice", 0)),
                fill_qty=float(resp.get("executedQty", 0)),
                status=resp.get("status", ""),
            )
        except BinanceAPIError as e:
            return OrderResult(success=False, error=f"平仓失败: {e.msg}")

    def update_stop_loss(self, symbol: str, old_order_id: str,
                         new_stop_price: float, quantity: float, side: str = "LONG") -> OrderResult:
        """撤旧止损，挂新止损"""
        # 先撤旧单
        self.cancel_order_safe(symbol, old_order_id)
        # 挂新止损
        stop_result = self._place_stop_with_retry(symbol, quantity, new_stop_price, side=side)
        if stop_result.order_id:
            return OrderResult(success=True, order_id=stop_result.order_id, status="NEW")
        return OrderResult(success=False, error=f"更新止损失败: {stop_result.error}")

    def place_take_profit(self, symbol: str, price: float, quantity: float,
                          side: str = "LONG") -> OrderResult:
        return self._place_take_profit_with_retry(symbol, price, quantity, retries=1, side=side)

    def cancel_order_safe(self, symbol: str, order_id: str) -> bool:
        if not order_id:
            return True
        try:
            if self.client.use_unified_account:
                self.client.cancel_conditional_order(symbol, int(order_id))
            else:
                self.client.cancel_order(symbol, int(order_id))
            return True
        except BinanceAPIError as e:
            # -2011: Unknown order / already canceled
            if e.code == -2011:
                return True
            _log(f"撤单失败 {symbol} #{order_id}: {e}")
            return False

    def get_account_balance(self) -> dict:
        return self.client.get_balance()

    def get_open_orders(self, symbol: str) -> list[dict]:
        try:
            return self.client.get_open_orders(symbol)
        except BinanceAPIError:
            return []


# === 工厂函数 ===

_live_executor: BinanceLiveExecutor | None = None


def get_executor(mode: str) -> OrderExecutor:
    """根据 mode 返回合适的 executor 实例。live 模式复用单例。"""
    if mode == "live":
        global _live_executor
        if _live_executor is None:
            _live_executor = BinanceLiveExecutor()
        return _live_executor
    return PaperExecutor()
