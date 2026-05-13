"""
后台工作进程：
- 5 分钟持续抓取广场
- 抓完一轮后计算榜单
- 给榜单上有合约的代币 + 观察列表的代币 刷新行情快照，写入数据库
- Web 进程读数据库展示

运行：python worker.py
"""
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone

from rich.console import Console

import config
import storage
from scraper import SquareScraper
from analyzer import extract_tokens_from_text, compute_short_scores
from filters import is_likely_human, post_passes_quality
from market import has_perpetual, get_market_snapshot, get_futures_symbols
from signals import analyze as analyze_signals


console = Console()
_running = True


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，抓完当前轮后停止...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def _utcnow():
    return datetime.now(timezone.utc)


def refresh_market_snapshots(tokens_to_check: list[str], watchlist: list[str] = None):
    """对给定代币列表，拉取行情 + 综合打分 + 存数据库
    如果代币在 watchlist 里，还会追加 followup 记录 + 更新浮亏极值 + 检查是否触发归档

    性能优化（v2）：
      - 每个 token 用独立的短事务，不持有长时间写锁
      - 网络请求发生在事务外，不阻塞 web 端读取
      - 失败重试不阻塞整个流程
    """
    if not tokens_to_check:
        return 0
    try:
        futures_set = get_futures_symbols()
    except Exception as e:
        console.print(f"[red]   获取合约列表失败: {e}[/red]")
        return 0

    watch_set = set(t.upper() for t in (watchlist or []))
    updated = 0
    archived_tokens = []
    benchmark = str(getattr(config, "TRADING_REGIME_BENCHMARK_TOKEN", "BTC") or "BTC").upper()
    benchmark_diag = {
        "seen": False,
        "written": False,
        "reason": "未处理",
        "mark_price": None,
        "change_1h_pct": None,
        "change_4h_pct": None,
        "oi_change_1h_pct": None,
    }

    # Step 1：一次性读好需要的基础数据（短事务）
    with storage.get_conn() as conn:
        short_scores = compute_short_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    # Step 2：逐个 token 处理，网络请求在事务外，写库用独立小事务
    for token in tokens_to_check:
        up = token.upper()
        if up == benchmark:
            benchmark_diag["seen"] = True
        if up not in futures_set:
            if up == benchmark:
                benchmark_diag["reason"] = "无永续合约（未在 futures symbol 列表中）"
            continue

        # === 网络请求（事务外，不占 DB 锁）===
        try:
            snap = get_market_snapshot(token)
        except Exception as e:
            console.print(f"   [dim][red]{token} 抓取失败: {e}[/red][/dim]")
            if up == benchmark:
                benchmark_diag["reason"] = f"抓取失败: {e}"
            continue
        if not snap:
            if up == benchmark:
                benchmark_diag["reason"] = "抓取返回空快照"
            continue

        social_score = social_map.get(token, 0.0)
        try:
            analysis = analyze_signals(snap, social_score)
        except Exception as e:
            console.print(f"   [dim][red]{token} 分析失败: {e}[/red][/dim]")
            if up == benchmark:
                benchmark_diag["reason"] = f"分析失败: {e}"
            continue

        snap_json = json.dumps(snap, default=str, ensure_ascii=False)
        ana_json = json.dumps(analysis, default=str, ensure_ascii=False)

        # === 写入（短事务，就这个 token 的几条数据）===
        try:
            with storage.get_conn() as conn:
                storage.snapshot_upsert(conn, token, snap_json, ana_json)

                # 收藏代币的额外处理
                if up in watch_set:
                    entry = storage.entry_get(conn, up)
                    if entry is None:
                        price = snap.get("mark_price") or 0
                        if price > 0:
                            storage.entry_upsert(conn, up, price, snap_json, ana_json)
                    else:
                        cur_price = snap.get("mark_price") or 0
                        anchor = entry.get("anchor_price") or 0
                        if cur_price > 0 and anchor > 0:
                            pnl = (cur_price - anchor) / anchor * 100
                            storage.followup_add(
                                conn, up, cur_price, pnl, snap_json, ana_json,
                                min_seconds=config.WATCHLIST_FOLLOWUP_MIN_INTERVAL_SECONDS,
                            )
                            storage.entry_update_extremes(conn, up, pnl)

                            if (pnl <= config.LOSS_ARCHIVE_THRESHOLD_PCT
                                    and not entry.get("archived")):
                                storage.archive_loss_sample(conn, up, cur_price, pnl)
                                archived_tokens.append((up, pnl))
            updated += 1
            if up == benchmark:
                benchmark_diag["written"] = True
                benchmark_diag["reason"] = "ok"
                benchmark_diag["mark_price"] = snap.get("mark_price")
                benchmark_diag["change_1h_pct"] = snap.get("change_1h_pct")
                benchmark_diag["change_4h_pct"] = snap.get("change_4h_pct")
                benchmark_diag["oi_change_1h_pct"] = snap.get("oi_change_1h_pct")
        except Exception as e:
            console.print(f"   [dim][red]{token} 入库失败: {e}[/red][/dim]")
            if up == benchmark:
                benchmark_diag["reason"] = f"入库失败: {e}"
            continue

        # 节流（事务外，web 读取不受影响）
        time.sleep(0.4)

    if archived_tokens:
        for t, pnl in archived_tokens:
            console.print(f"   [yellow]⚠ {t} 浮亏 {pnl:.1f}% 已归档为学习样本[/yellow]")

    if benchmark_diag["seen"]:
        if benchmark_diag["written"]:
            ch1h = benchmark_diag["change_1h_pct"]
            ch4h = benchmark_diag["change_4h_pct"]
            oi1h = benchmark_diag["oi_change_1h_pct"]
            missing = []
            if ch1h is None:
                missing.append("change_1h_pct")
            if ch4h is None:
                missing.append("change_4h_pct")
            if oi1h is None:
                missing.append("oi_change_1h_pct")
            if missing:
                console.print(
                    f"[yellow]   Regime 基准 {benchmark} 已写入，但字段缺失: {', '.join(missing)}"
                    f" | mark={benchmark_diag['mark_price']}[/yellow]"
                )
            else:
                console.print(
                    f"[green]   Regime 基准 {benchmark} 已写入"
                    f" | mark={benchmark_diag['mark_price']}"
                    f" | 1h={ch1h:+.2f}% 4h={ch4h:+.2f}% OI1h={oi1h:+.2f}%[/green]"
                )
        else:
            console.print(
                f"[red]   Regime 基准 {benchmark} 本轮未写入: {benchmark_diag['reason']}[/red]"
            )
    else:
        console.print(
            f"[yellow]   Regime 基准 {benchmark} 不在本轮刷新列表（请检查组合逻辑）[/yellow]"
        )

    return updated


def _write_status(**fields):
    """辅助函数：快速写一次 worker 状态"""
    try:
        with storage.get_conn() as conn:
            storage.status_update(conn, **fields)
    except Exception:
        pass


_ROUND_NUMBER = 0


async def one_round(scraper: SquareScraper):
    """一轮：持续抓取 + 存库 + 刷新榜单和观察列表的合约数据"""
    global _ROUND_NUMBER
    _ROUND_NUMBER += 1
    round_start = time.time()
    console.print(f"[blue]=> 开始抓取轮 #{_ROUND_NUMBER}... {datetime.now():%H:%M:%S} "
                  f"（持续 {config.SCRAPE_ROUND_SECONDS}s）[/blue]")

    _write_status(
        stage="scraping",
        detail=f"正在抓取广场帖子（0s / {config.SCRAPE_ROUND_SECONDS}s）",
        round_start=_utcnow().isoformat(timespec="seconds"),
        round_number=_ROUND_NUMBER,
        posts_this_round=0,
        saved_this_round=0,
    )

    def progress(elapsed, scrolls, posts_so_far):
        # 实时写状态到数据库，web 读取就能显示
        _write_status(
            stage="scraping",
            detail=f"抓取中 {int(elapsed)}s / {config.SCRAPE_ROUND_SECONDS}s，"
                   f"滚动 {scrolls} 次，累计 {posts_so_far} 条",
            posts_this_round=posts_so_far,
        )
        if scrolls % 10 == 0:
            console.print(f"   [dim]...{int(elapsed)}s / {config.SCRAPE_ROUND_SECONDS}s, "
                          f"滚动 {scrolls} 次，累计 {posts_so_far} 条[/dim]")

    posts, authors = await scraper.scrape_continuous(
        duration_seconds=config.SCRAPE_ROUND_SECONDS,
        progress_cb=progress,
    )
    console.print(f"   本轮捕获 {len(posts)} 条帖子，{len(authors)} 个作者")

    _write_status(
        stage="saving",
        detail=f"处理帖子入库... 捕获 {len(posts)} 条",
        posts_this_round=len(posts),
    )

    # ============================================================
    # 事务 1：作者 + 帖子入库（短，就入数据）
    # ============================================================
    with storage.get_conn() as conn:
        human_count = 0
        for user_id, a in authors.items():
            a["is_human"] = 1 if is_likely_human(a) else 0
            a["post_count_24h"] = 0
            a["last_seen"] = _utcnow()
            storage.upsert_author(conn, a)
            if a["is_human"]:
                human_count += 1

        saved = 0
        saved_by_kol = 0
        saved_by_engagement = 0
        excluded = config.EXCLUDED_TOKENS or set()
        for post in posts:
            author = authors.get(post["user_id"])
            if not author:
                continue
            if not post_passes_quality(post, author):
                continue

            from filters import is_verified_kol
            if is_verified_kol(author):
                saved_by_kol += 1
            else:
                saved_by_engagement += 1

            tokens = post.get("tokens") or set()
            if not tokens:
                tokens = extract_tokens_from_text(post.get("content", ""))
            if config.TRACKED_TOKENS:
                tokens = {t for t in tokens if t in config.TRACKED_TOKENS}
            tokens = {t for t in tokens if t not in excluded}

            post_for_db = {k: v for k, v in post.items() if k != "tokens"}
            storage.upsert_post(conn, post_for_db)
            if tokens:
                storage.insert_mentions(conn, post["post_id"], tokens)
            saved += 1
    # 事务 1 结束 —— 释放写锁，web 端此时可以自由读取

    # ============================================================
    # 事务 2：清理旧数据（只在每 N 轮跑，不是每轮）
    # ============================================================
    # purge_old 删 7 天前的帖子，每 20 轮跑一次即可（每轮 5 分钟 = 100 分钟/次）
    if _ROUND_NUMBER % 20 == 0:
        try:
            with storage.get_conn() as conn:
                storage.purge_old(conn, days=7)
            console.print(f"   [dim]已清理 7 天前的老帖子[/dim]")
        except Exception as e:
            console.print(f"   [dim]purge 失败: {e}[/dim]")

    # ============================================================
    # 事务 3：统计计数（只读，短）
    # ============================================================
    with storage.get_conn() as conn:
        humans = conn.execute("SELECT COUNT(*) FROM authors WHERE is_human=1").fetchone()[0]
        total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_authors = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    console.print(
        f"   本轮入库 {saved} 条（大V {saved_by_kol} + 高互动 {saved_by_engagement}）"
    )
    console.print(f"   [dim]累计：大V {humans}, 帖子总数 {total_posts}[/dim]")

    # ============================================================
    # 事务 4：状态更新（短）
    # ============================================================
    with storage.get_conn() as conn:
        storage.status_update(
            conn,
            saved_this_round=saved,
            total_posts=total_posts,
            total_authors=total_authors,
        )

    # ============================================================
    # 事务 5：算热度榜 + 写历史（读多写少）
    # ============================================================
    with storage.get_conn() as conn:
        short_scores = compute_short_scores(conn)
        if short_scores:
            storage.heat_history_add(conn, _ROUND_NUMBER, short_scores)
        # 热度历史清理改为每 100 轮（约 8 小时）
        if _ROUND_NUMBER % 100 == 0:
            storage.heat_history_purge_old(conn, keep_last_rounds=200)
        watchlist = storage.watchlist_get_all(conn)

    console.print(f"   15 分钟榜代币数: {len(short_scores)}")
    for i, s in enumerate(short_scores[:10], 1):
        console.print(f"     {i}. {s['token']}  热度={s['score']:.1f}  帖子={s['unique_posts']}")

    # ============================================================
    # HTTP 密集型：拉合约快照
    # 这部分可能要 30-60 秒（30 个代币 × 多个端点），必须在事务外。
    # refresh_market_snapshots 内部自己管连接。
    # ============================================================
    if config.ENABLE_MARKET_ANALYSIS:
        top_tokens = [s["token"] for s in short_scores[:config.MARKET_ANALYSIS_MAX]]
        benchmark = str(getattr(config, "TRADING_REGIME_BENCHMARK_TOKEN", "BTC") or "BTC").upper()
        combined = list(dict.fromkeys(top_tokens + watchlist + [benchmark]))
        _write_status(
            stage="market",
            detail=f"查询合约数据（榜单 {len(top_tokens)} + 观察 {len(watchlist)} = {len(combined)} 代币）",
        )
        console.print(f"[blue]=> 刷新合约数据（榜单 {len(top_tokens)} + 观察列表 {len(watchlist)} = {len(combined)} 去重）...[/blue]")
        updated = refresh_market_snapshots(combined, watchlist=watchlist)
        console.print(f"   已更新 {updated} 个代币的合约快照")

    elapsed = time.time() - round_start
    console.print(f"[green]本轮 #{_ROUND_NUMBER} 总耗时 {elapsed:.0f}s[/green]\n")

    _write_status(
        stage="idle",
        detail=f"本轮完成（入库 {saved} 条 · 耗时 {elapsed:.0f}s）· 即将开始下一轮",
    )


async def main():
    storage.init_db()
    scraper = SquareScraper()

    console.print("[green]=== 币安广场监控 Worker 启动 ===[/green]")
    console.print(f"   每轮持续抓取：{config.SCRAPE_ROUND_SECONDS}s")
    console.print(f"   15 分钟榜单窗口：{config.SHORT_WINDOW_MINUTES} 分钟")
    console.print(f"   粉丝阈值：{config.MIN_FOLLOWERS}")
    console.print(f"   Web 仪表盘：请另开一个终端运行 python web.py")
    console.print()

    while _running:
        try:
            await one_round(scraper)
        except Exception as e:
            console.print(f"[red]本轮出错：{e}[/red]")
            import traceback
            traceback.print_exc()
            # 出错等 30 秒再试
            for _ in range(30):
                if not _running:
                    break
                await asyncio.sleep(1)

    console.print("[green]已退出[/green]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
