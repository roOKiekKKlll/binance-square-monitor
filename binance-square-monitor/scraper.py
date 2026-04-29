"""
币安广场抓取器（持续抓取版）

一次 scrape_once() 持续 config.SCRAPE_ROUND_SECONDS 秒：
- 打开一次浏览器
- 不断滚动
- 每滚动 SCROLL_RESET_EVERY 次刷新一次页面，避免懒加载卡死
- 时间到自动关闭

这样一轮能抓到的帖子比"打开-滚几十次-关闭"多几倍。
"""
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Response
import config


FEED_API_KEYWORD = "pgc/feed"
SQUARE_URL = "https://www.binance.com/en/square"
USER_DATA_DIR = Path(__file__).parent / "user_data"


def _utcnow():
    return datetime.now(timezone.utc)


class SquareScraper:
    def __init__(self):
        self.captured_posts = []
        self.captured_authors = {}

    async def _handle_response(self, response: Response):
        url = response.url
        if FEED_API_KEYWORD not in url:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = await response.json()
        except Exception:
            return
        self._scan_for_posts(data)

    def _scan_for_posts(self, node):
        if isinstance(node, dict):
            for key in ("vos", "list", "items", "feedList", "posts"):
                val = node.get(key)
                if isinstance(val, list) and val:
                    if any(self._looks_like_post(x) for x in val[:3]):
                        for post in val:
                            if isinstance(post, dict):
                                try:
                                    self._process_post(post)
                                except Exception:
                                    pass
            for v in node.values():
                self._scan_for_posts(v)
        elif isinstance(node, list):
            for item in node:
                self._scan_for_posts(item)

    @staticmethod
    def _looks_like_post(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        has_content = any(k in obj for k in ("content", "title", "text"))
        has_author = any(k in obj for k in ("authorName", "squareAuthorId", "username", "authorId"))
        has_engagement = any(k in obj for k in ("likeCount", "commentCount", "viewCount"))
        return has_content and has_author and has_engagement

    def _process_post(self, raw: dict):
        post_id = str(
            raw.get("id") or raw.get("contentId") or raw.get("postId")
            or raw.get("handWork") or ""
        )
        if not post_id:
            return

        user_id = str(raw.get("squareAuthorId") or raw.get("authorId") or "")
        if not user_id:
            return

        content = raw.get("content") or ""
        title = raw.get("title") or ""
        full_text = (title + "\n" + content).strip() if title else content

        posted_ts = raw.get("date") or raw.get("createTime")
        if isinstance(posted_ts, (int, float)):
            if posted_ts > 1e11:
                posted_at = datetime.fromtimestamp(posted_ts / 1000, tz=timezone.utc)
            else:
                posted_at = datetime.fromtimestamp(posted_ts, tz=timezone.utc)
        else:
            posted_at = _utcnow()

        likes = int(raw.get("likeCount") or 0)
        comments = int(raw.get("commentCount") or 0)
        shares = int(raw.get("quoteCount") or raw.get("shareCount") or 0)

        tokens = self._extract_tokens_from_post(raw)
        followers = self._extract_followers(raw.get("userLabels") or [])

        self.captured_authors[user_id] = {
            "user_id": user_id,
            "username": raw.get("authorName") or raw.get("username") or "",
            "followers": followers,
            "following": 0,
            "account_created": None,
        }

        self.captured_posts.append({
            "post_id": post_id,
            "user_id": user_id,
            "content": full_text[:2000],
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "posted_at": posted_at,
            "fetched_at": _utcnow(),
            "tokens": tokens,
        })

    @staticmethod
    def _extract_tokens_from_post(raw: dict) -> set[str]:
        tokens = set()
        for tp in (raw.get("tradingPairs") or []):
            code = tp.get("code")
            if code:
                tokens.add(code.upper())
        for tp in (raw.get("tradingPairsV2") or []):
            code = tp.get("code")
            if code:
                tokens.add(code.upper())
        for item in (raw.get("coinPairList") or []):
            if isinstance(item, str):
                sym = item.strip().lstrip("$").strip().upper()
                if sym:
                    tokens.add(sym)
        return tokens

    @staticmethod
    def _extract_followers(user_labels: list) -> int:
        for label in user_labels:
            name = (label.get("name") or "").strip()
            if "follower" not in name.lower():
                continue
            parts = name.split()
            if not parts:
                continue
            num_str = parts[0].replace(",", "")
            try:
                if num_str.lower().endswith("k"):
                    return int(float(num_str[:-1]) * 1000)
                if num_str.lower().endswith("m"):
                    return int(float(num_str[:-1]) * 1_000_000)
                return int(float(num_str))
            except ValueError:
                continue
        return 0

    async def scrape_continuous(self, duration_seconds: int,
                                 progress_cb=None) -> tuple[list[dict], dict[str, dict]]:
        """持续抓取 duration_seconds 秒
        progress_cb(elapsed, scrolls, posts_so_far) 可选回调，用于终端进度提示
        """
        self.captured_posts = []
        self.captured_authors = {}

        start = time.time()
        scrolls = 0

        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=config.HEADLESS,
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"),
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", self._handle_response)

            try:
                await page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)

                while time.time() - start < duration_seconds:
                    await page.mouse.wheel(0, 4000)
                    scrolls += 1
                    await page.wait_for_timeout(config.SCROLL_PAUSE_SECONDS * 1000)

                    # 定期刷新页面避免懒加载卡死
                    if scrolls % config.SCROLL_RESET_EVERY == 0:
                        try:
                            await page.goto(SQUARE_URL, wait_until="domcontentloaded", timeout=60000)
                            await page.wait_for_timeout(2500)
                        except Exception:
                            pass

                    if progress_cb:
                        progress_cb(time.time() - start, scrolls, len(self.captured_posts))
            except Exception as e:
                print(f"[scraper] 出错：{e}")
            finally:
                await context.close()

        # 去重
        seen = set()
        unique_posts = []
        for p in self.captured_posts:
            if p["post_id"] in seen:
                continue
            seen.add(p["post_id"])
            unique_posts.append(p)

        return unique_posts, self.captured_authors
