# 量化交易模块重构：CHANGELOG

对照之前诊断的 13 个问题，逐条说明已做修复：

---

## 🔴 真 Bug 修复

### #1 TP2 触发条件的边界 bug
**位置**：`trade_logic.py` `update_paper_positions()`
**原代码**：`elif closed_qty > 0 and closed_qty < qty * 0.79 and price >= tp2:`
**问题**：硬编码 0.79，依赖 TP1 + TP2 累计平仓 < 79% 才能进入 TP2 分支。如果用户把 `TP1_CLOSE_PCT` 改到 80%，TP2 永远不会被触发。
**修复**：用语义清晰的布尔变量 `tp1_done`、`tp2_done`，彻底消除对魔数 0.79 的依赖。
**验证**：`test_trade_logic.py::test_tp2_can_trigger` 构造 TP1=80% 场景，确认 TP2 仍能触发（closed_qty=9.5 而非 8.0）。

### #2 止损滑点假设不真实
**问题**：原代码 `realized += (stop - entry) * open_qty`，按止损价**精确成交**。真实行情止损被击穿时往往会滑点，纸面盈亏过度乐观。
**修复**：新增 `TRADING_STOP_SLIPPAGE_PCT` 配置（默认 0.15%），止损成交价 = `min(当前价, stop × (1 - 滑点%))`。入场也加了 `TRADING_ASSUMED_SLIPPAGE_PCT`（默认 0.05%）。
**验证**：`test_stop_loss_with_slippage` 通过。

### #3 `open_paper_position` 不检查余额和并发数
**问题**：可无限开仓，不检查可用余额也不限并发持仓。
**修复**：`open_paper_position` 改为先调 `risk.check_account_risk()`（日亏损熔断 / 持仓上限 / 冷却 / 集中度），再调 `risk.compute_position_size()`（余额不足直接返回 0）。所有检查集中在 `risk.py`。

### #4 `signals.py` 死代码
**原代码**：`any(t.startswith(...) or "追高风险" in n for t in tags for n in [""]) and score < 40`
**问题**：`for n in [""]` 让 `"追高风险" in ""` 永远 False，整个过热判定基本不生效。
**修复**：用显式的标签集合判断 `overheated = any(t in tags for t in ("funding:极高", "15m:急涨", "1h:暴涨", "多空:极端多"))`，阈值也从 40 调到 45。

---

## 🟡 风控漏洞填补

### #5 止损太死板（固定 -2%）
**修复**：新增 `TRADING_STOP_MODE="atr"` 模式，用最近 14 根 1h K 线算 ATR%，止损 = 1.5 × ATR%。并用 `STOP_LOSS_MIN_PCT` / `STOP_LOSS_MAX_PCT` 夹在 `[-1.2%, -5%]` 区间防过松/过紧。拿不到 K 线时自动回退到固定模式。
**位置**：`risk.compute_atr_pct()` / `risk.compute_stop_distance_pct()`
**验证**：`test_atr`, `test_stop_distance_atr_mode`, `test_stop_distance_fallback`。

### #6 没有单笔风险限制（仓位计算方向反了）
**修复**：从"先定保证金"改为"先定风险百分比"（专业做法）。默认 `TRADING_RISK_PER_TRADE_PCT=1.0`，仓位 = (equity × 1%) / |entry - stop|。止损越宽仓位越小，止损越紧仓位越大——风险恒定。
**保留旧模式**：`TRADING_SIZING_MODE="fixed_margin"` 可切回原有行为（兼容旧用户习惯）。
**上限保护**：`TRADING_MAX_NOTIONAL_PCT=50%`，单笔名义价值不超过账户净值的 50%，防止极窄止损导致杠杆失控。
**位置**：`risk.compute_position_size()`
**验证**：4 个测试覆盖正常、half tier、上限压缩、余额不足。

### #7 没有最大持仓数 / 板块集中度
**修复**：
- `TRADING_MAX_CONCURRENT_POSITIONS=3`：同时最多 3 个持仓
- `TRADING_CORRELATED_LIMIT=2`：同板块最多 2 个（粗分类：majors / l2 / meme / ai / defi / alt_l1 / other）
- `risk.SECTOR_MAP` 给常见 token 做了分类，未登记的归 `other` 不限

### #8 没有日内亏损熔断
**修复**：
- `TRADING_MAX_DAILY_LOSS_PCT=5.0`：当日已实现+浮动亏损超净值 5% 停止开新仓
- `TRADING_MAX_DAILY_TRADES=15`：单日最多开 15 次（防刷单）
- `TRADING_COOLDOWN_MINUTES_AFTER_LOSS=30`：同 token 止损后冷却 30 分钟

### #9 没有追高保护
**修复**：即便 15m/1h 涨幅在区间内，如果 4h 涨幅 > 25% 或 24h 涨幅 > 50%，**硬否决**直接 skip。通过 `TRADING_MAX_CHANGE_4H_PCT` / `TRADING_MAX_CHANGE_24H_PCT` 配置。
**位置**：`risk.evaluate_entry_quality()` 的 `hard_block` 部分。

---

## 🟢 信号生成优化

### #10 入场条件改为分档制
**原来**：6 项硬 AND，一项不满足就完全 skip。
**现在**：7 项核心条件（多拆了 `verdict 健康`），按通过数 + signal_score 分三档：
- **FULL**（满仓）：7/7 全通过 且 signal_score ≥ 65
- **HALF**（半仓）：≥5/7 通过 且 signal_score ≥ 55
- **SKIP**：其他情况

仓位在 `compute_position_size` 里按 tier 折半。想要回到旧行为可设 `TRADING_ENTRY_MODE="strict"`。
**位置**：`risk.evaluate_entry_quality()`

### #11 做空 — 按用户要求不改
用户确认只做多。不过 `signals.py` 里的 `direction` 字段保留了，以后想扩展做空可以直接用。

### #12 signal_lock 永不过期
**修复**：新增 `storage.trade_signal_lock_cleanup(hours)`，在 `auto_trader.py` 主循环里每小时跑一次，清理 `TRADING_SIGNAL_LOCK_RETENTION_HOURS=72` 小时前的旧记录。

补充：经核实，`leaderboard_signal_key` 是基于热度历史的递增 id，每轮都变，所以"信号消失再出现时能否重开"这点原代码是对的。只需要防止表无限膨胀。

### #13 signal_score 没和仓位联动
**修复**：`signal_score` 现在直接决定 tier（FULL/HALF/SKIP），tier 决定风险百分比（half 自动减半），从而决定仓位。联动闭环：信号 → 质量评估 → 仓位。

---

## 🆕 结构性改动

### 新增 `risk.py` 模块
所有风控逻辑集中到一个模块，职责清晰：
- `AccountContext` / `RiskDecision` 数据类让接口语义明确
- 纯函数设计（不读写数据库），极易单元测试
- 把 `trade_logic` 从 590 行缩短并职责更清晰：只做"组装数据 + 调 risk + 落库"
- **实盘接入时**，只需要替换 `open_paper_position` 和 `update_paper_positions` 里"改数据库状态"的部分为"发 API 订单"，风控逻辑 100% 可复用

### 测试覆盖
- `test_risk.py`：17 个测试，覆盖 ATR / 仓位 sizing / 熔断 / 冷却 / 追高 / 分档
- `test_trade_logic.py`：3 个集成测试，用内存 SQLite 跑完整的 TP1→TP2→trail 流程

---

## 配置迁移指南

新老参数对应：

| 旧参数 | 新行为 |
|---|---|
| `TRADING_ORDER_AMOUNT=50` | 只在 `TRADING_SIZING_MODE="fixed_margin"` 时使用；默认切换到 risk_based |
| `TRADING_STOP_LOSS_PCT=-2.0` | 只在 `TRADING_STOP_MODE="fixed"` 时使用；默认切 ATR；K 线缺失时回退用这个 |

想保留旧行为？在 config.py 里：
```python
TRADING_SIZING_MODE = "fixed_margin"
TRADING_STOP_MODE = "fixed"
TRADING_ENTRY_MODE = "strict"
```

## 后续建议（我没动的部分）

1. **`web.py`** 1600 行我没去动，里面如果有读取 position.advice / open_reason 展示的地方，现在文案更长更信息量大，可以考虑加个"展开详情"折叠。
2. **数据库迁移**：新增的 `trade_positions.advice` 字段语义没变，但字段长度可能需要看一下，SQLite 是 TEXT 所以一般没问题。
3. **回测**：真要验证这套风控值不值钱，建议搭个基于 `trade_positions` 历史的回测脚本，用真实行情重放一遍对比新旧两套策略的夏普率和回撤。这个工作量比较大（至少 300+ 行），可以之后单独做。
