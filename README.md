币安广场热度监控 + 模拟交易
本地运行的工具。从币安广场抓帖子算热度，对热门代币拉合约数据做规则评分，并基于这些信号做纸面（模拟）交易。
> ⚠️ **不是投资建议。** 所有评分、verdict、操作建议都是基于规则的客观呈现，不预测未来。加密货币合约风险很高。
---
快速开始
系统要求
Windows 10/11、macOS 11+、Ubuntu 20.04+
Python 3.10+（如果系统没装，Windows 版 `install.bat` 会尝试自动下载安装）
磁盘 ≥ 500MB
网络能访问 binance.com（中国大陆需要科学上网，建议全局/TUN 模式）
安装
Windows：双击 `install.bat`
macOS / Linux：`./install.sh`
跨平台兜底：`python install.py`
安装脚本做的事：
找系统现有 Python 3.10+，找不到就从镜像下载安装（华为云 / 阿里云 / npmmirror / python.org）
创建 `.venv/` 虚拟环境
pip 装依赖（清华镜像 → 默认 PyPI）
装 Playwright Chromium 浏览器内核（淘宝镜像 → 默认源）
依赖：`playwright>=1.40`、`rich>=13.0`、`fastapi>=0.110`、`uvicorn>=0.27`、`websockets>=12.0`。
启动 / 停止
平台	启动	停止
Windows	`start.bat`	`stop.bat`
macOS / Linux	`./start.sh`	`./stop.sh`
启动后会拉起 4 个进程（`worker`、`market_realtime`、`web`、`auto_trader`），并自动用浏览器打开 http://127.0.0.1:8000。
---
项目结构
```
binance-square-monitor/
├── 安装/启动脚本
│   ├── install.bat / install.sh / install.py / install_python.ps1
│   ├── start.bat / start.sh / stop.bat / stop.sh
│   └── manage_processes.py     # 进程管理（启动/停止 4 个进程）
│
├── 核心模块
│   ├── config.py               # 所有配置参数
│   ├── storage.py (834 行)     # SQLite 数据层
│   ├── scraper.py (221 行)     # Playwright 抓取广场帖子
│   ├── filters.py (87 行)      # 真人 / 营销号过滤
│   ├── analyzer.py (221 行)    # 代币提取 + 热度计算
│   ├── market.py (423 行)      # 币安合约 REST API 封装
│   ├── market_realtime.py (283 行)  # 持仓中代币的 WebSocket 实时层
│   ├── signals.py (354 行)     # 多维信号合成 + verdict
│   ├── risk.py (506 行)        # 风控中枢
│   └── trade_logic.py (807 行) # 模拟交易执行
│
├── 后台进程入口
│   ├── worker.py (340 行)      # 数据采集主循环
│   ├── auto_trader.py          # 自动交易循环
│   └── web.py (1758 行)        # FastAPI Web 仪表盘
│
└── 测试（共 42 个，全部通过）
    ├── test_risk.py (17)
    ├── test_entry_v22.py (9)
    ├── test_taker_trend.py (10)
    ├── test_trade_logic.py (3)
    └── test_reset.py (3)
```
---
功能逐项说明
1. 社交热度采集（`scraper.py` + `analyzer.py` + `worker.py`）
抓取机制：
用 Playwright 打开 `https://www.binance.com/en/square`
拦截网页内部 API JSON 响应（不解析 HTML）：扫描所有响应里包含特定字段（`vos` / `list` / `items` / `feedList` / `posts`）的列表，过滤出"看起来像帖子"的对象（含 content / authorName / likeCount 等字段）
持续 `SCRAPE_ROUND_SECONDS`（默认 300 秒）持续滚动 + 周期性刷新页面避免懒加载卡死
真人/机器人过滤（`filters.py`）：
实际只有两条规则（不是我之前说的"粉丝/关注比、注册时长、日发帖数"，那些不存在）：
大 V 直通：`followers >= 100000` 直接通过
用户名"看起来改过"：用 6 个正则模式排除币安默认用户名格式：
`binance/user/binancian/anonymous` 前缀 + 字母数字
20 位以上纯字母数字哈希
6 位以上纯数字
12 位以上十六进制
`0x` 开头地址样
`User\d+` 格式
互动量兜底：作者不达标也行，只要帖子点赞 ≥ `MIN_POST_LIKES` 或评论 ≥ `MIN_POST_COMMENTS`
代币提取：
优先匹配 `$BTC` / `#BTC` 这种带前缀的格式
如果配置了 `TRACKED_TOKENS` 集合，再额外匹配裸符号 `BTC`
从 `EXCLUDED_TOKENS` 里排除误判（USDT / USA / CEO 等）
热度评分：
每条帖子分数 = `点赞 × 1 + 评论 × 3 + 转发 × 5`，乘以指数时间衰减（半衰期默认 0.25 小时 / 15 分钟），再做两层降权：
同作者降权：同一作者对同一代币超过 2 条后，第 3+ 条乘 0.25
相似文案降权：把帖子文本压成签名，重复签名的乘 0.35
输出：每 5 分钟一份"15 分钟热度榜"。
2. 合约市场数据采集（`market.py`）
`get_market_snapshot(token)` 调用币安 fapi 拉这些指标：
字段	来源
价格、15m / 1h / 4h / 24h 涨幅	`klines`
当前 OI、15m / 1h / 4h OI 变化率	`openInterestHist`
资金费率	`premiumIndex`
全网 LSR、大户 LSR	`globalLongShortAccountRatio`、`topLongShortPositionRatio`
主动买卖比 + 趋势（4 根 5m，20 分钟窗口）	`takerlongshortRatio`
±1% 盘口深度 + 不平衡度	`depth`
24h 成交额	`ticker/24hr`
`taker_trend_pct` 的计算方式：最新一根 5m 的 buy/sell ratio，对比前 3 根的平均值，输出百分比变化（正值 = 买盘增强，负值 = 衰退）。
3. 持仓中代币的实时层（`market_realtime.py`）
只对已开仓的代币开 WebSocket 订阅：
端点：`wss://fstream.binance.com/stream?streams=`
订阅两类流：`{symbol}@bookTicker`（最优买卖价）和 `{symbol}@aggTrade`（聚合成交流）
每秒级更新数据库的 `market_realtime` 表，字段包括：60 秒滚动主动买卖比、最优买卖价
作用：让 `update_paper_positions()` 在判断止损/止盈时能用到秒级精度的价格，而不是 5 分钟前的快照。
4. 多维信号评分（`signals.py`）
`signals.analyze()` 返回：
```python
{
  "score": 67,                     # 0-100 综合分
  "verdict": "✅ 看起来健康",        # 5 选 1 + 1 个数据不足
  "tags": [...],
  "notes": [...],
  "oi_divergence": {...}           # OI 与价格背离检测（可选）
}
```
5 个 verdict 字符串（来自 web.py 的 `VERDICT_ORDER`）：
✅ 看起来健康
🎯 值得留意
⚠ 过热预警
📉 信号偏弱
⚪ 中性
数据不足
注意 verdict 字符串的具体决策规则在 `signals.py` 内部，没有简单的"score >= X 就是 Y"对应表。
5. Web 仪表盘（`web.py`）
http://127.0.0.1:8000 — FastAPI + 单文件 HTML/JS。
实际暴露的 API：
路径	方法	功能
`/`	GET	HTML 页面
`/api/leaderboard`	GET	热度榜 + 合约信号合并视图（2s TTL 缓存）
`/api/watchlist`	GET	收藏列表 + 价格追踪
`/api/watchlist/add`	POST	加入收藏（会触发一次手动模拟开仓尝试）

`/api/watchlist/remove`	POST	移除收藏
`/api/watchlist/refresh`	POST	强制刷新收藏价格
`/api/loss_samples`	GET	止损归档样本
`/api/status`	GET	Worker 进度 / 状态
`/api/trading`	GET	账户状态 + 持仓 + 已平仓 + 失败标签统计
`/api/trading/settings`	POST	保存交易设置
`/api/trading/reset`	POST	重置交易账户（清空持仓/锁/归档，保留配置）
性能：`/api/leaderboard` 和 `/api/trading` 用 2 秒 TTL 内存缓存，写操作（保存/重置/收藏）后缓存失效。
界面分区（HTML 内置，单文件）：
采集 Worker 进度 / 第几轮 / 累计帖子数
自动交易面板（开关、模式、初始余额、杠杆、开仓金额）+ 7 个账户指标 + 保存按钮 + 红色重置账户按钮
持仓列表
已平仓代币（含累计胜率）
合约扫描与操作建议
止损失败归档（标签统计 + 样本列表）
收藏 / 观察列表
15 分钟热度榜
6. 自动模拟交易（`auto_trader.py` + `trade_logic.py`）
> 默认 paper 模式。**live 模式被显式拦截**：`auto_trader.py` 第 48-50 行检查 `settings.get("mode") != "paper"` 时直接返回 `live_blocked: True`，不会调用任何真实交易所 API。
`auto_trader.py` 主循环：
每 60 秒调一次 `trade_logic.run_auto_trade_round`
拉候选币（`build_trade_candidates_from_leaderboard`）
对每个 `passed=True` 的候选，开仓前：
抢 `signal_lock`（同一榜单同一币只能开一次）
调 `risk.check_account_risk()` 做账户级检查
调 `risk.compute_position_size()` 算仓位
调 `trade_logic.update_paper_positions()` 更新所有持仓的当前价 / 触发止盈止损
TP 金字塔（v2.3 之后，参数在 config.py 可调）：
档位	触发	默认平仓比例	后续动作
TP1	价 ≥ 入场价 + 1.5R	30%	止损上移到入场价（保本）
TP2	TP1 已触发后，价 ≥ 入场价 + 3.0R	30%	设置初始 trailing stop
跟踪止盈	TP2 触发后剩余仓位，从最高价回撤 2.5%	剩余 40%	全部平仓
止损	价 ≤ stop_loss_price（含 0.15% 假设滑点）	全部	写入失败归档
`R = 入场价 - 止损价`。
7. 风控中枢（`risk.py`）
仓位 sizing（`compute_position_size`）：
风险反推 — `risk_amount = equity × TRADING_RISK_PER_TRADE_PCT%`，`qty = risk_amount / |entry - stop|`。再受 `TRADING_MAX_NOTIONAL_PCT` 上限约束（单笔名义价值不超过净值百分比）。
止损距离（`compute_stop_distance_pct`）：
模式 `atr`：`stop = -(ATR_pct × 1.5)`，clamp 到 `[TRADING_STOP_LOSS_MAX_PCT, TRADING_STOP_LOSS_MIN_PCT]`（默认 [-5%, -1.2%]）
模式 `fixed`：直接用 `TRADING_STOP_LOSS_PCT`
ATR 拿不到 K 线时回退到 fixed
账户级风控（`check_account_risk`，按以下顺序）：
日亏损熔断：当日亏损（已实现+浮动）超 `TRADING_MAX_DAILY_LOSS_PCT`% 净值 → 拒
日交易次数：单日开仓 ≥ `TRADING_MAX_DAILY_TRADES` → 拒
最大持仓数：`open_count >= TRADING_MAX_CONCURRENT_POSITIONS` → 拒（配置为 0 时跳过此检查）
同 token 止损冷却：同 token 上一次止损后 `TRADING_COOLDOWN_MINUTES_AFTER_LOSS` 分钟内 → 拒
板块集中度：同板块（见下方 SECTOR_MAP）持仓 ≥ `TRADING_CORRELATED_LIMIT` → 拒。注意：板块为 "other" 不参与此限制。
`SECTOR_MAP` 是硬编码字典（risk.py:24-40），登记了大约 50 个代币的板块归属：
majors（BTC、ETH）
l2（ARB、OP、STRK 等 6 个）
meme（DOGE、SHIB、PEPE 等 10 个）
ai（FET、AGIX、WLD 等 8 个）
defi（UNI、AAVE 等 5 个）
alt_l1（SOL、AVAX 等 8 个）
其他全部归 "other"
入场质量分档（`evaluate_entry_quality`）：
7 项核心条件（每项可通过/不通过）：
analyzer verdict 包含 "健康"
15m 涨幅 ∈ [`TRADING_ALLOW_15M_PULLBACK_PCT`, `TRADING_MAX_ENTRY_CHANGE_15M`]（默认 [-1.5%, 2%]）
1h 涨幅 ∈ [0, MAX_ENTRY_CHANGE_1H]
OI 15m 增加
OI 1h 增加
OI 4h 增加
主动买卖比 ∈ (MIN_ENTRY_TAKER_RATIO, TRADING_MAX_ENTRY_TAKER_RATIO)
通过后按"通过数 + signal_score"分 3 档：
FULL：7/7 + score ≥ 65
HALF：≥ 5 项 + score ≥ 55
SKIP：其他
入场硬否决（任一命中直接 SKIP，不看上面 7 项）：
verdict 是 "过热预警"
4h 涨幅 > `TRADING_MAX_CHANGE_4H_PCT`（默认 25%）
24h 涨幅 > `TRADING_MAX_CHANGE_24H_PCT`（默认 50%）
资金费率 ≥ `TRADING_MAX_ENTRY_FUNDING_PCT`（默认 0.05%）
散户 LSR ≥ `TRADING_MAX_ENTRY_LSR`（默认 1.7）
主动买卖比 ≥ `TRADING_MAX_ENTRY_TAKER_RATIO`（默认 1.8）
taker 趋势 ≤ `TRADING_MAX_TAKER_DECAY_PCT`（默认 -5%）
聪明钱分歧加分（`TRADING_PREFER_SMART_MONEY_DIVERGENCE = True`）：
如果大户 LSR > 1.5 且 散户 LSR < 0.7，自动升档（skip→half、half→full）。
8. 止损失败归档（`trade_logic._failure_tags`）
每次止损平仓时回溯入场快照，对照退出时的市场状态，从这 9 个标签里选出能命中的：
标签	命中条件
`entry_not_healthy`	入场时 verdict 不含"健康"
`entry_15m_hot`	入场时 15m 涨幅 > MAX_ENTRY_CHANGE_15M
`entry_1h_hot`	入场时 1h 涨幅 > MAX_ENTRY_CHANGE_1H
`entry_funding_hot`	入场时 funding ≥ ARCHIVE_FUNDING_HOT_PCT
`entry_lsr_hot`	入场时 LSR ≥ ARCHIVE_LONG_SHORT_HOT
`oi15_reversed`	退出时 OI 15m 变化 ≤ 0
`oi1h_reversed`	退出时 OI 1h 变化 ≤ 0
`oi4h_reversed`	退出时 OI 4h 变化 ≤ 0
`buy_pressure_faded`	退出时 taker ratio < ARCHIVE_TAKER_WEAK
如果一个都没命中，给一个兜底标签 `price_hit_stop`。
Web 面板会按标签出现频率排序展示，用来反哺策略迭代——出现频率高的标签可以升级为入场硬门槛。
9. 收藏 / 观察列表（`web.py` + `storage.py`）
界面上点 ⭐ 收藏一个代币：
当前价被记为 `anchor_price`（入场价）
数据库表：`watchlist` / `watchlist_entries` / `watchlist_followups`
每次手动点"刷新"按钮 / 或 worker 顺带刷新时，写一条 followup 记录（含浮盈百分比）
当浮亏 ≤ `LOSS_ARCHIVE_THRESHOLD_PCT`（默认 -10%）→ 调 `storage.archive_loss_sample` 归档为反面样本
收藏时还会触发一次手动模拟开仓尝试（`trade_logic.manual_open_on_watch`），但会被 live 模式拦截、被余额/价格不足拦截、被风控拦截
10. 进程管理（`manage_processes.py`）
启动 4 个独立 Python 进程：
`worker.py` — 抓社交数据 + 刷合约快照
`market_realtime.py` — 持仓中代币的 WebSocket 监听
`web.py` — FastAPI Web 服务
`auto_trader.py` — 自动交易循环
进程 PID 写到 `.monitor_processes.json`，`stop` 子命令读这个文件杀进程。
---
配置参数
所有参数在 `config.py`，按用途分组。常用项：
```python
# === 抓取 ===
SCRAPE_ROUND_SECONDS = 300              # 每轮抓取持续秒数
SHORT_WINDOW_MINUTES = 15               # 榜单时间窗口
SHORT_HALF_LIFE_HOURS = 0.25            # 热度衰减半衰期
WEIGHT_LIKE = 1
WEIGHT_COMMENT = 3
WEIGHT_SHARE = 5
HEADLESS = True                         # Playwright 是否后台

# === 自动交易 ===
TRADING_ENABLED = False                 # 总开关（Web 面板可改）
TRADING_MODE = "paper"                  # paper / live（live 被代码硬阻断）
TRADING_INITIAL_BALANCE = 1000.0
TRADING_LEVERAGE = 2
TRADING_ORDER_AMOUNT_USDT = 50

# === 仓位 sizing ===
TRADING_SIZING_MODE = "risk_based"
TRADING_RISK_PER_TRADE_PCT = 1.0
TRADING_MAX_NOTIONAL_PCT = 50.0

# === 止损 ===
TRADING_STOP_MODE = "atr"               # atr / fixed
TRADING_ATR_PERIOD = 14
TRADING_ATR_STOP_MULTIPLIER = 1.5
TRADING_STOP_LOSS_MIN_PCT = -1.2
TRADING_STOP_LOSS_MAX_PCT = -5.0
TRADING_STOP_SLIPPAGE_PCT = 0.15

# === TP 金字塔 ===
TRADING_TP1_R = 1.5
TRADING_TP1_CLOSE_PCT = 30.0
TRADING_TP2_R = 3.0
TRADING_TP2_CLOSE_PCT = 30.0
TRADING_TRAIL_REMAIN_PCT = 40.0
TRADING_TRAIL_CALLBACK_PCT = 2.5

# === 入场硬门槛 ===
TRADING_MAX_CHANGE_4H_PCT = 25.0
TRADING_MAX_CHANGE_24H_PCT = 50.0
TRADING_MAX_ENTRY_FUNDING_PCT = 0.05
TRADING_MAX_ENTRY_LSR = 1.7
TRADING_MAX_ENTRY_TAKER_RATIO = 1.8
TRADING_MAX_TAKER_DECAY_PCT = -5.0

# === 入场区间 ===
TRADING_MAX_ENTRY_CHANGE_15M = 2.0
TRADING_ALLOW_15M_PULLBACK_PCT = -1.5

# === 账户级熔断 ===
TRADING_MAX_DAILY_LOSS_PCT = 5.0
TRADING_MAX_DAILY_TRADES = 15
TRADING_MAX_CONCURRENT_POSITIONS = 0    # 0 = 不限
TRADING_COOLDOWN_MINUTES_AFTER_LOSS = 30
TRADING_CORRELATED_LIMIT = 2

# === 调试 ===
TRADING_DEBUG = True                    # 打印开仓拒绝原因
```
---
数据库（`binance_square.db`）
SQLite + WAL 模式。关键表：
表	内容
`posts`	抓到的帖子（id、内容、点赞/评论/转发数、发帖时间）
`authors`	作者（粉丝数、是否大 V、is_human 标志）
`mentions`	帖子-代币的多对多映射
`snapshots`	合约快照（每个代币一条最新快照）
`market_realtime`	实时层数据（持仓中代币的最新 1 行）
`heat_history`	热度榜历史
`trade_settings`	当前交易设置
`trade_positions`	持仓和已平仓记录
`trade_signal_locks`	防重复开仓锁
`trade_loss_archive`	止损归档（含失败标签）
`watchlist` / `watchlist_entries` / `watchlist_followups`	收藏 + 价格追踪
---
测试
```bash
.venv/Scripts/python.exe test_risk.py          # Windows
.venv/bin/python test_risk.py                  # Unix

# 共 5 个测试文件，42 个用例
test_risk.py            17 个 — 风控逻辑（含板块集中度、熔断、聪明钱等）
test_entry_v22.py        9 个 — 入场逻辑（含硬否决、聪明钱分歧）
test_taker_trend.py     10 个 — taker 趋势计算与硬否决
test_trade_logic.py      3 个 — TP/止损完整闭环
test_reset.py            3 个 — 重置账户功能
```
---
调试
`manage_processes.py start` 把 4 个进程开成后台，看不到 stderr 日志。想看 `[trade-debug] REJECT` 之类的输出，单独前台跑：
```bash
.venv/Scripts/python.exe auto_trader.py
```
Worker 抓不到帖子（"本轮入库 0 条"）：把 `config.py` 里 `HEADLESS = True` 改 `False`，会弹出 Chromium 窗口便于看 Network。
---
已知局限
明确说明，避免误解：
没有真实下单功能。`live` 模式在 `auto_trader.py` 和 `trade_logic.manual_open_on_watch` 都被显式拦截，会返回 `live_blocked: True`。要做实盘需要自己实现下单接入。
没有回测系统。当前所有数据都是实时抓取 + 实时模拟交易，没有 K 线重放回测。
板块集中度限制有缺口：`SECTOR_MAP` 只登记了约 50 个代币，其它都归 "other"，"other" 不受板块限制。
依赖币安网页结构：`scraper.py` 通过拦截 API 响应工作，币安如果改了 API 路径或字段名，可能失效。可以把 `HEADLESS=False` 后用 F12 排查。
`signals.py` 没在本次审计中详细拆：score 的具体打分公式、verdict 的决策树细节请直接看代码。
---
免责声明
不是投资建议，不预测市场
加密合约交易高风险，5× 杠杆下 -2% 价格 = -10% 保证金
默认 paper 模拟，无任何真实下单逻辑
任何盈亏自负
详见 LICENSE。
---
License: MIT
