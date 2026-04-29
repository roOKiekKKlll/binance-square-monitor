"""Automatic trading loop.

Supports both paper (simulated) and live (real Binance futures) trading.
Mode is determined by the 'mode' setting in trading_settings table.

Run:
    python auto_trader.py
"""
from __future__ import annotations

import sqlite3
import signal
import sys
import time

from rich.console import Console

import config
import storage
import trade_logic
from executor import get_executor, BinanceLiveExecutor


console = Console()
_running = True
_last_lock_log_at = 0.0


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，自动交易循环准备停止...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def _verify_live_api() -> bool:
    """启动时验证实盘 API 连通性"""
    try:
        executor = get_executor("live")
        if isinstance(executor, BinanceLiveExecutor):
            balance = executor.get_account_balance()
            console.print(
                f"[green]实盘 API 验证通过 — "
                f"余额: ${balance.get('balance', 0):.2f} "
                f"可用: ${balance.get('available', 0):.2f}[/green]"
            )
            return True
    except Exception as e:
        console.print(f"[red]实盘 API 验证失败: {e}[/red]")
        console.print("[red]请检查 .env 中的 BINANCE_API_KEY 和 BINANCE_API_SECRET[/red]")
        return False
    return False


def one_scan():
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)

    mode = settings.get("mode", "paper")
    executor = get_executor(mode)

    # 仓位管理
    if mode == "live":
        import live_manager
        live_manager.update_live_positions(executor)
    else:
        with storage.get_conn() as conn:
            trade_logic.update_paper_positions(conn)

    if not settings.get("enabled"):
        return {"opened": 0, "enabled": False}

    with storage.get_conn() as conn:
        candidates = trade_logic.build_trade_candidates(
            conn, limit=config.COMPOSITE_HEAT_TOP_N, passed_only=True)

    opened = 0
    mode_label = "实盘" if mode == "live" else "模拟"
    for candidate in candidates:
        if not candidate.get("has_active_position"):
            with storage.get_conn() as conn:
                if trade_logic.open_position(conn, candidate, settings, executor):
                    opened += 1
                    console.print(
                        f"[green]{mode_label}市价开多信号: {candidate['token']} "
                        f"price={candidate['price']:.8g}[/green]"
                    )
    return {"opened": opened, "enabled": True, "mode": mode}


def main():
    storage.init_db()

    # 检测初始模式
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
    mode = settings.get("mode", "paper")

    if mode == "live":
        console.print("[bold yellow]=== 自动交易循环启动（实盘模式） ===[/bold yellow]")
        if not _verify_live_api():
            console.print("[red]实盘 API 不可用，退出。[/red]")
            sys.exit(1)
    else:
        console.print("[green]=== 自动交易循环启动（模拟交易） ===[/green]")
        console.print("[dim]Web 面板里打开自动交易后，才会按规则生成模拟市价多单。[/dim]")

    last_cleanup_at = 0.0
    while _running:
        try:
            result = one_scan()
            if result.get("enabled") and result.get("opened"):
                mode_label = "实盘" if result.get("mode") == "live" else "模拟"
                console.print(f"[green]本轮新增{mode_label}订单 {result['opened']} 个[/green]")

            # 每小时清理一次旧的 signal lock
            now = time.time()
            if now - last_cleanup_at >= 3600:
                try:
                    with storage.get_conn() as conn:
                        deleted = storage.trade_signal_lock_cleanup(
                            conn, config.TRADING_SIGNAL_LOCK_RETENTION_HOURS)
                    if deleted:
                        console.print(f"[dim]已清理 {deleted} 条过期 signal lock[/dim]")
                except Exception as e:
                    console.print(f"[dim]signal lock 清理失败: {e}[/dim]")
                last_cleanup_at = now
        except sqlite3.OperationalError as e:
            global _last_lock_log_at
            if "database is locked" not in str(e).lower():
                console.print(f"[red]自动交易循环错误: {e}[/red]")
                time.sleep(3)
                continue
            now = time.time()
            if now - _last_lock_log_at >= 30:
                console.print("[yellow]数据库正忙，本轮自动交易跳过，稍后重试。[/yellow]")
                _last_lock_log_at = now
        except Exception as e:
            console.print(f"[red]自动交易循环错误: {e}[/red]")
            time.sleep(3)
            continue
        time.sleep(2)
    console.print("[green]自动交易循环已退出[/green]")


if __name__ == "__main__":
    main()
