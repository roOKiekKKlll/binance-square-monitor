"""
Realtime futures market cache for watchlist and open-position tokens.

Run:
    python market_realtime.py

This process listens to tokens in the local watchlist and open positions. It keeps a fast
SQLite cache updated from Binance USD-S Futures WebSocket streams:
mark price, best bid/ask, top depth, and recent aggregate trades.
"""
from __future__ import annotations

import asyncio
import json
import signal
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import websockets
from rich.console import Console

import config
import storage
import ssl


FSTREAM_BASE = "wss://fstream.binance.com/stream?streams="
TRADE_WINDOW_SECONDS = 60

console = Console()
_running = True


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，正在关闭实时行情...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _symbol(token: str) -> str:
    return f"{token.upper()}USDT"


def _token_from_symbol(symbol: str) -> str:
    up = symbol.upper()
    return up[:-4] if up.endswith("USDT") else up


def _get_realtime_tokens() -> list[str]:
    """构造需要实时订阅的代币集合：观察列表 + 当前持仓 + 最近一轮热度榜前 N。

    热度榜从 token_heat_history 取最近一轮、按 score 降序前 N 个；之前是按
    market_snapshots.updated_at 排序，那是"最近被 worker 刷过的"，并不是真正的
    热度榜——worker 跨周期把不同批次都刷过后，结果会偏向"刷新时间近"的代币。
    """
    try:
        with storage.get_conn() as conn:
            tokens = set(storage.watchlist_get_all(conn))
            tokens.update(p["token"] for p in storage.trade_open_positions(conn))

            try:
                res = conn.execute("""
                    SELECT token FROM token_heat_history
                    WHERE round_number = (SELECT MAX(round_number) FROM token_heat_history)
                    ORDER BY score DESC
                    LIMIT 20
                """).fetchall()
                tokens.update(r[0] for r in res)
            except Exception:
                # token_heat_history 还没有数据时静默跳过
                pass

            return sorted(t.upper() for t in tokens if t)
    except Exception as e:
        console.print(f"[red]读取实时订阅列表失败: {e}[/red]")
        return []


def _streams_for(tokens: list[str]) -> list[str]:
    streams = []
    for token in tokens:
        sym = _symbol(token).lower()
        streams.extend([
            f"{sym}@markPrice@1s",
            f"{sym}@bookTicker",
            f"{sym}@aggTrade",
            f"{sym}@depth5@100ms",
        ])
    return streams


class RealtimeState:
    def __init__(self, tokens: list[str]):
        self.tokens = {t.upper() for t in tokens}
        self.data = {t.upper(): self._empty(t.upper()) for t in tokens}
        self.trades = defaultdict(deque)

    @staticmethod
    def _empty(token: str) -> dict:
        symbol = _symbol(token)
        return {
            "token": token,
            "symbol": symbol,
            "mark_price": None,
            "last_trade_price": None,
            "best_bid": None,
            "best_bid_qty": None,
            "best_ask": None,
            "best_ask_qty": None,
            "bid_ask_spread_pct": None,
            "depth_bid_top_usd": None,
            "depth_ask_top_usd": None,
            "depth_imbalance_pct": None,
            "trade_buy_usd_60s": 0.0,
            "trade_sell_usd_60s": 0.0,
            "trade_buy_sell_ratio_60s": None,
            "trade_count_60s": 0,
            "updated_at": None,
            "source": "binance_futures_ws",
        }

    def _ensure(self, token: str) -> dict:
        token = token.upper()
        if token not in self.data:
            self.data[token] = self._empty(token)
        return self.data[token]

    def handle(self, event: dict):
        stream = event.get("stream", "")
        stream_l = stream.lower()
        data = event.get("data") or {}
        event_type = data.get("e")
        symbol = data.get("s")
        if not symbol:
            return
        token = _token_from_symbol(symbol)
        if token not in self.tokens:
            return
        item = self._ensure(token)

        if event_type == "markPriceUpdate":
            self._handle_mark_price(item, data)
        elif stream_l.endswith("@bookticker"):
            self._handle_book_ticker(item, data)
        elif event_type == "aggTrade":
            self._handle_agg_trade(token, item, data)
        elif "depth" in stream_l:
            self._handle_depth(item, data)

        item["updated_at"] = _utc_iso()

    @staticmethod
    def _handle_mark_price(item: dict, data: dict):
        try:
            item["mark_price"] = float(data.get("p"))
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _handle_book_ticker(item: dict, data: dict):
        try:
            bid = float(data.get("b"))
            ask = float(data.get("a"))
            item["best_bid"] = bid
            item["best_ask"] = ask
            item["best_bid_qty"] = float(data.get("B"))
            item["best_ask_qty"] = float(data.get("A"))
            mid = item.get("mark_price") or ((bid + ask) / 2)
            if mid > 0:
                item["bid_ask_spread_pct"] = (ask - bid) / mid * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _handle_agg_trade(self, token: str, item: dict, data: dict):
        try:
            price = float(data.get("p"))
            qty = float(data.get("q"))
            ts = int(data.get("T") or _now_ms())
            is_buyer_maker = bool(data.get("m"))
        except (TypeError, ValueError):
            return

        item["last_trade_price"] = price
        usd = price * qty
        side = "sell" if is_buyer_maker else "buy"
        q = self.trades[token]
        q.append((ts, side, usd))

        cutoff = _now_ms() - TRADE_WINDOW_SECONDS * 1000
        while q and q[0][0] < cutoff:
            q.popleft()

        buy = sum(v for _, s, v in q if s == "buy")
        sell = sum(v for _, s, v in q if s == "sell")
        item["trade_buy_usd_60s"] = buy
        item["trade_sell_usd_60s"] = sell
        item["trade_count_60s"] = len(q)
        item["trade_buy_sell_ratio_60s"] = (buy / sell) if sell > 0 else None

    @staticmethod
    def _handle_depth(item: dict, data: dict):
        try:
            bids = [(float(p), float(q)) for p, q in data.get("b", [])]
            asks = [(float(p), float(q)) for p, q in data.get("a", [])]
        except (TypeError, ValueError):
            return
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        item["depth_bid_top_usd"] = bid_usd
        item["depth_ask_top_usd"] = ask_usd
        total = bid_usd + ask_usd
        if total > 0:
            item["depth_imbalance_pct"] = (bid_usd - ask_usd) / total * 100

    def flush(self) -> int:
        rows = [d for d in self.data.values() if d.get("updated_at")]
        if not rows:
            return 0
        with storage.get_conn() as conn:
            for item in rows:
                storage.realtime_upsert(
                    conn,
                    item["token"],
                    item["symbol"],
                    json.dumps(item, ensure_ascii=False),
                )
        return len(rows)


async def run_for_watchlist(tokens: list[str]):
    token_set = {t.upper() for t in tokens}
    streams = _streams_for(tokens)
    if not streams:
        return

    url = FSTREAM_BASE + "/".join(streams)
    state = RealtimeState(tokens)
    last_flush = 0.0
    consecutive_errors = 0
    last_error_msg = ""
    last_error_log_at = 0.0

    console.print(f"[green]实时行情订阅: {', '.join(sorted(token_set))}[/green]")
    ssl_context = ssl._create_unverified_context()
    async with websockets.connect(url, ping_interval=150, ping_timeout=600, ssl=ssl_context) as ws:
        last_watchlist_check = 0.0
        while _running:
            now = time.time()
            if now - last_watchlist_check >= config.REALTIME_WATCHLIST_POLL_SECONDS:
                last_watchlist_check = now
                if set(t.upper() for t in _get_realtime_tokens()) != token_set:
                    console.print("[yellow]实时订阅列表变化，重建实时订阅...[/yellow]")
                    return

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                event = json.loads(raw)
                state.handle(event)
                consecutive_errors = 0
            except asyncio.TimeoutError:
                pass
            except websockets.exceptions.ConnectionClosed as e:
                console.print(f"[yellow]WS 连接已关闭({e.code}), 将重建连接...[/yellow]")
                return
            except Exception as e:
                consecutive_errors += 1
                err_str = str(e)
                now_t = time.time()
                if err_str != last_error_msg or now_t - last_error_log_at >= 10:
                    console.print(
                        f"[dim red]实时消息处理失败: {e}"
                        f"{f' (连续 {consecutive_errors} 次)' if consecutive_errors > 1 else ''}"
                        f"[/dim red]"
                    )
                    last_error_msg = err_str
                    last_error_log_at = now_t
                if consecutive_errors >= 50:
                    console.print("[yellow]连续错误过多，将重建连接...[/yellow]")
                    return

            now = time.time()
            if now - last_flush >= config.REALTIME_CACHE_FLUSH_SECONDS:
                count = state.flush()
                last_flush = now
                if count:
                    console.print(f"[dim]实时缓存已更新 {count} 个代币[/dim]")


async def main():
    storage.init_db()
    console.print("[green]=== Binance Futures 实时行情缓存启动 ===[/green]")
    console.print("[dim]监听观察列表和当前持仓里的代币；列表为空时会等待。[/dim]")

    while _running:
        tokens = _get_realtime_tokens()
        if not tokens:
            console.print("[yellow]实时订阅列表为空，等待添加观察或持仓代币...[/yellow]")
            await asyncio.sleep(config.REALTIME_WATCHLIST_POLL_SECONDS)
            continue
        try:
            await run_for_watchlist(tokens)
        except Exception as e:
            if _running:
                console.print(f"[red]实时行情连接异常: {e}[/red]")
                await asyncio.sleep(5)

    console.print("[green]实时行情已退出[/green]")


if __name__ == "__main__":
    asyncio.run(main())
