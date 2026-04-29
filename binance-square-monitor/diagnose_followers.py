"""
诊断脚本：查看数据库里作者粉丝数分布
回答问题："为什么 258 作者里只有 11 个过关？是粉丝阈值太高，还是粉丝数根本没抓到？"

运行：python diagnose_followers.py
"""
import sqlite3
from collections import Counter


def main():
    conn = sqlite3.connect("binance_square.db")
    conn.row_factory = sqlite3.Row

    # 总数
    total = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    print(f"=== 数据库作者总数：{total} ===\n")

    # 粉丝数分布
    bins = [
        (0, 0, "0 粉丝（字段缺失或真的是 0）"),
        (1, 9, "1-9"),
        (10, 49, "10-49"),
        (50, 99, "50-99"),
        (100, 499, "100-499"),
        (500, 999, "500-999"),
        (1000, 9999, "1000-9999"),
        (10000, 99999, "1万-10万"),
        (100000, 10**9, ">=10万"),
    ]

    print("粉丝数分布：")
    for lo, hi, label in bins:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM authors WHERE followers >= ? AND followers <= ?",
            (lo, hi)
        ).fetchone()[0]
        pct = cnt * 100 / total if total else 0
        bar = "█" * int(pct / 2)
        print(f"  {label:<25} {cnt:>6}  {pct:>5.1f}%  {bar}")

    # 真人判定结果
    print("\n真人判定结果：")
    human = conn.execute("SELECT COUNT(*) FROM authors WHERE is_human=1").fetchone()[0]
    bot = conn.execute("SELECT COUNT(*) FROM authors WHERE is_human=0").fetchone()[0]
    print(f"  真人: {human} ({human*100/total:.1f}%)")
    print(f"  非真人: {bot} ({bot*100/total:.1f}%)")

    # 粉丝=0 的作者是否发过帖子（证明账号真实存在）
    print('\n「粉丝=0」作者中，近 24h 发过帖子的占比：')
    zero_total = conn.execute("SELECT COUNT(*) FROM authors WHERE followers = 0").fetchone()[0]
    zero_active = conn.execute("""
        SELECT COUNT(DISTINCT a.user_id)
        FROM authors a JOIN posts p ON p.user_id = a.user_id
        WHERE a.followers = 0 AND p.posted_at > datetime('now', '-24 hours')
    """).fetchone()[0]
    print(f"  粉丝=0 总数: {zero_total}")
    print(f"  其中近 24h 有发帖: {zero_active} ({zero_active*100/zero_total if zero_total else 0:.1f}%)")

    # 样本：粉丝=0 的作者的最新几条帖子（看互动量）
    print("\n粉丝=0 作者的近期帖子样本（前 10 条按点赞排序）：")
    rows = conn.execute("""
        SELECT a.username, a.followers, p.likes, p.comments, p.content
        FROM authors a JOIN posts p ON p.user_id = a.user_id
        WHERE a.followers = 0
        ORDER BY p.likes DESC
        LIMIT 10
    """).fetchall()
    for r in rows:
        content = (r["content"] or "").replace("\n", " ")[:60]
        print(f"  👤 {r['username'][:20]:<20}  粉丝={r['followers']:<5}  赞={r['likes']:<4} 评={r['comments']:<3}  {content}")

    # 样本：不同粉丝段的代表作者
    print("\n各粉丝段代表作者（取互动最高的 3 个）：")
    for lo, hi, label in [(50, 99, "50-99"), (100, 999, "100-999"),
                          (1000, 9999, "1k-10k"), (10000, 10**9, ">=10k")]:
        print(f"\n  [{label}]")
        rows = conn.execute("""
            SELECT a.username, a.followers, SUM(p.likes) AS total_likes
            FROM authors a JOIN posts p ON p.user_id = a.user_id
            WHERE a.followers >= ? AND a.followers <= ?
            GROUP BY a.user_id
            ORDER BY total_likes DESC
            LIMIT 3
        """, (lo, hi)).fetchall()
        for r in rows:
            print(f"    👤 {r['username'][:25]:<25}  粉丝={r['followers']:<8}  总赞={r['total_likes']}")

    conn.close()
    print("\n=== 诊断结束 ===")


if __name__ == "__main__":
    main()
