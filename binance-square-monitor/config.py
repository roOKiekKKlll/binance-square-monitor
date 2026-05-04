"""配置文件：可根据实际情况调整阈值"""

# === 真人 / 内容质量过滤 ===
MIN_FOLLOWERS = 50                # 粉丝数下限（feed 接口拿不到普通用户粉丝，实际主要作用于大 V）
MIN_ACCOUNT_AGE_DAYS = 30
MAX_POSTS_PER_DAY = 50
MIN_FOLLOWER_FOLLOWING_RATIO = 0.05

# 帖子级质量过滤：粉丝数拿不到时，用互动量代替
# 帖子满足以下任一即可进入榜单：
#   - 作者粉丝 >= 10 万（大 V，肯定已过 filters.is_likely_human）
#   - 点赞 >= MIN_POST_LIKES
#   - 评论 >= MIN_POST_COMMENTS
MIN_POST_LIKES = 3
MIN_POST_COMMENTS = 2

# === 社交去重 / 防刷屏 ===
MAX_POSTS_PER_AUTHOR_PER_TOKEN = 2   # 同一作者对同一代币，前 N 条按原分
AUTHOR_EXTRA_POST_WEIGHT = 0.25      # 超过 N 条后的热度降权系数
SIMILAR_TEXT_WEIGHT = 0.35           # 相似文案重复出现时的热度降权系数

# === 热度计算权重 ===
WEIGHT_LIKE = 1
WEIGHT_COMMENT = 3
WEIGHT_SHARE = 5

# === 抓取参数 ===
# 现在是"一轮 = 5 分钟持续抓取"，所以 INTERVAL = 每轮时长
SCRAPE_ROUND_SECONDS = 300      # 每轮持续 5 分钟
HEADLESS = True                 # 仪表盘模式下建议 True（浏览器别挡视线）
SCROLL_PAUSE_SECONDS = 3        # 每次滚动之间等待
SCROLL_RESET_EVERY = 40         # 每滚动 N 次后刷新一次页面（防止懒加载卡死）

# === 数据库 ===
DB_PATH = "binance_square.db"

# === 代币白名单/黑名单 ===
TRACKED_TOKENS = set()

EXCLUDED_TOKENS = {
    "BTC", "ETH", "SOL",
    "USDT", "USDC", "U", "USD1", "USTC",
    "DAI", "BUSD", "TUSD", "USDE", "FDUSD", "PYUSD",
    "SPY", "SPYON", "QQQ", "GLD", "NVDA", "TSLA",
    "DM", "DEX", "CEX", "NFT", "AI", "USA", "UK", "EU",
    "CEO", "CFO", "CTO", "ATH", "ATL",
    "TP", "SL", "ROI", "APY", "APR", "TVL", "DCA",
    "FOMO", "FUD", "HODL", "FYI", "IMO", "AMA",
}

# === 15 分钟榜单 ===
SHORT_WINDOW_MINUTES = 15       # 榜单时间窗口
SHORT_HALF_LIFE_HOURS = 0.25    # 热度衰减半衰期
TOP_N_SHORT = 20                # 榜单显示前 N

# === Web 仪表盘 ===
WEB_HOST = "127.0.0.1"
WEB_PORT = 8000

# === 合约分析 ===
ENABLE_MARKET_ANALYSIS = True
MARKET_ANALYSIS_MAX = 30        # 榜单自动分析前 N 个有合约的代币
WATCHLIST_REFRESH_SECONDS = 300 # 观察列表数据刷新间隔
WATCHLIST_REALTIME_REFRESH_SECONDS = 1   # Web 观察列表自动刷新合约快照
REALTIME_WATCHLIST_POLL_SECONDS = 5      # market_realtime.py 检查观察列表变化间隔
REALTIME_CACHE_FLUSH_SECONDS = 1         # market_realtime.py 写入缓存间隔

# === 行情确认 / 流动性 ===
DEPTH_LIMIT = 100
DEPTH_RANGE_PCT = 1.0           # 统计正负 1% 盘口深度
MIN_DEPTH_1PCT_USD = 100000     # 1% 单侧深度低于该阈值则降权
MAX_SPREAD_PCT = 0.20           # 买一/卖一价差超过该阈值则降权

# === 收藏代币的学习反馈 ===
LOSS_ARCHIVE_THRESHOLD_PCT = -10.0  # 浮亏超过这个阈值就归档为负面样本（-10 即亏损 10%）
COMPOSITE_HEAT_TOP_N = 20            # 综合热度榜显示前 N
COMPOSITE_HISTORY_WINDOW = 20        # 综合热度参考最近 N 轮历史

# 同一 watchlist 代币追加 followup 的最小间隔（秒）。
# worker 每轮 5min 写一次足以反映长期趋势；web 端的手动刷新（/api/watchlist/refresh）
# 因为可能被反复点击，叠加节流避免短时间写大量重复记录污染样本。
WATCHLIST_FOLLOWUP_MIN_INTERVAL_SECONDS = 60

# === 自动交易（默认模拟，不会真实下单）===
TRADING_ENABLED = False
TRADING_MODE = "paper"               # paper / live
TRADING_INITIAL_BALANCE = 100.0
TRADING_LEVERAGE = 20

# --- 仓位 sizing（专业量化风格：先定风险，再反推仓位）---
# 默认使用"风险优先"模式：每笔交易最多亏损账户净值的 RISK_PER_TRADE_PCT
# 仓位 = 风险金额 / |entry - stop|
TRADING_SIZING_MODE = "risk_based"       # risk_based / fixed_margin
TRADING_RISK_PER_TRADE_PCT = 5.0         # 每笔最大风险占账户净值的比例（%）
TRADING_ORDER_AMOUNT = 50.0              # fixed_margin 模式下的固定保证金（兼容旧配置）
TRADING_MIN_NOTIONAL = 5.0               # 名义价值下限，低于此值不开仓（避免滑点放大）
TRADING_MAX_NOTIONAL_PCT = 50.0          # 单笔名义价值不超过账户净值的百分比

# --- 风控硬限制 ---
TRADING_MAX_CONCURRENT_POSITIONS = 6     # 最多同时 5 个仓位
TRADING_MAX_DAILY_LOSS_PCT = 25.0        # 当日浮动+已实现亏损超该百分比则熔断停机（$100账户=亏$25）
TRADING_MAX_DAILY_TRADES = 50            # 当日最多开仓次数
TRADING_COOLDOWN_MINUTES_AFTER_LOSS = 30 # 同一 token 止损后冷却期（分钟）
TRADING_CORRELATED_LIMIT = 2             # 相关度高的板块同向仓位上限（目前按粗分类）

# --- 止损：波动率自适应（ATR 风格）---
# 止损距离 = max(MIN_STOP_PCT, ATR_MULTIPLIER * 最近 N 根 K 线的 ATR%)
# 若拿不到 K 线，回退到 TRADING_STOP_LOSS_PCT
TRADING_STOP_MODE = "atr"                # atr / fixed
TRADING_ATR_PERIOD = 14                  # 用多少根 1h K 线算 ATR
TRADING_ATR_STOP_MULTIPLIER = 1.5        # 止损 = 1.5 × ATR
TRADING_STOP_LOSS_PCT = -2.0             # 固定模式 或 ATR 回退时使用
TRADING_STOP_LOSS_MIN_PCT = -1.2         # ATR 模式下止损下限（防止太紧）
TRADING_STOP_LOSS_MAX_PCT = -5.0         # ATR 模式下止损上限（防止风险过大）

# --- 止盈（基于 R 值阶梯）---
# v2.3 改为"金字塔"结构：让赢家跑得更远，减少早砍仓
TRADING_TP1_R = 1.2                      # +1.2R 平 40%，先回收一部分利润
TRADING_TP1_CLOSE_PCT = 40.0
TRADING_TP2_R = 3.0                      # +3R 再平 30%（原 2R，给趋势空间）
TRADING_TP2_CLOSE_PCT = 30.0
TRADING_TRAIL_REMAIN_PCT = 40.0          # 剩余 40% 交给跟踪止盈（原 20%）
TRADING_TRAIL_CALLBACK_PCT = 2.5         # 从高点回撤 2.5% 触发（原 1.5%，防震荡误扫）

# --- 入场质量（评分 + 分档开仓）---
# 不再是所有条件硬 AND，改为分档：
#   FULL (100% 仓位): 所有核心条件通过 + 信号分 >= FULL 阈值
#   HALF (50% 仓位): 至少通过 core_required 的 N 项 + 信号分 >= HALF 阈值
#   SKIP: 其他
TRADING_ENTRY_MODE = "tiered"            # tiered / strict (原来的全AND)
TRADING_SIGNAL_FULL_THRESHOLD = 72       # signals.analyze 返回的 score 阈值（满仓）
TRADING_SIGNAL_HALF_THRESHOLD = 62       # 半仓阈值
TRADING_CORE_REQUIRED_PASS_COUNT = 6     # 7 项核心条件里至少通过几项才可半仓

# --- 追高保护 ---
# 即便 15m/1h 涨幅在区间内，若 4h/24h 已经大幅拉升，也视为追高
TRADING_MAX_CHANGE_4H_PCT = 25.0         # 4h 涨幅超此值则拒绝（追高）
TRADING_MAX_CHANGE_24H_PCT = 50.0        # 24h 涨幅超此值则拒绝

# --- 入场时机硬门槛（v2.2 新增，基于失败归档数据反哺）---
# 观察到历史亏损样本里 funding_hot(27)/lsr_hot(38)/buy_pressure_faded(73) 标签高频命中，
# 说明这些情况下入场就是"派发顶"。做成硬否决。
TRADING_MAX_ENTRY_FUNDING_PCT = 0.05     # 资金费率 >= 0.05%/8h 视为多头拥挤，不开仓
TRADING_MAX_ENTRY_LSR = 1.5              # 散户多空比 >= 1.5 视为情绪过热，不开仓
TRADING_MAX_ENTRY_TAKER_RATIO = 1.6      # 主动买卖比 >= 1.6 视为买盘透支，不开仓
                                          # （配合 MIN_ENTRY_TAKER_RATIO=1.15，形成 [1.15, 1.8] 区间）

# --- taker 趋势过滤（v2.4 新增，对应 buy_pressure_faded 标签）---
# 即使 taker_ratio 当前在允许区间，如果最近 20m 的 taker 趋势明显衰退，
# 说明买盘正在"消退顶部"，这种情况入场后很快会被买盘消失拖到止损。
# taker_trend_pct 定义：(最新 5m 的 taker_ratio) vs (较早 15m 平均)，负值表示衰退
TRADING_MAX_TAKER_DECAY_PCT = -5.0       # v2.5：-10% → -5%。历史数据显示 -10% 太宽松，
                                          # buy_pressure_faded 标签仍然 67 次高频命中。
                                          # 收紧到 -5%，即"任何明显衰退都不入场"。

# --- 入场时机软门槛 ---
# 15m 涨幅改窄：不要在刚急拉的 K 线顶部买入
TRADING_MAX_ENTRY_CHANGE_15M = 1.2       # 15m 涨幅不超过 1.2%，减少追涨
                                          # 理想入场：1h/4h 正向 + 15m 缓和或轻微回调

# 允许"小幅回调入场"（比硬要求 15m > 0 更现实）
TRADING_ALLOW_15M_PULLBACK_PCT = -1.5    # 15m 允许回调到 -1.5% 以内仍视为有效（买回调）
TRADING_ENTRY_MAX_PRICE_DRIFT_PCT = 1.5  # 信号价与下单前实时价最大允许偏离（%），超出则放弃本次开仓

# 大户/散户分歧加分（已存在于 signals，这里显式拉出来做入场参考）
TRADING_PREFER_SMART_MONEY_DIVERGENCE = True  # top_lsr > 1.5 且 lsr < 0.7 时优先开仓

# --- 滑点 / 订单 ---
TRADING_ASSUMED_SLIPPAGE_PCT = 0.05      # 模拟交易假设的市价滑点（入场）
TRADING_STOP_SLIPPAGE_PCT = 0.15         # 止损触发时的假设滑点（通常更坏）
TRADING_LIMIT_ORDER_TIMEOUT_SECONDS = 10

# --- 维护 ---
TRADING_SIGNAL_LOCK_RETENTION_HOURS = 72 # signal_lock 表保留时长

# --- 调试 / 日志 ---
TRADING_DEBUG = True             # 打印开仓拒绝原因，找不到"为啥不开仓"时开这个
                                 # 稳定后可以关掉减少日志噪音

# === 实盘交易安全参数 ===
LIVE_MAX_POSITION_SIZE_USD = 200.0        # 单笔最大名义价值硬限（USD）
LIVE_MAX_TOTAL_EXPOSURE_USD = 600.0      # 总敞口硬限（USD）
LIVE_ORDER_RETRY_COUNT = 3               # 下单失败重试次数
LIVE_ORDER_RETRY_DELAY_S = 1.0           # 重试间隔（秒）
LIVE_STOP_ORDER_RETRY_COUNT = 2          # 保护单最多短重试；失败后只记录提醒，避免循环刷手续费
LIVE_STOP_ORDER_RETRY_DELAY_S = 1.0      # 止损挂单重试间隔（秒）
LIVE_STOP_MARK_PRICE_BUFFER_PCT = 0.2    # 止损价若已高于标记价，则下移到标记价下方 0.2%
LIVE_TAKE_PROFIT_MARK_PRICE_BUFFER_PCT = 0.2  # 止盈价若已低于标记价，则上移到标记价上方 0.2%
LIVE_EMERGENCY_CLOSE_ON_STOP_TRANSIENT_FAILURE = False  # 止损失败不自动平仓，只记录提醒
LIVE_AUTO_REPAIR_MISSING_STOP = False     # 缺失止损不后台无限补挂，避免反复扰动实盘
LIVE_STOP_REPAIR_MIN_INTERVAL_S = 10     # 无止损仓位补挂止损的最小间隔（秒）
LIVE_GUARDIAN_ENABLED = True             # 开启裸单守护巡检（每隔一段时间补挂缺失保护单）
LIVE_GUARDIAN_INTERVAL_S = 600           # 守护巡检间隔（秒），默认 10 分钟
LIVE_GUARDIAN_REPAIR_MIN_INTERVAL_S = 120  # 同一仓位同类补挂最小间隔（秒），防止重复打单
LIVE_RECONCILE_INTERVAL_S = 60           # DB vs 交易所对账间隔（秒）
LIVE_TRAILING_STOP_MIN_UPDATE_S = 30     # 追踪止损最小更新间隔（秒）
LIVE_TRAILING_STOP_MIN_IMPROVEMENT_PCT = 0.3  # 止损至少改善这么多才更新（%）

# --- 手动开仓（收藏触发）的风控豁免 ---
# 手动收藏是用户强意愿信号，某些账户级限制可以跳过：
MANUAL_BYPASS_MAX_CONCURRENT = True   # 手动开仓不受 MAX_CONCURRENT_POSITIONS 限制
MANUAL_BYPASS_SECTOR_LIMIT = True     # 手动开仓不受板块集中度限制
MANUAL_BYPASS_COOLDOWN = False        # 手动开仓仍受止损冷却期约束（建议 False 保护）
# 注意：日亏损熔断永远不豁免，这是最后的保命线
