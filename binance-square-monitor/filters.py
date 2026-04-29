"""过滤逻辑

真人识别策略（按优先级）：
1. 大 V（10 万+ 粉丝徽章）→ 直接通过
2. 用户名"看起来被修改过"（不是币安默认格式）→ 算真人
3. 帖子互动量达标（点赞/评论）→ 作为补充信号

机器人默认用户名的典型特征：
- Binance-Xxxxxx / User-Xxxxxx / BinanceUser-Xxxxxx
- 纯数字（至少 6 位，如 "12345678"）
- 长串十六进制或 base58 乱码
- 全大写字母 + 数字组合且无空格连字符（如 "ABC12345XYZ"）
"""
import re
from datetime import datetime, timezone
import config


# 币安默认用户名模式（不区分大小写）
BOT_NAME_PATTERNS = [
    # 前缀关键词 + 数字/字母后缀，允许前缀之间拼接（如 binanceuser-xxx）
    re.compile(r"^(binance|user|binancian|anonymous)+[-_]?[a-z0-9]{3,}$", re.IGNORECASE),
    re.compile(r"^[a-z0-9]{20,}$", re.IGNORECASE),      # 20 位以上的纯字母数字（通常是乱码哈希）
    re.compile(r"^\d{6,}$"),                            # 6 位以上纯数字
    re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE),      # 12 位以上十六进制
    re.compile(r"^0x[0-9a-f]{6,}$", re.IGNORECASE),     # 以 0x 开头的地址样
    re.compile(r"^user\d+$", re.IGNORECASE),            # User123
]


def is_default_bot_name(username: str) -> bool:
    """判断用户名是否看起来是币安系统默认生成的（即"没改过"）"""
    if not username:
        return True  # 空用户名当默认
    name = username.strip()
    if not name:
        return True
    # 去掉两端空格后再判断
    for pat in BOT_NAME_PATTERNS:
        if pat.match(name):
            return True
    return False


def has_customized_name(author: dict) -> bool:
    """反过来的判定：用户名看起来被修改过"""
    return not is_default_bot_name(author.get("username") or "")


def is_verified_kol(author: dict) -> bool:
    """是否是被币安认证为 10 万+ 粉丝的大 V（userLabels 里抓到的）"""
    return (author.get("followers") or 0) >= 100000


def is_likely_human(author: dict) -> bool:
    """
    向后兼容：authors 表里存的 is_human 标志

    新策略：满足任一即可
    - 是大 V（有 10万+ 粉丝徽章）
    - 用户名看起来被改过（不是机器人默认格式）
    """
    if is_verified_kol(author):
        return True
    if has_customized_name(author):
        return True
    return False


def post_passes_quality(post: dict, author: dict) -> bool:
    """帖子层面的过滤：综合作者特征 + 帖子互动量

    进榜单条件（满足其一即可）：
    - 作者是认证大 V（10 万+ 粉丝）
    - 作者用户名不是机器人默认格式（改过名的真人）
    - 帖子点赞 >= config.MIN_POST_LIKES
    - 帖子评论 >= config.MIN_POST_COMMENTS
    """
    if is_verified_kol(author):
        return True
    if has_customized_name(author):
        return True
    if (post.get("likes") or 0) >= config.MIN_POST_LIKES:
        return True
    if (post.get("comments") or 0) >= config.MIN_POST_COMMENTS:
        return True
    return False
