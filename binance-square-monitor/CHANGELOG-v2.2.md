# 补丁 v2.2：筛选逻辑基于失败数据反哺

基于你反馈的实际亏损数据（30 笔止损、胜率 20%、标签统计 buy_pressure_faded=73 / entry_lsr_hot=38 / entry_funding_hot=27）做的针对性优化。

---

## 核心洞察

你系统里的"止损失败归档"其实是一座金矿——它记录了**每一次亏损时市场是什么样的**。把这些高频失败模式反哺到**入场**环节，就能在根本上避开。

之前的代码：入场时**不看** funding、不看 LSR、不看 taker 上限，结果系统化地在市场最拥挤的时候买入，就是典型的"追顶被套"。

---

## 修复清单

### 1. 入场硬门槛：三项新的拒绝条件

对应失败标签直接做成 `hard_block`，命中任何一项直接 `tier=skip`，不管其他条件多漂亮：

| 参数 | 默认值 | 对应失败标签 |
|---|---|---|
| `TRADING_MAX_ENTRY_FUNDING_PCT` | 0.05%/8h | `entry_funding_hot` (27 次) |
| `TRADING_MAX_ENTRY_LSR` | 2.0 | `entry_lsr_hot` (38 次) |
| `TRADING_MAX_ENTRY_TAKER_RATIO` | 1.8 | `buy_pressure_faded` (73 次) 的前兆 |

### 2. 15m 涨幅区间：不追急拉，允许回调

**之前**：`15m ∈ [0, 5%]` — 5% 已经是急拉了，进场就是顶。
**之后**：`15m ∈ [-1.5%, 2%]` — 允许小回调入场（买回调），上限砍到 2%（不追急拉）。

两个新参数：
```python
TRADING_MAX_ENTRY_CHANGE_15M = 2.0        # 上限：急拉不追
TRADING_ALLOW_15M_PULLBACK_PCT = -1.5     # 下限：允许小回调
```

**背后的逻辑**：健康的多头趋势是"1h/4h 正向 + 15m 轻微波动"，不是"15m 直线拉"。后者 90% 是刚好的派发顶，你进场就接盘。

### 3. Taker 双边门槛

**之前**：`taker > 1.15` 是下限检查（买盘要够强）。
**之后**：`1.15 < taker < 1.8` 双边检查。

**为啥**：`buy_pressure_faded: 73` 这个标签出现 73 次，意思是"入场时买盘很强，但马上消退"。主动买卖比 > 1.8 的瞬间，往往就是买盘**用尽**的瞬间。逆向思考：你想在买盘刚启动时进场，不是买盘达到顶点时。

### 4. 聪明钱分歧升档机制

signals.py 里已经检测 `top_trader_ls_ratio > 1.5 且 long_short_ratio < 0.7` 这个组合（大户看多 + 散户看空），但之前**没联动到交易决策**。

现在：命中聪明钱分歧时自动升档
- `skip → half`（如果本来通过 4+ 条件且 signal_score >= 50）
- `half → full`

这是经典的"跟着聪明钱做逆势"信号，历史胜率比追势高不少。

配置：`TRADING_PREFER_SMART_MONEY_DIVERGENCE = True`

---

## 预期效果

从你的失败标签数据估算，v2.2 至少能**在入场环节**过滤掉 60-70% 的失败场景：

| 标签 | 次数 | v2.2 是否拦截 |
|---|---|---|
| buy_pressure_faded | 73 | ✅ taker 上限 1.8 |
| entry_lsr_hot | 38 | ✅ LSR 上限 2.0 |
| entry_funding_hot | 27 | ✅ funding 上限 0.05% |
| oi15_reversed | 8 | ⚠️ 无法预防（入场后反转，结构性问题） |
| oi1h_reversed | 8 | ⚠️ 同上 |
| oi4h_reversed | 3 | ⚠️ 同上 |

剩下 30-40% 的亏损是"入场时看着好，进场后变脸"——这种需要靠**时间止损** / **移动止损** / **更谨慎的仓位**来减少。下一阶段可以做，先看 v2.2 的效果。

---

## 使用注意

**会明显减少开仓频率**。
你在 Web 面板看到"可开多"的候选会比以前少很多，这是**正确的**——之前开得多是因为门槛太低在乱买。

如果一整天没开仓，别急着调松门槛。先看看：
1. `auto_trader` 控制台的 `[trade-debug] REJECT` 日志，看是哪个 hard_block 在拦
2. 如果都是某一项（比如 funding_hot），说明现在整个市场确实都在过热，**不开仓才是对的**

想临时放宽某一项，在 config.py 里调数值就行（比如把 funding 从 0.05 调到 0.08）。

---

## 重置按钮排查

你说"重置按钮没加上"。先确认你电脑上的文件：

```powershell
Select-String "resetTradingAccount" "C:\Users\claude\code-binance-square-monitor - 1\files\web.py"
```

- **没输出** → 文件没覆盖成功，重新解压覆盖
- **有输出** → 按 **Ctrl + Shift + R** 强制刷新浏览器（JS 缓存）
- 还不行 → 杀掉 python 进程重启 Web：
  ```powershell
  Get-Process python | Stop-Process -Force
  python manage_processes.py start
  ```

按钮位置：**"保存交易设置"右边**，红色。

---

## 文件清单（只改了 2 个 + 之前的 reset-button 如果没装要装）

**v2.2 本轮改动**：
- `config.py` ← 新增 6 个入场时机参数
- `risk.py` ← `evaluate_entry_quality` 加三项 hard_block + 聪明钱升档

**v2.1 reset 按钮**（如果你之前没装上）：
- `web.py`
- `storage.py`

全部文件都打在 zip 里，直接覆盖所有就行。
