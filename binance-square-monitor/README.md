# 币安广场热度监控 + 量化模拟交易

本地运行的加密货币市场分析工具，基于 **币安广场（Binance Square）社交热度** + **币安永续合约链上指标** 进行综合信号分析，并提供**纸面（模拟）交易**功能。

> ⚠️ **本程序不是投资建议**。所有信号、评分、"健康判断"都是基于规则的客观数据呈现，**不预测市场未来走向**。加密货币合约交易风险极高，仓位翻倍亏损也可能。仅供个人研究学习。

---

## 目录

- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [所有功能详解](#所有功能详解)
- [配置参数详解](#配置参数详解)
- [运维 / 调试](#运维--调试)
- [免责声明](#免责声明)

---

## 快速开始

### 系统要求

- **操作系统**：Windows 10/11、macOS 11+、Ubuntu 20.04+
- **Python**：3.10 或更新版本（[下载地址](https://www.python.org/downloads/)）
- **磁盘空间**：≥ 500MB（依赖 + Chromium 浏览器内核）
- **网络**：能访问 binance.com 和 pypi.org

### 安装（三选一）

#### 方式 1：Windows 双击

```
双击 install.bat
```

#### 方式 2：macOS / Linux 终端

```bash
chmod +x install.sh
./install.sh
```

#### 方式 3：跨平台 Python 安装

```bash
python install.py
```

安装脚本自动完成 5 件事：

1. 检查 Python 版本（需要 3.10+）
2. 创建独立虚拟环境 `.venv/`
3. 升级 pip
4. 安装 Python 依赖
5. 安装 Playwright Chromium 浏览器内核（约 150MB）

### 启动

```bash
# Windows
start.bat

# macOS / Linux
./start.sh
```

启动后会：
- 后台运行 4 个 Python 进程（`worker` / `market_realtime` / `web` / `auto_trader`）
- 自动用默认浏览器打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 停止

```bash
# Windows
stop.bat

# macOS / Linux
./stop.sh
```

---

## 项目结构

```
binance-square-monitor/
├── 启动脚本
│   ├── install.bat / install.sh / install.py    # 一键安装
│   ├── start.bat   / start.sh                   # 启动 4 个进程
│   ├── stop.bat    / stop.sh                    # 停止所有进程
│   └── manage_processes.py                      # 进程管理器
│
├── 核心模块
│   ├── config.py            # 所有配置参数
│   ├── storage.py           # SQLite 数据层
│   ├── scraper.py           # Playwright 抓取广场帖子
│   ├── filters.py           # 真人 / 营销号过滤
│   ├── analyzer.py          # 热度计算 + 综合评分
│   ├── market.py            # 币安合约 API 封装
│   ├── market_realtime.py   # 实时盘口 / Taker / 成交流
│   ├── signals.py           # 多维信号合成 + verdict
│   ├── risk.py              # 风控中枢（仓位、止损、熔断、板块）
│   └── trade_logic.py       # 模拟交易执行
│
├── 后台进程
│   ├── worker.py            # 数据采集主循环（5min/轮）
│   ├── market_realtime.py   # 实时行情监控
│   ├── auto_trader.py       # 自动交易循环
│   └── web.py               # FastAPI Web 仪表盘
│
├── 测试
│   ├── test_risk.py
│   ├── test_entry_v22.py
│   ├── test_taker_trend.py
│   ├── test_trade_logic.py
│   └── test_reset.py
│
└── 文档
    ├── README.md            # 本文件
    ├── LICENSE              # MIT
    ├── requirements.txt
    └── CHANGELOG-*.md       # 版本变更记录
```

---

## 所有功能详解

### 一、社交热度采集

**目标**：自动追踪币安广场上"哪些代币正在被讨论"，并量化"被谁讨论、讨论得多火"。

**怎么做**：

1. **抓取**（`scraper.py`）：用 Playwright 打开币安广场页面，**拦截网页内部 API 的 JSON 响应**（不是解析 HTML，更稳定）。每 5 分钟一轮，持续滚动 5 分钟，每轮捕获 200-700 条帖子。

2. **真人/营销号过滤**（`filters.py`）：通过下面几个特征排除机器人和营销号：
   - 粉丝数 / 关注数比例
   - 账号注册时长
   - 日发帖数（疯狂刷帖的不算）
   - 是否官方认证 / 是否大 V

3. **代币提取**（`analyzer.py`）：从帖子文本里用正则提取 `$BTC`、`#ETH`、裸符号 `BTC` 等格式，过滤掉 `EXCLUDED_TOKENS`（USDT、USA、CEO 这种误判）。

4. **热度计算**：每条帖子分数 = `点赞 × 1 + 评论 × 3 + 转发 × 5`，再乘以**时间衰减**（半衰期默认 0.25 小时 = 15 分钟），让"现在最热"的内容权重最大。

5. **去重 / 防刷屏**：
   - 同一作者同一代币最多算 2 条原分，后续降权 0.25
   - 相似文案降权 0.35

**输出**：每 5 分钟一份"15 分钟热度榜"，排前 N 的代币进入下一阶段。

### 二、合约市场分析

**目标**：对热度榜里的代币，**判断它的链上情况是否健康**。社交热度高 ≠ 价格会涨。

**采集的指标**（`market.py`，对每个代币调用币安 fapi）：

| 维度 | 指标 |
|---|---|
| **价格** | 当前价、15m / 1h / 4h / 24h 涨幅 |
| **持仓量 (OI)** | 当前 OI、15m / 1h / 4h OI 变化率 |
| **资金费率** | funding rate（多头拥挤度） |
| **多空比 LSR** | 全网账户多空比 + 大户持仓多空比 |
| **主动买卖** | 5m / 15m / 20m 主动买入 vs 主动卖出 + 趋势 |
| **盘口深度** | ±1% 范围内的买卖盘流动性 |
| **24h 成交额** | 流动性总览 |

**实时层**（`market_realtime.py`）：对持仓中的代币额外开 WebSocket，每秒级采集：
- 60 秒滚动主动买卖比
- 实时盘口最优买卖价
- 用于精确止损止盈触发

### 三、综合信号评分

**目标**：把 N 个原始指标合成一个 **0-100 的综合分**，外加一个"判断 verdict"。

**`signals.py` 怎么打分**：

每个维度按区间打加减分（例如 `funding rate < 0.005% +5 分`、`> 0.05% 视为极端拥挤 -10 分`），最后输出：

```python
{
  "score": 67,                      # 0-100
  "verdict": "✅ 看起来健康",         # 或 "⚠️ 过热预警" / "🎯 值得留意" / "📉 信号偏弱" / "⚪ 中性"
  "tags": ["1h:温和涨", "OI:激增+涨"],
  "notes": ["1h 涨幅 +4.2%", ...]
}
```

**Verdict 决策表**：
- `score >= 65` 且有"温和涨" / "OI 激增" 标签 → `✅ 看起来健康`
- `score >= 55` → `🎯 值得留意`
- `score <= 35` → `📉 信号偏弱`
- 命中过热标签且 `score < 45` → `⚠️ 过热预警`

### 四、Web 仪表盘

[http://127.0.0.1:8000](http://127.0.0.1:8000) — `web.py`，FastAPI + 单文件 HTML/JS。

**界面分区**（从上到下）：

1. **采集 Worker 进度条**：实时显示当前抓取进度、轮次
2. **自动交易面板**：
   - 6 个开关/输入：自动交易开关、模式（paper/live）、初始余额、杠杆倍数、开仓金额
   - 7 个状态卡片：初始金额、账户权益、剩余金额、占用保证金、已实现盈亏、浮动盈亏
   - **🔴 重置账户**：一键清空所有持仓、已平仓记录、止损归档（保留配置）
3. **持仓代币**：当前 OPEN 状态的仓位
4. **已平仓代币**：累计胜率、总盈亏、每笔记录
5. **合约扫描与操作建议**：当前符合自动开仓规则的候选币
6. **止损失败归档**：所有亏损样本的失败原因标签统计（用于策略迭代）
7. **观察列表**：手动收藏的代币 + 价格追踪
8. **15 分钟热度榜**：完整代币列表 + 综合信号

**性能优化**：
- `/api/leaderboard` 和 `/api/trading` 有 2 秒 TTL 内存缓存
- 任何写操作（收藏 / 改设置 / 重置）后缓存自动失效

### 五、自动模拟交易

> 默认 **paper** 模拟交易，所有"下单"只是写数据库，不会调用任何真实交易所 API。

**`auto_trader.py` 主循环**（每 60 秒一次）：

1. 读 `trading_settings`，没启用就跳过
2. 调用 `trade_logic.build_trade_candidates_from_leaderboard()` 拿候选币
3. 对每个 `passed=True` 的候选币：
   - 抢一个 `signal_lock`（同一榜单同一币只能开一次）
   - 调 `risk.check_account_risk()` 做账户级检查
   - 调 `risk.compute_position_size()` 算仓位
   - 写入 `trade_positions` 表
4. 调 `trade_logic.update_paper_positions()` 更新所有持仓的当前价 / 止盈止损触发

**TP 金字塔**（v2.3 之后）：

| 档位 | 触发条件 | 平仓比例 | 备注 |
|---|---|---|---|
| TP1 | 价格 ≥ 入场价 + 1.5R | 30% | 触发后止损上移到保本 |
| TP2 | 价格 ≥ 入场价 + 3.0R | 30% | |
| 跟踪止盈 | 从最高价回撤 2.5% | 剩余 40% | "让赢家跑" |
| 止损 | 价格 ≤ ATR 自适应止损价 | 全部 | 含假设滑点 |

其中 1R = `入场价 - 止损价`。

### 六、专业风控系统

**`risk.py` 是整个项目的核心**：

**1. 仓位 sizing 是"风险反推"**：
```
风险金额 = 账户权益 × 1%
仓位数量 = 风险金额 / |入场价 - 止损价|
```
止损越远仓位越小，止损越紧仓位越大，**单笔风险恒定**。

**2. ATR 自适应止损**：
```
止损距离 = max(1.2%, min(5%, 1.5 × ATR))
```
小币波动大止损就宽，主流币波动小止损就紧。

**3. 多重账户级熔断**：
- ✅ 日亏损熔断（超 5% 净值停开新仓）
- ✅ 日交易次数（最多 15 笔）
- ✅ 最大持仓数（可设 0 = 不限）
- ✅ 同币种止损冷却期（30 分钟）
- ✅ 板块集中度（meme / l2 / ai 等同板块最多 2 个仓位）

**4. 入场质量分档**：
- **FULL（满仓）**：7/7 项核心条件 + 综合分 ≥ 65
- **HALF（半仓）**：5+/7 项 + 综合分 ≥ 55
- **SKIP**：其他

**5. 入场硬否决**（任一命中直接 skip）：
- ❌ 4h 涨幅 > 25% / 24h 涨幅 > 50%（追高）
- ❌ funding rate ≥ 0.05%/8h（多头拥挤）
- ❌ 散户多空比 ≥ 1.7（情绪过热）
- ❌ 主动买卖比 ≥ 1.8（买盘透支）
- ❌ Taker 趋势衰退 ≥ 5%（买盘消退）
- ❌ verdict = "过热预警"

**6. 聪明钱分歧加分**：
- 大户多空比 > 1.5 + 散户多空比 < 0.7 → 自动升档（`half → full`）

### 七、止损失败学习

每次仓位被止损平仓时，`trade_logic._failure_tags()` 会**回溯入场时的 snapshot**，给这笔失败打上"诊断标签"：

| 标签 | 含义 |
|---|---|
| `entry_funding_hot` | 入场时 funding > 0.05% |
| `entry_lsr_hot` | 入场时 LSR > 2.0 |
| `buy_pressure_faded` | 入场后 taker ratio 跌破 1.15 |
| `entry_not_healthy` | 入场时 verdict 不健康 |
| `oi15_reversed` / `oi1h_reversed` / `oi4h_reversed` | 入场后 OI 反转下跌 |
| `price_hit_stop` | 单纯被价格击穿（无明显诱因） |

这些标签会显示在 Web 面板的"止损失败归档"区域。

**这就是这套系统能不断进化的关键**：观察哪些标签出现频率最高，把它们升级为"入场硬门槛"，迭代降低胜率最差的失败模式。

### 八、观察列表 / 收藏

界面上点 ⭐ 收藏一个代币时：

1. 当前价被锚定为"入场价"
2. 每 5 分钟追踪相对入场价的浮盈浮亏
3. 若浮亏 ≤ -10% → 自动归档为"反面教材"
4. 收藏时**默认会触发一次手动模拟开仓**（仍受日亏损熔断保护）

---

## 配置参数详解

所有配置在 `config.py`：

```python
# === 抓取频率 ===
SCRAPE_ROUND_SECONDS = 300              # 每轮抓取持续时间
HEADLESS = True                         # 是否后台跑浏览器

# === 真人过滤 ===
MIN_FOLLOWERS = 50
MIN_POST_LIKES = 3
MIN_POST_COMMENTS = 2

# === 自动交易开关（默认 paper 模拟）===
TRADING_ENABLED = False
TRADING_MODE = "paper"                  # paper / live
TRADING_INITIAL_BALANCE = 1000.0
TRADING_LEVERAGE = 2                    # 强烈建议 2-3

# === 仓位 sizing ===
TRADING_SIZING_MODE = "risk_based"
TRADING_RISK_PER_TRADE_PCT = 1.0
TRADING_MAX_NOTIONAL_PCT = 50.0

# === 止损 ===
TRADING_STOP_MODE = "atr"
TRADING_ATR_STOP_MULTIPLIER = 1.5
TRADING_STOP_LOSS_MIN_PCT = -1.2
TRADING_STOP_LOSS_MAX_PCT = -5.0

# === TP 金字塔 ===
TRADING_TP1_R = 1.5
TRADING_TP1_CLOSE_PCT = 30.0
TRADING_TP2_R = 3.0
TRADING_TP2_CLOSE_PCT = 30.0
TRADING_TRAIL_REMAIN_PCT = 40.0
TRADING_TRAIL_CALLBACK_PCT = 2.5

# === 入场硬门槛 ===
TRADING_MAX_CHANGE_4H_PCT = 25.0
TRADING_MAX_ENTRY_FUNDING_PCT = 0.05
TRADING_MAX_ENTRY_LSR = 1.7
TRADING_MAX_ENTRY_TAKER_RATIO = 1.8
TRADING_MAX_TAKER_DECAY_PCT = -5.0

# === 账户级熔断 ===
TRADING_MAX_DAILY_LOSS_PCT = 5.0
TRADING_MAX_DAILY_TRADES = 15
TRADING_MAX_CONCURRENT_POSITIONS = 0    # 0 = 不限
TRADING_COOLDOWN_MINUTES_AFTER_LOSS = 30

# === 调试 ===
TRADING_DEBUG = True                    # 打印开仓拒绝原因到 stderr
```

---

## 运维 / 调试

### 看后台日志

`manage_processes.py start` 把 4 个进程开成后台 console 窗口。想看 `[trade-debug] REJECT` 日志，单独前台跑：

```bash
.venv/bin/python auto_trader.py        # macOS / Linux
.venv\Scripts\python.exe auto_trader.py  # Windows
```

### 跑测试

```bash
.venv/bin/python test_risk.py           # 风控（17 个）
.venv/bin/python test_entry_v22.py      # 入场逻辑（9 个）
.venv/bin/python test_taker_trend.py    # taker 趋势（10 个）
.venv/bin/python test_trade_logic.py    # 完整闭环（3 个）
.venv/bin/python test_reset.py          # 重置功能（3 个）
```

### 数据库

所有数据存在 `binance_square.db`（SQLite）。

主要表：
- `posts` / `authors` / `mentions` — 社交数据
- `snapshots` — 合约快照
- `heat_history` — 热度榜历史
- `trade_positions` — 模拟交易仓位
- `trade_signal_locks` — 防重复开仓
- `trade_loss_archive` — 止损归档
- `watchlist` / `watchlist_entries` / `watchlist_followups` — 收藏

### 重置

界面上有红色"重置账户"按钮。也可以直接删 db 重新开始：

```bash
# 危险操作！会丢失所有历史
rm binance_square.db
```

### 首次运行调试

如果第一次运行抓不到帖子（"本轮入库 0 条"），可能是币安广场 API 路径有变。把 `config.py` 里 `HEADLESS = True` 改成 `False`，会弹出 Chromium 窗口，按 F12 看 Network 面板，对照 `scraper.py` 的 `API_URL_PATTERN`。

---

## 免责声明

**本程序不是投资建议。**

- 所有"健康判断"、"信号评分"都是基于规则和历史数据的**客观呈现**，不能预测市场
- 加密货币合约交易**风险极高**，5× 杠杆下 -2% 价格波动 = -10% 保证金亏损
- 默认 **paper 模拟交易**，把模式改成 `live` 也并未实现真实下单
- 任何因使用本程序而产生的盈亏，作者不承担任何责任
- 如果用于实盘，请：先 paper 跑够 100+ 笔有统计意义的样本、用 ≤ 3× 低杠杆、严格止损

详见 [LICENSE](./LICENSE) 文件中的 `DISCLAIMER FOR FINANCIAL/TRADING USE`。

---

## 贡献

欢迎 Issue / PR。建议优先方向：
- 补全 `risk.SECTOR_MAP` 板块分类
- 接入 LLM 对帖子做情绪分析
- 加回测系统（基于 `trade_positions` 历史 + K 线重放）
- 把"市场环境识别"做成模块（连续亏损时自动暂停）

---

**License**: MIT
