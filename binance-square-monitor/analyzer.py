"""
代币提取 + 热度计算（只保留 15 分钟榜）
"""
import re
from collections import defaultdict
from datetime import datetime, timezone
import config


def _text_signature(text: str) -> str:
    """把文案压成粗粒度签名，用来识别复制粘贴刷屏。"""
    if not text:
        return ""
    text = re.sub(r"https?://\S+", " ", text.lower())
    text = re.sub(r"[$#][a-z0-9]{2,10}", " ", text)
    words = re.findall(r"[\w\u4e00-\u9fff]+", text)
    if not words:
        return ""
    return " ".join(words[:40])


def extract_tokens_from_text(text: str) -> set[str]:
    if not text:
        return set()
    found = set()
    for m in re.finditer(r"[$#]([A-Z]{2,10})\b", text.upper()):
        found.add(m.group(1))
    if config.TRACKED_TOKENS:
        for m in re.finditer(r"\b([A-Z]{2,10})\b", text.upper()):
            sym = m.group(1)
            if sym in config.TRACKED_TOKENS:
                found.add(sym)
    return found


def time_decay(dt: datetime, now: datetime | None = None,
               half_life_hours: float = None) -> float:
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    half = half_life_hours if half_life_hours is not None else config.SHORT_HALF_LIFE_HOURS
    hours = max((now - dt).total_seconds() / 3600, 0)
    return 0.5 ** (hours / half)


def compute_short_scores(conn, max_post_age_hours: int = 24) -> list[dict]:
    """15 分钟榜：最近 N 分钟被抓到的帖子，且帖子本身发布不超过 max_post_age_hours"""
    excluded = config.EXCLUDED_TOKENS or set()
    window_expr = f"-{config.SHORT_WINDOW_MINUTES} minutes"
    age_expr = f"-{max_post_age_hours} hours"

    if excluded:
        placeholders = ",".join("?" * len(excluded))
        sql = f"""
            SELECT p.post_id, p.user_id, p.content, p.likes, p.comments, p.shares,
                   p.posted_at, p.first_seen_at, m.token
            FROM posts p
            JOIN mentions m ON m.post_id = p.post_id
            WHERE p.first_seen_at > datetime('now', ?)
              AND p.posted_at > datetime('now', ?)
              AND m.token NOT IN ({placeholders})
            ORDER BY p.first_seen_at ASC
        """
        params = (window_expr, age_expr, *excluded)
    else:
        sql = """
            SELECT p.post_id, p.user_id, p.content, p.likes, p.comments, p.shares,
                   p.posted_at, p.first_seen_at, m.token
            FROM posts p
            JOIN mentions m ON m.post_id = p.post_id
            WHERE p.first_seen_at > datetime('now', ?)
              AND p.posted_at > datetime('now', ?)
            ORDER BY p.first_seen_at ASC
        """
        params = (window_expr, age_expr)

    cur = conn.execute(sql, params)
    now = datetime.now(timezone.utc)
    agg = defaultdict(lambda: {
        "token": "", "score": 0.0, "mentions": 0,
        "total_likes": 0, "total_comments": 0, "total_shares": 0,
        "unique_posts": set(), "unique_authors": set(),
        "raw_score": 0.0, "author_capped_posts": 0, "similar_posts": 0,
    })
    author_hits = defaultdict(int)
    text_seen = defaultdict(set)

    for row in cur.fetchall():
        token = row["token"]
        author_key = (token, row["user_id"] or "")
        try:
            dt = datetime.fromisoformat(row["first_seen_at"])
        except Exception:
            dt = now
        engagement = (
            row["likes"] * config.WEIGHT_LIKE
            + row["comments"] * config.WEIGHT_COMMENT
            + row["shares"] * config.WEIGHT_SHARE
        )
        raw_score = engagement * time_decay(dt, now, config.SHORT_HALF_LIFE_HOURS)

        weight = 1.0
        author_hits[author_key] += 1
        if author_hits[author_key] > config.MAX_POSTS_PER_AUTHOR_PER_TOKEN:
            weight *= config.AUTHOR_EXTRA_POST_WEIGHT

        sig = _text_signature(row["content"] or "")
        is_similar_text = False
        if sig and sig in text_seen[token]:
            is_similar_text = True
            weight *= config.SIMILAR_TEXT_WEIGHT
        elif sig:
            text_seen[token].add(sig)

        score = raw_score * weight

        bucket = agg[token]
        bucket["token"] = token
        bucket["score"] += score
        bucket["raw_score"] += raw_score
        bucket["mentions"] += 1
        bucket["total_likes"] += row["likes"]
        bucket["total_comments"] += row["comments"]
        bucket["total_shares"] += row["shares"]
        bucket["unique_posts"].add(row["post_id"])
        if row["user_id"]:
            bucket["unique_authors"].add(row["user_id"])
        if author_hits[author_key] > config.MAX_POSTS_PER_AUTHOR_PER_TOKEN:
            bucket["author_capped_posts"] += 1
        if is_similar_text:
            bucket["similar_posts"] += 1

    result = []
    for b in agg.values():
        b["unique_posts"] = len(b["unique_posts"])
        b["unique_authors"] = len(b["unique_authors"])
        b["raw_score"] = round(b["raw_score"], 1)
        result.append(b)
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def compute_composite_scores(conn, current_scores: list[dict],
                              history_window: int = 20) -> list[dict]:
    """基于历史热度数据，给每个当前榜单代币计算"综合热度"和趋势标记

    综合热度 = 当前分 * 0.6 + 历史均分 * 0.3 + 历史峰值分 * 0.1
    趋势标记：比较本轮 vs 上轮的热度
      ↑↑ : >50% 增长
      ↑  : >15% 增长
      —  : ±15% 内
      ↓  : >15% 下降
      ↓↓ : >50% 下降
      🆕 : 刚首次上榜

    参数:
      current_scores: 当前的热度榜（compute_short_scores 的结果）
      history_window: 取最近多少轮历史来算均值
    """
    enhanced = []
    for s in current_scores:
        token = s["token"]
        cur_score = s["score"]
        history = heat_history_recent_for_token(conn, token, history_window)

        if not history:
            # 首次上榜
            composite = cur_score
            trend = "🆕"
            prev_score = None
            avg_score = cur_score
            peak_score = cur_score
            appeared_rounds = 1
        else:
            prev_score = history[0]["score"] if history else None
            avg_score = sum(h["score"] for h in history) / len(history)
            peak_score = max(h["score"] for h in history)
            appeared_rounds = len(history) + 1

            composite = cur_score * 0.6 + avg_score * 0.3 + peak_score * 0.1

            # 趋势
            if prev_score is None or prev_score == 0:
                trend = "🆕"
            else:
                change_pct = (cur_score - prev_score) / prev_score * 100
                if change_pct >= 50:
                    trend = "↑↑"
                elif change_pct >= 15:
                    trend = "↑"
                elif change_pct <= -50:
                    trend = "↓↓"
                elif change_pct <= -15:
                    trend = "↓"
                else:
                    trend = "—"

        enhanced.append({
            **s,
            "composite_score": round(composite, 1),
            "trend": trend,
            "prev_score": round(prev_score, 1) if prev_score is not None else None,
            "avg_history_score": round(avg_score, 1),
            "peak_history_score": round(peak_score, 1),
            "appeared_rounds": appeared_rounds,
        })
    # 按综合热度降序
    enhanced.sort(key=lambda x: x["composite_score"], reverse=True)
    return enhanced


def heat_history_recent_for_token(conn, token: str, limit: int = 20) -> list[dict]:
    """小助手：避免 analyzer 依赖 storage 模块。直接用 SQL 查"""
    cur = conn.execute("""
        SELECT round_number, recorded_at, score
        FROM token_heat_history
        WHERE token = ?
        ORDER BY id DESC
        LIMIT ?
    """, (token, limit))
    return [dict(r) for r in cur.fetchall()]
