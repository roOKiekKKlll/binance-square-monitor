# 补丁 v2.1：三个问题的修复

修复 v2.0 部署后你反馈的三个实际问题。这个补丁**只更新 5 个文件**，在上一版 `quant-upgrade.zip` 基础上覆盖即可。

---

## 问题 1：检测到"可开多"但没开仓

### 根因
`open_paper_position` 有 8 个可能的拒绝点（信号不过 / 已有持仓 / 风控熔断 / 余额不足 / ATR 失败 / 名义价值下限 / signal_lock / DB 插入失败），原代码全部静默 `return False`，你从日志看不出到底卡在哪一步。

### 修复
在 `config.py` 加了调试开关：
```python
TRADING_DEBUG = True  # 默认开
```

`trade_logic.py` 里每个拒绝点现在都会在 `auto_trader` 的控制台打出具体原因：
```
[trade-debug] REJECT PEPE: 账户风控: 板块 'meme' 已有 2 个同向仓位（上限 2） | tier=full signal_score=72
[trade-debug] REJECT BTC: 仓位计算: 名义价值 $8.5 < 下限 $10.0 | tier=half signal_score=58
[trade-debug] REJECT DOGE: signal_lock 已占用 (signal_key=heat:12345) | tier=full signal_score=67
```

观察几轮就能定位到底是风控太严、余额不够、还是信号质量不够。

### 排查思路
你反馈"账户初始余额 1000"，最可能的瓶颈是：
1. **风险反推模式下仓位可能很小**：止损如果是 ATR 算出来的 -4%，仓位 = 10/4 = 2.5 coins。如果价格 $0.5，名义 = $1.25，低于 `TRADING_MIN_NOTIONAL=10` → 拒绝
2. **板块集中度**：如果热度榜前几名都是 meme 币，开 2 个之后第 3 个就被拒
3. **追高硬否决**：4h 涨幅 > 25% 的币会被拒

看完日志知道原因后，可以针对性调参：
- 仓位太小 → 把 `TRADING_MIN_NOTIONAL` 从 10 降到 5，或把 `TRADING_RISK_PER_TRADE_PCT` 从 1.0 调到 2.0
- 板块限制太严 → `TRADING_CORRELATED_LIMIT` 从 2 调到 3

---

## 问题 2：收藏代币后没有开仓

### 根因
`manual_open_on_watch` 在 v2.0 里完全走账户级风控，包括：
- 持仓上限（默认 3，满了就拒收藏）
- 板块集中度（meme 满 2 个就拒）
- 止损冷却

**"收藏"是用户强意愿表达**，这种场景下应该允许突破一些限制——不然用户点收藏却没反应，体验很差。

### 修复
`risk.check_account_risk()` 加了三个豁免参数：
```python
check_account_risk(
    account, token,
    bypass_max_concurrent=True,   # 持仓上限豁免
    bypass_sector_limit=True,     # 板块集中度豁免
    bypass_cooldown=False,        # 冷却仍保留
)
```

`config.py` 里配成：
```python
MANUAL_BYPASS_MAX_CONCURRENT = True   # 默认豁免
MANUAL_BYPASS_SECTOR_LIMIT = True     # 默认豁免
MANUAL_BYPASS_COOLDOWN = False        # 保留冷却保护（刚止损就重开是韭菜行为）
```

**日亏损熔断永不豁免**——这是最后的保命线。

### 同时优化了收藏失败的提示
原来 toast 只显示 `未开仓`，现在会显示具体原因：
- `可用余额不足：可用余额 $5.00 不足，需 $250.00。当前 equity=$1000.00，已锁定保证金=$750.00`
- `按风险反推的仓位太小：名义价值 $3.50 < 下限 $10.0。可尝试：1) 增大账户余额 2) 把 TRADING_SIZING_MODE 改成 'fixed_margin'`
- `{token} 缺少可用市价（可能没有永续合约或接口超时）`

### 价格兜底
如果本地数据库没有这个币的合约快照（新上榜或刚收藏），会实时去 Binance 拉一次 mark_price，而不是直接报错。

---

## 问题 3：每 5 分钟采集完数据后页面卡顿

### 根因分析
我 diff 了原 worker.py，发现问题其实比我一开始想的更严重：

**问题点 A：长事务**
原来 `worker.one_round` 把"帖子入库 + 老数据清理 + 统计 + 热度计算 + 历史记录 + 读观察列表"**塞进一个 `with get_conn() as conn`**，这是一个持续几秒到十几秒的写事务。

**问题点 B：HTTP 请求持有写锁**
更糟的是 `refresh_market_snapshots` 把"对 30 个 token 发 HTTP 请求 + `time.sleep(0.4)`"也包在一个 `with get_conn()` 里。30 个币 × (几百毫秒网络 + 400ms 睡眠) ≈ 20-30 秒，**整整 30 秒持有 SQLite 写连接**。

SQLite 的 WAL 模式虽然能并发读，但写事务开启时后续写操作会阻塞；Web 里读操作虽然能读，但如果 web 本身也要写（比如更新 trading_settings），就会超时。

**问题点 C：清理操作每轮跑**
`purge_old` 删 7 天前的帖子、`heat_history_purge_old` 都是重操作，但之前每轮都在跑（每 5 分钟一次，完全没必要）。

### 修复

**A. worker.one_round 拆成 5 个独立小事务**：
1. 帖子/作者入库
2. 老数据清理（改为每 20 轮 = 100 分钟一次）
3. 统计计数（只读，快）
4. 状态更新（1 行 SQL）
5. 热度计算 + 历史记录

每个事务完成立刻释放锁。

**B. refresh_market_snapshots 重写**：
- 读基础数据用一次短事务
- **每个 token 独立小事务**：网络请求 → 短事务写入 → sleep → 下一个 token
- HTTP 请求和 sleep 都在事务外进行

这意味着 web 每次读操作最多等 1 个 token 的写时间（几十毫秒），不会再等 30 秒。

**C. 延迟清理**：
| 操作 | 原来 | 现在 |
|---|---|---|
| `purge_old(days=7)` | 每轮 | 每 20 轮 |
| `heat_history_purge_old` | 每 50 轮 | 每 100 轮 |

**D. web.py 加 2 秒 TTL 内存缓存**：
`/api/leaderboard` 和 `/api/trading` 是前端最频繁轮询的两个接口，里面会算 `compute_short_scores` + `build_trade_candidates`（两个都是重计算）。加了个简单的线程安全内存缓存：
- 2 秒内相同请求直接返回缓存
- 用户的写操作（收藏/取消/改设置）会立刻清掉缓存，保证变更马上可见

### 预期效果
- 采集结束后的卡顿时间应从 **30+ 秒降到 2-3 秒以内**
- Web 点收藏/调设置等操作不再感觉"卡死"
- 控制台日志不会变多，因为这是同一套信息只是分成多步

---

## 文件清单（只需覆盖这 5 个）

```
config.py         ← 新增 TRADING_DEBUG、MANUAL_BYPASS_* 参数
risk.py           ← check_account_risk 加豁免参数
trade_logic.py    ← open_paper_position 加调试日志、manual_open 放宽风控
worker.py         ← 大事务拆分、清理延迟
web.py            ← 加 TTL 缓存
```

其他 5 个文件（storage/market/signals/auto_trader + 测试）和 v2.0 相同，不动就行。
为了省事我把所有新版文件都打包给你，直接全覆盖最简单。

---

## 调试建议

跑起来后的前 1-2 轮，你看 `auto_trader` 控制台的 `[trade-debug] REJECT` 日志：

**如果几乎所有信号都被 `仓位计算` 拒绝**  
→ 说明余额 1000 配风险 1% 算出的单笔 $10 风险在 ATR 止损下仓位太小。  
处理：调低 `TRADING_MIN_NOTIONAL`（比如改 5），或提高 `TRADING_RISK_PER_TRADE_PCT`（比如改 2）。

**如果都是 `板块满了`**  
→ 说明热度榜集中在某个赛道。  
处理：把 `TRADING_CORRELATED_LIMIT` 从 2 调到 3，或给更多币加板块分类。

**如果都是 `持仓数上限`**  
→ 已经开了 3 个仓，这是正常保护，等其中一个平仓就好。  
想放宽：`TRADING_MAX_CONCURRENT_POSITIONS = 5`。

**如果都是 `signal_lock 已占用`**  
→ 说明同一轮同一个币第二次进来，这是**正常去重**，不是 bug。

**如果都是 `tier=skip` 或 `candidate.passed=False`**  
→ 信号评估本身没通过，不是风控问题。检查 `TRADING_ENTRY_MODE`、`TRADING_SIGNAL_FULL_THRESHOLD` 阈值。
