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

import config
from binance_client import BinanceFuturesClient, BinanceAPIError


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


# === 抽象基类 ===

class OrderExecutor(ABC):

    @abstractmethod
    def open_long(self, symbol: str, quantity: float, entry_price: float,
                  stop_loss_price: float, tp1_price: float, tp1_qty: float,
                  leverage: int) -> OrderResult:
        """开多：下买单 + 挂止损 + 挂 TP1。返回实际成交结果。"""
        ...

    @abstractmethod
    def close_position(self, symbol: str, quantity: float, reason: str = "") -> OrderResult:
        """市价平仓（部分或全部）"""
        ...

    @abstractmethod
    def update_stop_loss(self, symbol: str, old_order_id: str,
                         new_stop_price: float, quantity: float) -> OrderResult:
        """撤旧止损挂新止损"""
        ...

    @abstractmethod
    def place_take_profit(self, symbol: str, price: float, quantity: float) -> OrderResult:
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

    def close_position(self, symbol, quantity, reason="") -> OrderResult:
        return OrderResult(
            success=True, order_id=self._next_id(),
            fill_qty=quantity, status="FILLED",
        )

    def update_stop_loss(self, symbol, old_order_id, new_stop_price, quantity) -> OrderResult:
        return OrderResult(success=True, order_id=self._next_id(), status="NEW")

    def place_take_profit(self, symbol, price, quantity) -> OrderResult:
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

    def open_long(self, symbol: str, quantity: float, entry_price: float,
                  stop_loss_price: float, tp1_price: float, tp1_qty: float,
                  leverage: int) -> OrderResult:
        """
        开多完整流程：
        1. 设置杠杆
        2. 市价买入
        3. 确认成交后挂止损单
        4. 挂 TP1 止盈单
        若止损单挂失败 → 重试 → 仍失败则市价平仓
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
        fill_price = float(entry_resp.get("avgPrice", 0))
        fill_qty = float(entry_resp.get("executedQty", 0))
        status = entry_resp.get("status", "")

        # avgPrice 可能为 0（罕见），通过查单获取实际成交价
        if status == "FILLED" and fill_price <= 0 and order_id:
            try:
                detail = self.client.get_order(symbol, int(order_id))
                fill_price = float(detail.get("avgPrice", 0))
            except Exception:
                pass

        if status != "FILLED" or fill_qty <= 0:
            _log(f"买入未完全成交 {symbol}: status={status} fill_qty={fill_qty}")
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty, status=status,
                error=f"买入状态异常: {status}",
            )

        # 用实际成交价重新计算止损止盈价格
        # (因为市价单的实际成交价可能和预期价格有偏差)
        extra = {"entry_order_id": order_id}

        # 3. 挂止损单（最关键 — 必须成功）
        stop_order_id = self._place_stop_with_retry(
            symbol, fill_qty, stop_loss_price)
        if not stop_order_id:
            # 止损挂不上 → 立即平仓保命
            _log(f"止损单挂失败，紧急平仓 {symbol}")
            try:
                self.client.market_sell(symbol, fill_qty)
            except Exception as e2:
                _log(f"紧急平仓也失败了！{symbol}: {e2}")
            return OrderResult(
                success=False, order_id=order_id,
                fill_price=fill_price, fill_qty=fill_qty,
                error="止损单挂失败，已紧急平仓",
            )
        extra["stop_order_id"] = stop_order_id

        # 4. 挂 TP1 止盈单（非关键，失败不阻塞）
        tp1_order_id = None
        if tp1_qty > 0 and tp1_price > fill_price:
            try:
                tp1_qty_rounded = self.client.round_quantity(symbol, tp1_qty)
                tp1_resp = self.client.take_profit_market_sell(
                    symbol, tp1_qty_rounded, tp1_price)
                tp1_order_id = str(tp1_resp.get("orderId", ""))
                extra["tp1_order_id"] = tp1_order_id
            except BinanceAPIError as e:
                _log(f"TP1 止盈单挂失败（非致命）{symbol}: {e}")

        return OrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            status="FILLED",
            extra=extra,
        )

    def _place_stop_with_retry(self, symbol: str, quantity: float,
                                stop_price: float) -> str | None:
        """挂止损单，失败重试。返回 order_id 或 None。"""
        retries = getattr(config, "LIVE_ORDER_RETRY_COUNT", 3)
        delay = getattr(config, "LIVE_ORDER_RETRY_DELAY_S", 1.0)
        for attempt in range(retries):
            try:
                resp = self.client.stop_market_sell(symbol, quantity, stop_price)
                oid = str(resp.get("orderId", ""))
                if oid:
                    return oid
            except BinanceAPIError as e:
                _log(f"止损单重试 {attempt+1}/{retries}: {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
        return None

    def close_position(self, symbol: str, quantity: float, reason: str = "") -> OrderResult:
        try:
            resp = self.client.market_sell(symbol, quantity)
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
                         new_stop_price: float, quantity: float) -> OrderResult:
        """撤旧止损，挂新止损"""
        # 先撤旧单
        self.cancel_order_safe(symbol, old_order_id)
        # 挂新止损
        new_id = self._place_stop_with_retry(symbol, quantity, new_stop_price)
        if new_id:
            return OrderResult(success=True, order_id=new_id, status="NEW")
        return OrderResult(success=False, error="更新止损失败")

    def place_take_profit(self, symbol: str, price: float, quantity: float) -> OrderResult:
        try:
            resp = self.client.take_profit_market_sell(symbol, quantity, price)
            return OrderResult(
                success=True,
                order_id=str(resp.get("orderId", "")),
                status=resp.get("status", "NEW"),
            )
        except BinanceAPIError as e:
            return OrderResult(success=False, error=f"止盈单失败: {e.msg}")

    def cancel_order_safe(self, symbol: str, order_id: str) -> bool:
        if not order_id:
            return True
        try:
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
