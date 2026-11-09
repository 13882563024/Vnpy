# 跨期均值回归策略 · 实现方案

> 状态：**已实现（P1–P7 完成，P8 待账号）** · 使用说明见 `docs/跨期策略使用说明.md`
> 制定日期：2026-10-27 ｜ 实现回填：见第 14 节
> 依据：`docs/SDR/cross_period_strategy_recommendations.md`（策略规则）、`docs/全品种手续费表_交易所标准_20260919(1).xlsx`（费率）、`docs/SDRTrader2-strategy-reading/`（参考实现，仅作设计参照）
> 前置：主程序与 CTP 网关已就绪（`docs/plan/PLAN-vnpy-ctp最小策略交易终端.md` v4）
> 变更：v1 初版；**v2 纳入参考版实证**，修正止损口径（整体→逐层）、平今判断（自然日→交易日）、止盈挂单范围（每层→最新层一张）、止盈单撤挂策略（超时撤单→不撤），并补齐 8 项已确认结论（见 11.1）；**v3 纳入使用者实盘经验**：组合单可直接下单、持仓按单腿登记，组合代码以配置为准不做拼接，vnpy 适配重点从"造能力"转为"回报解析容错"（见 5.3）；**v4 回填实现**：P1–P7 已落地并附 76 个单测，第 14 节逐条记录与方案的差异（含 8.1 回测数据前置的源码级修正）


---

## 0. 交付边界

| 能力 | 本次是否交付 |
| --- | --- |
| 策略主类（信号 / 分层 / 过滤器 / 平仓体系） | ✅ 交付 |
| 预挂止盈单（逐层、按公式计算目标价差） | ✅ 交付 |
| 手续费模块（元 / 万分之、平今 / 平昨、自适应阈值） | ✅ 交付 |
| 执行层双实现（两腿报单 / 交易所原生套利指令）+ 配置切换 | ✅ 交付 |
| 价差回测入口 | ✅ 交付 |
| 自动选合约（near/far 每日挑选） | ❌ **不做**，由用户手工维护 |
| 历史 tick 数据下载 | ❌ 不做，但**回测依赖它**（见 8.1） |
| 原生套利指令（组合单） | ⚠️ 下单可行性已由实盘经验确认；但 **vnpy_ctp 的适配与回报解析仍需账号实测**（见 5.3.3） |

---

## 1. 已确认决策

| 决策项 | 结论 |
| --- | --- |
| 实现载体 | `vnpy_spreadtrading`（价差模块） |
| 原策略代码 | 参考版 `docs/SDRTrader2-strategy-reading/` **仅作设计参照**，代码从零实现，不抄 |
| 执行层 | **双执行器 + 配置切换**：`two_leg` / `exchange_spread` |
| 逐层止盈与回归平仓 | **逐层止盈取代**「回归平仓」，其余三条平仓路径不变 |
| 预挂止盈单数量 | **只给最新层挂一张**，平掉后再给新的最新层重挂 |
| 止损口径 | **逐层止损**：任一单层浮亏 ≤ `−stop_loss ÷ max_positions` 即平该层 |
| 信号价差口径 | **mid**（`near_mid − far_mid`）；开仓挂单与平仓判定用对手价 |
| 组合单可行性 | **CTP 可直接对组合合约下单**（实盘经验，如 `SP a2611&a2701`），持仓按单腿登记 |
| 组合代码来源 | 由配置文件提供原始字符串，**程序不拼接**（避免推断两所不同的前缀与月份位数） |
| near/far 选取 | **由用户维护**要交易的跨期合约，策略不做筛选 |

### 1.1 用户补充的核心需求

原策略不提前挂止盈单，本次新增**预挂止盈单**，计算方式：

```text
目标价差 = 该层均价 + 方向 × (min_exit_pnl + 该层「开+平」手续费) ÷ (合约乘数 × 该层手数)
```

---

## 2. 策略规格

### 2.1 价差定义

用户维护每个价差的合约对，策略只负责交易：

```text
spread = near_mid − far_mid
near_mid = (near_bid1 + near_ask1) / 2
far_mid  = (far_bid1  + far_ask1 ) / 2
```

同品种跨月两腿乘数相同，故价差直接用价格差，不需要乘数加权。`vnpy_spreadtrading` 侧注册（**函数签名已核对源码**）：

```python
add_spread(
    name: str,
    leg_settings: list[dict],    # 每项：vt_symbol / variable / trading_direction / trading_multiplier
    price_formula: str,          # "near - far"
    active_symbol: str,
    min_volume: float,
    save: bool = True,
)
```

其中 `leg_settings` 结构化后决定价差的价格公式与下单手数换算：

```python
leg_settings = [
    {"vt_symbol": "OI505.CZCE", "variable": "near", "trading_direction": 1,  "trading_multiplier": 1},
    {"vt_symbol": "OI509.CZCE", "variable": "far",  "trading_direction": -1, "trading_multiplier": -1},
]
price_formula = "near - far"
```

> 注意：`trading_multipliers` 不是独立入参，而是从 `leg_settings[*]["trading_multiplier"]` 里读出来的（`SpreadData` 内部用它做价差手数 ↔ 腿手数换算）。

**`SpreadData` 的合成口径与我们的三个价格一一对应**（已核对 `base.py::calculate_price`，负向腿会自动交换 bid/ask）：

| SpreadData | 计算式 | 我们的用法 |
| --- | --- | --- |
| `ask_price` | `near_ask − far_bid` | **做多价差的成交价** = 参考版的 `long_cross_price` |
| `bid_price` | `near_bid − far_ask` | **做空价差的成交价** = `short_cross_price`；也是平多（卖组合）的成交价 |
| `(bid + ask) / 2` | 恒等于 `near_mid − far_mid` | **信号价差**（用户决策用 mid） |

两点注意：

- `SpreadData` 会把 bid/ask 按 `pricetick` 四舍五入，所以 mid 可能落在半个 tick 的价位上 —— 信号窗口用浮点即可，不必强行对齐。
- `calculate_price` 要求**两腿都有一档挂量**（`leg.bid_volume` / `leg.ask_volume` 非零），否则整条价差报价判为无效。这已经天然实现了 D1 过滤的一部分；而且 `bid_volume`/`ask_volume` 取各腿按 multiplier 折算后的**最小值**，可直接当作"该价差当前可成交手数"用于更严格的深度过滤 —— 比参考版 `depth_ok` 恒 True 的空转要强。

用户维护的配置（`config/spreads.json`）：

```json
{
  "OI_505_509": {
    "legs": ["OI505.CZCE", "OI509.CZCE"],
    "active_symbol": "OI505.CZCE",
    "exchange_spread": "SPD OI505&OI509",
    "product": "OI",
    "size": 10,
    "pricetick": 1,
    "vr_max": 0
  }
}
```

`exchange_spread` 为该合约对的交易所标准组合代码，**仅 `two_leg` 与 `exchange_spread` 切换时使用**；未挂牌组合对时留空即自动降级。

### 2.2 信号

```text
mean = 最近 3 分钟 spread 均值
sd   = 最近 30 分钟 spread 标准差
```

滚动窗口按 tick 时间戳维护（`deque[(datetime, spread)]`），不使用 bar 聚合，避免丢失 tick 级过滤条件。

### 2.3 分层加仓

```text
max_positions = 5        # 最多 5 层
layer_volume  = 1        # 每层 1 手（组合手数）
layer_step    = 0.3      # 每层阈值递增

第 i 层阈值 X_i = 1.0 + 0.3 × (i − 1)      i = 1..5
```

开仓条件（同一时间只允许同方向持仓）：

```text
做多价差：spread < mean − X_i × sd      → 买 near（ask）+ 卖 far（bid）
做空价差：spread > mean + X_i × sd      → 卖 near（bid）+ 买 far（ask）
```

第 i 层仅在已有 i−1 层同方向持仓、且当前持仓手数 < `max_positions` 时允许开仓。

> **判定用 mid，成交价用对手价**：信号与阈值判断基于 mid 价差；实际挂单价取盘口对手价 —— 做多价差 = `near_ask − far_bid`，做空价差 = `near_bid − far_ask`。对手价本身已是盘口价差，**无需再对齐 tick**（参考版 `limit_price` 就是这么做的）。真实成本因此天然包含一次买卖价差。

### 2.4 过滤器

全部为 tick 级前置条件，任一不满足则本次不开仓：

| 过滤器 | 参数 | 条件 |
| --- | --- | --- |
| 盘口宽度 | `width_ticks = 1` | `near_ask − near_bid ≤ 1 tick` 且 `far_ask − far_bid ≤ 1 tick` |
| 报价同步 | `sync_seconds = 0.5` | `abs(near_datetime − far_datetime) ≤ 0.5s` |
| 一档深度 | `min_depth = 1` | 四档量最小值 ≥ `min_depth` |
| 成交量比例 | `vr_max`（RM=10，其余=0 不启用） | `max(vol) / min(vol) ≤ vr_max` |
| 脏数据 | 见下 | 全部通过 |

脏数据规则：`bid > 0`、`ask > 0`、`ask ≥ bid`、盘口宽度 ≤ `max_clean_width_ticks`、单腿 mid 瞬时跳变 ≤ `max_clean_jump_ticks`。

> 一档深度只是最低可成交性要求，**没有模拟 5 层同时吃单的深度压力**。实盘前建议提高 `min_depth` 或另做深度测试（见风险 R6）。

### 2.5 平仓体系（改造后）

| 优先级 | 路径 | 口径 | 范围 |
| --- | --- | --- | --- |
| 1 | 换月强平 | 下一交易日任一腿进入当月 | 整仓 |
| 2 | **逐层止损** | 任一单层浮亏 ≤ `−stop_loss ÷ max_positions` | **该层** |
| 3 | 时间止损 | 持仓交易日数 ≥ `max_hold_days` | 整仓 |
| 4 | 逐层止盈 | 预挂止盈单成交（见第 3 节） | **该层** |

> **节流**：每个 tick 至多产生一条平仓决策（最多平一层），避免一拍之内连平多层把整仓打光。整仓级事件（强平/时间止损）不受此限。

单层浮亏口径（止损判定）：

```text
tier_pnl   = direction × (exit_price − tier_avg) × size × tier_volume
exit_price = 当前平仓对手价（平多 = near_bid − far_ask；平空 = near_ask − far_bid）
```

> 用**平仓对手价**而不是 mid：那才是真正能成交的价格，与执行口径一致；用 mid 会系统性低估浮亏（尤其在宽盘口下）。

> **变更点**：原第 4 条「回到入场均值且 `pnl ≥ min_exit_pnl` 时整体回归平仓」已由逐层止盈取代（用户确认）。
>
> 注意参考版是**两者并存**（预挂止盈为主动出场手段，回归止盈仍在，每 tick 至多平一层）；本方案按你的决定只保留预挂止盈。
>
> 整仓平仓（强平/时间止损）触发时，**必须先撤在挂的止盈单**再平仓，避免"仓已平、单还在"。

---

## 3. 预挂止盈单（核心新增）

### 3.1 目标价差

每层开仓**成交后立即**计算并挂出该层的止盈单：

```text
target = layer_avg + direction × (min_exit_pnl + fee_oc) / (size × layer_volume)
```

| 符号 | 含义 |
| --- | --- |
| `layer_avg` | 该层成交均价（价差口径，= near 成交均价 − far 成交均价） |
| `direction` | `+1` 做多价差（先买 near 卖 far）/ `−1` 做空价差 |
| `min_exit_pnl` | 该层要求的最低净盈利，**金额，单位元**（见 4.3） |
| `fee_oc` | 该层「开仓 + 平仓」手续费合计，**元**，两腿都算（见 3.2） |
| `size` | 合约乘数（用单腿合约的 `size`，不用组合合约） |
| `layer_volume` | 该层手数（组合手数） |

量纲校验：`(元 + 元) ÷ (元/点 × 手) = 点`，与 `layer_avg` 同为价差点，可加。

### 3.2 手续费口径

`fee_oc` 是**该层四条腿费**之和：

```text
fee_oc = fee(near, 开) + fee(far, 开) + fee(near, 平) + fee(far, 平)
```

| 项 | 取值 |
| --- | --- |
| 开仓费 | 按开仓成交价、开仓费率、`layer_volume` 计算 |
| 平仓费 | 按**开仓成交价**估算（见下），平仓费率、`layer_volume` 计算 |
| 平今 / 平昨 | 按**交易日**判断：`tier.open_trading_day == 当前交易日` → 平今费率，否则平昨费率（夜盘属下一交易日，用自然日会判错） |

> 平仓费按开仓价估算而非目标价：万分之费率下两者差异在 1 tick 量级，对费用影响 < 0.1%，可忽略。若需保守，调 `fee_buffer`（默认 `1.0`，可设 `1.1` 加 10% 余量）。
>
> 跨期组合单**手续费无优惠**，两腿分别计费，与上式一致。

### 3.3 取整规则

止盈价必须对齐 `pricetick`（取两腿 pricetick 的最小值，`SpreadData.pricetick` 已内置），且取整方向必须**保守**，保证成交后净盈利不低于 `min_exit_pnl`：

| 持仓方向 | 平仓动作 | 价格条件 | 取整 |
| --- | --- | --- | --- |
| `direction = +1`（多价差） | 卖出组合（卖 near + 买 far） | 成交价必须 **≥** target | **向上取整（ceil）** |
| `direction = −1`（空价差） | 买入组合（买 near + 卖 far） | 成交价必须 **≤** target | **向下取整（floor）** |

```python
def round_to_tick(price: float, pricetick: float, mode: str) -> float:
    eps = 1e-9
    if mode == "ceil":
        return math.ceil(price / pricetick - eps) * pricetick
    return math.floor(price / pricetick + eps) * pricetick
```

> 向上取整意味着卖得更贵、向下取整意味着买得更便宜，都是「宁可少成交、不可少赚钱」的方向。反过来取整会让实际盈利低于 `min_exit_pnl`，与需求相冲突。

### 3.4 挂单范围与生命周期

**只给最新层挂一张**（用户确认，与参考实现一致）。理由：与「逐层止盈从最新层开始」的口径一致，且同一价差只需一个挂单槽位。平掉一层后自动为新的最新层重挂；老层不单独挂单，靠逐层止损 / 时间止损 / 换月强平管理。

```text
开/加仓成交 ──> 为最新层挂止盈单 ──成交──> 该层 CLOSED ──> 为新的最新层重挂
                     │
                     └── 加仓 / 整仓平仓前 ──> 先撤单 ──> 动作完成后重挂
```

| 时点 | 动作 |
| --- | --- |
| 开/加仓成交 | 为**最新层**计算 `target` 并挂止盈单 |
| 开/加仓部分成交 | 按实际成交手数重建该层后挂单；剩余未成交部分撤销 |
| 止盈单成交 | 该层 `CLOSED`，结算已实现盈亏（含实际手续费），随即为新的最新层挂单 |
| **不撤单追价** | 预挂单 `timeout = 0`，持续挂着等目标价成交；**不参与超时撤单** |
| 加仓前 | 先撤在挂止盈单（让出挂单槽位），加仓成交后再挂 |
| 整仓平仓前（强平/时间止损） | 先撤止盈单并等撤单确认（超时 3s 告警），再发平仓单 |
| 收盘 / 换日 | CTP 委托当日作废。新交易日**直接归档**旧单（不查柜台、不发撤单），再按当前持仓重新挂单 |
| 已有在途单 | 不重复挂（同一价差同时只允许一张在途单） |

### 3.5 与平仓路径的联动

- **必须先撤后平**：任何整仓平仓（换月强平/时间止损）前，撤掉在挂止盈单并等撤单回报（超时 3s 告警），再发平仓单，否则"仓已平、单还在"会形成反向开仓。
- 止盈单在挂期间，该层手数仍计入总持仓，逐层止损与时间止损照常计算。
- 平掉一层后，为**新的最新层**按 3.1 重算 `target` 并挂单；老层不挂单。
- 若被平掉的是非最新层（逐层止损打到老层），在挂的止盈单不受影响，无需重挂。

### 3.6 异常处理

| 异常 | 处理 |
| --- | --- |
| 挂单被柜台拒绝 | 记录日志 + 告警，该层标 `PENDING_RETRY`，下一 tick 重挂（指数退避，最多 3 次） |
| 撤单失败 | 重试 3 次；仍失败则告警并停止该价差新开仓 |
| 组合单被拒（错误码 15） | 记录 `StatusMsg` 原文；若连续 3 次，自动切换执行器并告警（见 5.5） |
| 一腿成交另一腿未成交（两腿执行器） | 立即以对手价 IOC 补另一腿；失败则市价补齐并告警 |
| 止盈单成交时持仓已不足 | 说明存在并发平仓，撤单并告警（不应发生，用于兜底） |

---

## 4. 手续费模块

### 4.1 数据源与转换

**唯一数据源**：`docs/全品种手续费表_交易所标准_20260919(1).xlsx` 的 `期货手续费` sheet（89 行）。

转换脚本 `tools/build_fee_table.py` 生成 `fees/fee_table.json`：

```json
{
  "OI": {"exchange": "CZCE", "unit": "元",     "open": 2.0, "close_yesterday": 2.0, "close_today": 2.0},
  "J":  {"exchange": "DCE",  "unit": "万分之", "open": 1.0, "close_yesterday": 1.0, "close_today": 1.4},
  "RM": {"exchange": "CZCE", "unit": "元",     "open": 1.5, "close_yesterday": 1.5, "close_today": 1.5},
  "A":  {"exchange": "DCE",  "unit": "元",     "open": 2.0, "close_yesterday": 2.0, "close_today": 2.0},
  "B":  {"exchange": "DCE",  "unit": "元",     "open": 1.0, "close_yesterday": 1.0, "close_today": 1.0}
}
```

> 「计费单位」列有两类：`元`（按手固定）与 `万分之`（按成交额比例）。**5 个推荐品种中只有 J 是万分之，其余全是元**，两种都必须实现，不能只做一种。
>
> **匹配粒度（借鉴参考版）**：xlsx 是**品种级**费率，而参考实现用的是**合约级 slug**（`a2611-a2701,元,2,2,2`）—— 因为交易所会对特定月份给差异费率，表里第三个 sheet `特定合约费率备查` 就是这类。故 `fee_table.json` 设计为三层回落：
>
> ```text
> 合约 slug（如 OI505-OI509） → 品种代码（如 OI） → 兜底 2 元/手/边
> ```
>
> 参考实现未命中时正是回落 2 元/手/边。

### 4.2 计算规则

```python
def calc_fee(rate: float, unit: str, price: float, size: float, volume: float) -> float:
    if unit == "元":
        return rate * volume
    return price * size * volume * rate / 10000      # 万分之
```

| 场景 | 价格 | 费率 |
| --- | --- | --- |
| 开仓 | 该腿开仓成交价 | `open` |
| 平今（`tier.open_trading_day == 当前交易日`） | 该腿开仓成交价（估算） | `close_today` |
| 平昨（跨交易日平） | 该腿开仓成交价（估算） | `close_yesterday` |

> `万分之` 计算取 `abs(price)` 做防御：腿价本为正，但价差可能为负，误传价差时不应得出负费用（参考实现同样用 `abs`）。

### 4.3 min_exit_pnl（自适应）

```text
min_exit_pnl = max(4 × 单腿单边手续费, 1 × tick_size × size)
```

- `单腿单边手续费` = 单腿 1 手**开仓**费用（元），按当前价格实时算
- `tick_size × size` = 1 跳的金额（元）
- 每次开仓时按当时价格重算，**不是**一次性常量

**已核实数值**（按文档给的价格区间反算，与推荐文档表格一致）：

| 品种 | unit | 1 手开仓费 | 4× 费用 | 1 跳金额 | `min_exit_pnl` | 文档值 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| J | 万分之 1（价 1250） | 12.5 | 50 | 0.5×100 = 50 | 50 | 50 ✅ |
| OI | 元 2 | 2 | 8 | 1×10 = 10 | 10 | 10 ✅ |
| A | 元 2 | 2 | 8 | 1×10 = 10 | 10 | 10 ✅ |
| B | 元 1 | 1 | 4 | 1×10 = 10 | 10 | 10 ✅ |
| RM | 元 1.5 | 1.5 | 6 | 1×10 = 10 | 10 | 10 ✅ |

> 该表同时印证了 `min_exit_pnl` 是**金额（元）**而非点数 —— 若按点数理解，J 的 50 点会对应 5000 元，是 4× 费用的 100 倍，无法自洽。
>
> 提供 `min_exit_pnl_override` 参数：填正值则直接覆盖自适应结果（便于按文档表格固定为 50/10/10/10/10 复现回测）。

---

## 5. 执行层

### 5.1 接口

```python
class LayerExecutor(Protocol):
    def open_layer(self, spread: SpreadData, direction: int, volume: float, layer_index: int) -> str:
        """开一层，返回 algoid"""

    def exit_layer(self, spread: SpreadData, layer: "PositionLayer") -> str:
        """按 layer.target_spread 预挂止盈单，返回 algoid"""

    def cancel(self, algoid: str) -> None: ...
```

两种实现通过 `extra={"mode": "two_leg" | "exchange_spread"}` 分派。

### 5.2 两腿执行器（`two_leg`，默认）

vnpy 只自带 `SpreadTakerAlgo`（主动腿吃单 + 被动腿对冲），**没有预先挂单等成交的算法**，必须自写 `TwoLegMakerAlgo`：

| 环节 | 做法 |
| --- | --- |
| 止盈挂单 | 主动腿（配置指定，默认 `active_symbol`）按目标价差分摊后的价格挂**限价单**等成交；被动腿不动 |
| 主动腿成交 | 立即对被动腿发**对手价 IOC** 吃单对冲 |
| 超时未成交 | **不撤单追价**（与组合单口径一致：预挂单就是等目标价成交）；仅在加仓或整仓平仓前撤单 |
| 被动腿未成交 | 市价补齐 + 告警 |

**为什么不是直接复用 `SpreadTakerAlgo`**：它一启动就吃单，语义是"立刻成交"，与"预挂等价格回来"相反；而止盈的本质就是等价格回来。这是本次必须新增 algo 的直接原因。

### 5.3 交易所原生套利执行器（`exchange_spread`）

用交易所挂牌的跨期组合合约直接报**一条**价差限价单 —— 交易所保证两腿同时成交，没有单腿裸露窗口。

> **可行性已由使用者实盘经验确认**：CTP 可以直接对组合合约下单；成交后**持仓按单腿登记**，没有"跨期持仓"这一形式。这点与 `vnpy_spreadtrading` 的模型天然兼容 —— 它本来就是从单腿持仓反算价差持仓（`SpreadData.calculate_pos`）。

#### 5.3.1 组合代码：格式统一，但仍以配置为准

实盘确认的写法与交易所文档一致 —— **两腿都带品种前缀**：

| 交易所 | 跨期组合代码 | 示例 | 月份位数 |
| --- | --- | --- | --- |
| 大商所 DCE | `SP 第一腿&第二腿` | `SP a2611&a2701` | 4 位 |
| 郑商所 CZCE | `SPD 第一腿&第二腿` | `SPD OI505&OI509` | 3 位 |

代码**含空格和 `&`**，字符串处理必须原样保留，不能 strip 空格。

即便如此，程序仍**不自行拼接组合代码**，直接使用 `config/spreads.json` 中 `exchange_spread` 的原始字符串。理由是避免程序去推断交易所规则：前缀 `SP` / `SPD` 两所不同，月份位数 4 位 / 3 位不同，且换月后组合代码会变 —— 这些都属于"合约由用户维护"的范畴。程序只校验两项：非空、能在合约列表里查到。

#### 5.3.2 价差方向

近月为第一腿、远月为第二腿；组合价格 = 第一腿 − 第二腿，与策略 `spread = near − far` 同号，无需取反。买组合 = 买第一腿 + 卖第二腿。

#### 5.3.3 vnpy_ctp 的适配点：回报解析比发单更脆弱

按 CTP 官方《客户端开发指南》，组合合约的 `CombOffsetFlag` **只需填第 1 个字符**，与单腿一致 —— 也就是说 `vnpy_ctp` 的 `send_order` 很可能**开箱即可发组合单**，只要 `InstrumentID` 填组合代码、`ExchangeID` 正确、`LimitPrice` 填价差。因此实施顺序是：**先不加改造直接试，失败再补**。

真正脆弱的是回报解析 —— 以下三处在 `vnpy_ctp` 源码中都是**无容错的硬地址下标**，组合单回报一旦形态不同就直接 `KeyError` 抛异常：

| 位置 | 代码 | 风险 |
| --- | --- | --- |
| `onRtnOrder` | `contract = symbol_contract_map[symbol]` | 组合合约未被合约查询收录时 `KeyError` |
| `onRtnOrder` | `offset=OFFSET_CTP2VT[data["CombOffsetFlag"]]` | 回报若是两位（如 `"00"`）则 `KeyError` |
| `onRtnTrade` | `offset=OFFSET_CTP2VT[data["OffsetFlag"]]` | 同上 |

所以项目内的子类（**不改 site-packages**）重点不是"造出组合单能力"，而是**加容错**：

```python
class SpreadCtpTdApi(CtpTdApi):
    def onRtnOrder(self, data: dict) -> None:
        # 组合合约未收录时补建 ContractData，避免 symbol_contract_map KeyError
        # 回报 offset 取首字符，兼容一位 / 两位 CombOffsetFlag
        ...
```

覆盖范围：`send_order`（仅在需要两位 `CombOffsetFlag` 时）+ `onRtnOrder` / `onRtnTrade`（容错）。`cancel_order` 沿用原实现。

#### 5.3.4 前置校验（启动即做，不做运行期猜测）

1. 组合合约存在于 `main_engine.get_contract(vt_symbol)`；不存在 → 告警并降级 `two_leg`
2. 组合合约的 `ContractData.size` 为 0（`VolumeMultiple=0`）→ 正常现象，**所有金额/费用计算改用单腿合约的 `size`**
3. 组合合约的 `pricetick` 若为 0 → 用 `SpreadData.pricetick`（两腿最小值）

> 成交回报的 `InstrumentID` 究竟是组合代码还是腿代码，**必须实测确认**。若是腿代码（参考版 `resolve_slug_for_instrument` 的注释即按"OnRtnTrade 用腿代码"处理），则 `SpreadAlgoTemplate.update_trade` 按 leg 匹配的逻辑可直接复用，价差持仓自动更新；若是组合代码，则需要在 algo 内额外做腿映射。

**关键不确定项**：组合单成交回报据公开资料是**两条 `OnRtnTrade`（单腿）**，`TradeType = THOST_FTDC_TRDT_CombinationDerived`；若是这样，`SpreadAlgoTemplate.update_trade` 按 leg 匹配的逻辑可直接复用，价差持仓能正常更新。**此点必须实盘实测确认**（见 5.3.3 / 5.3.4）。

> **参考版不能作为"组合单已验证"的证据**：`docs/SDRTrader2-strategy-reading/` 确实是按 SPD 组合单设计的（`StrategyConfig.instrument = "SPD OI611&OI701"`，报单经 `OrderSpec.instrument` 下传），但阅览版的 `trading/` 只有 `broker.py`（抽象接口）与 `exchange.py`，**真正的 `CtpBroker` 报单实现不在其中**；其 README 里程碑也明确标注「交易通道（CtpBroker/CppBroker）**真实前置联调待做**」。所以本节的所有不确定项（`CombOffsetFlag` 位数、回报形态、错误码 15）全部保留，仍需账号实测。

其他必须处理的限制：

| 限制 | 处理 |
| --- | --- |
| 组合合约 `VolumeMultiple = 0` | 乘数、pricetick 一律取单腿合约，不用组合合约的字段 |
| 不支持市价单 | 只发限价，`payup` 参数忽略并告警 |
| 价格必须是 pricetick 整数倍 | 用 3.3 的取整；组合合约自身 pricetick 可能为 0，用 `SpreadData.pricetick` |
| 开盘集合竞价不能报套利指令 | 集合竞价时段不挂单（时段由 `trading_sessions` 配置判断），等第一个有效 tick |
| 大商所单边委托优先于套利委托 | 成交优先级天然更低，`interval` 内未成交属正常，不重试过频 |
| 只有交易所挂牌的月份对可用 | 见 5.5 降级 |

### 5.4 接入 vnpy 的方式

`SpreadAlgoEngine.algo_class` 是**类属性、全局唯一**（实测 `SpreadAlgoEngine.algo_class == SpreadTakerAlgo`），不能按 algo 注册多个类型。因此：

```python
# run_strategy.py
main_engine.add_app(SpreadTradingApp)
spread_engine = main_engine.get_engine(APP_NAME)          # APP_NAME == "SpreadTrading"
spread_engine.algo_engine.algo_class = CrossPeriodAlgo    # 单一入口，内部按 extra["mode"] 分派
```

`CrossPeriodAlgo` 继承 `SpreadAlgoTemplate`，在 `__init__` 里读 `extra["mode"]`，分派到 `two_leg` / `exchange_spread` 两套内部实现。策略侧通过 `start_long_algo/start_short_algo(..., extra={...})` 透传（实测 `extra: dict` 从 `SpreadStrategyTemplate` 一路传到 algo 构造函数）。

### 5.5 切换与降级矩阵

`config/spreads.json` 的 `executor` 字段决定首选执行器：

| 场景 | 行为 |
| --- | --- |
| `executor = "two_leg"` | 只用两腿 |
| `executor = "exchange_spread"` 且 `exchange_spread` 非空 | 用组合单；组合单连续 3 次被拒 → 自动降级为 `two_leg` + 告警 |
| `executor = "exchange_spread"` 但 `exchange_spread` 为空 | 启动时告警并降级为 `two_leg` |
| 组合合约在合约列表中查不到 | 启动时告警并降级为 `two_leg` |

降级只影响**新挂出的单**，已在挂的单保持原执行器，避免混乱。

---

## 6. 模块与文件结构

```
F:\workspace\Vnpy\
├── strategies/
│   ├── __init__.py
│   └── cross_period_mean_reversion.py    # CrossPeriodMRStrategy(SpreadStrategyTemplate)
├── execution/
│   ├── __init__.py
│   ├── base.py                           # PositionLayer 数据类、LayerExecutor 协议
│   ├── cross_period_algo.py              # CrossPeriodAlgo(SpreadAlgoTemplate)，按 mode 分派
│   ├── two_leg.py                        # 两腿挂单 + 对手腿对冲
│   └── exchange_spread.py                # 原生组合单报单
├── gateway/
│   ├── __init__.py
│   └── ctp_spread_gateway.py             # SpreadCtpGateway + SpreadCtpTdApi（覆盖 send_order）
├── fees/
│   ├── __init__.py
│   ├── calculator.py                     # FeeCalculator（元 / 万分之 / 平今平昨 / min_exit_pnl）
│   └── fee_table.json                    # 由 tools/build_fee_table.py 生成
├── config/
│   └── spreads.json                      # 用户维护：合约对、组合代码、执行器
├── tools/
│   └── build_fee_table.py                # xlsx → fee_table.json
├── tests/
│   ├── test_fee.py                       # 费率与自适应阈值
│   ├── test_target.py                    # 目标价差与取整
│   └── test_filters.py                   # 过滤器边界
├── run_strategy.py                       # 实盘启动
└── run_backtest.py                       # 回测入口
```

---

## 7. 参数表

```python
class CrossPeriodMRStrategy(SpreadStrategyTemplate):
    parameters = [
        # 信号
        "mean_window_minutes",      # 3
        "sd_window_minutes",        # 30
        "entry_k",                  # 1.0
        "layer_step",               # 0.3
        # 分层
        "max_positions",            # 5
        "layer_volume",             # 1
        # 过滤器
        "width_ticks",              # 1
        "sync_seconds",             # 0.5
        "min_depth",                # 1
        "vr_max",                   # 0 或 10
        "max_clean_width_ticks",    # 5
        "max_clean_jump_ticks",     # 20
        # 平仓
        "stop_loss",                # J=20000, 其余=10000（元）
        "max_hold_days",            # 10
        "roll_entry_cutoff_days",   # 3
        "roll_exit_days",           # 1
        # 止盈
        "exit_preplace",            # True：开/加仓成交后立即为最新层预挂止盈单
        "min_exit_pnl_override",    # 0 = 用自适应；>0 直接覆盖
        "fee_buffer",               # 1.0
        "target_round_mode",        # "conservative"（按 3.3 保守取整）
        # 执行
        "executor",                 # "two_leg" / "exchange_spread"
        "spread_offset_flag_width", # 1 或 2（组合单 CombOffsetFlag 位数）
        "market_close_timeout",     # 5.0（秒）：市价平仓单未成交则撤单重发
        # 风控
        "max_exit_per_tick",        # 1（每 tick 至多平一层）
        "close_only",               # False：只平不开
        # 实盘
        "trading",                  # False（默认只算不发单）
        "trading_sessions",         # 交易时段，用于集合竞价与非交易时段拦截
    ]
```

> **参考版实际生效值**（可作实盘起步配置）：`max_positions=3`、`max_hold_days=999`（等效关闭时间止损）、`stop_loss=10000`、`exit_preplace=true`、`close_only=true`（只平不开）、`order_timeout_s` 取极大值（挂单永不撤）。
>
> 与推荐文档的 `MP5 / H10` 不同，说明推荐参数在实盘中做过收敛 —— 起点建议按参考版（3 层、关闭时间止损），跑稳后再考虑放宽。

各品种参数沿用推荐文档：J/OI/A/B 为 `VR0`，RM 为 `VR10`；`MP5 LS0.3`、`H10`、`REC3 + REX1` 全品种一致。

---

## 8. 回测方案

### 8.1 前置：需要两腿历史 tick 数据

`vnpy_spreadtrading` 的 `BacktestingEngine` 在 tick 模式下按合成价差撮合，要求**两腿各自的 tick 数据已入库**（`load_tick` 按 `vt_symbol` 分别读取）。

> 当前 `~/.vntrader/database.db` **没有任何 tick 数据**（V4 验证时查到只有 4 张空表）。回测要跑起来，必须先有 tick 数据；策略推荐文档提到的 `data/hf_all_full_dedup` 在外部项目里，不在本仓库。**这是回测的硬前置，需确认数据来源。**

### 8.2 撮合口径与乐观偏差

回测引擎的 `cross_algo()` 逻辑（实测源码）：

```python
long_cross  = algo.direction == LONG  and algo.price >= tick.ask_price_1     # 以 ask 成交
short_cross = algo.direction == SHORT and algo.price <= tick.bid_price_1     # 以 bid 成交
```

即**价格触到就成交**，不对 maker 排队建模。对我们的预挂止盈单意味着：

- 卖止盈：价差 bid ≥ 挂价时成交，成交价按当时 bid（可能优于挂价）
- 买止盈：价差 ask ≤ 挂价时成交，成交价按当时 ask

这比真实 maker 更乐观（真实里挂着不一定成交，尤其是大商所套利单优先级低于单边）。**回测结果需按此打折理解**，实盘必须记录「止盈挂单成交率」这一指标来校准。

### 8.3 回测差异项

| 项 | 回测 | 实盘 |
| --- | --- | --- |
| 手续费 | 净额近似（`BacktestingEngine` 用单一 `rate`） | 按 `FeeCalculator` 精确逐腿计算 |
| 止盈成交 | 触价即成交 | 排队、可能不成交 |
| 两腿同步 | 合成 tick 同时成交 | 两腿有先后、可能单腿 |
| 组合单 | 不模拟 | 由交易所撮合 |

> 建议回测里用 `FeeCalculator` 覆写引擎的费率计算，保证单笔净盈利口径与实盘一致；否则 `min_exit_pnl` 的校准会失真。

---

## 9. 验证方案

| 序号 | 验证项 | 判定 |
| --- | --- | --- |
| T1 | 费率转换 | xlsx 89 行全部转换，`unit` 分类正确；J 被识别为 `万分之`，其余 4 品种为 `元` |
| T2 | 手续费计算 | 手算 4 组对照（J 万分之开/平今、OI 元开/平昨）误差 = 0 |
| T3 | `min_exit_pnl` | 5 品种复现 50/10/10/10/10；对 J 用价 1250 验证 |
| T4 | 目标价差 | 用参考版给出的两个实算基准**对拍**（`a2611-a2701`：乘数 10、tick 1、2 元/边、门槛 10 元）<br>① 1 手、层均 −41、费用 8 → 目标 −39.2 → 挂 **−39.0**，净利 **12 元**<br>② 3 手同层、费用 24 → 目标 −39.87 → 挂 **−39.0**，净利 **36 元**<br>再补 2 组空头方向用例，核对取整方向与净利 ≥ `min_exit_pnl` |
| T5 | 过滤器 | 每个过滤器的边界值（恰好等于/刚好超过）各一例 |
| T6 | 状态机 | 部分成交、撤单重挂、整体平仓前撤单 3 条路径，用 mock algo 走通 |
| T7 | 回测跑通 | 任一品种 tick 数据就位后，回测输出与推荐文档同量级（不要求复现） |
| T8 | 实盘冒烟 | 需账号；`trading=False` 时只计算不发单，日志能打印每层 target |

---

## 10. 风险

| 编号 | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| **R1** | 组合单的 **vnpy 适配细节**未验证 | 下单本身已由使用者实盘经验确认可行；但 `vnpy_ctp` 的回报解析**无容错**（3 处硬下标，见 5.3.3），且成交回报的 `InstrumentID` 是组合代码还是腿代码未定 —— 可能导致 `KeyError` 崩溃或价差仓位不更新 | 双执行器默认 `two_leg`；组合单可配置 + 自动降级；子类覆盖 `onRtnOrder`/`onRtnTrade` 加容错；拿账号后按 5.3.3/5.3.4 清单逐项实测 |
| R2 | 组合月份对未被交易所挂牌 | `exchange_spread` 不可用 | 启动时查询组合合约存在性，缺失即降级 `two_leg` 并告警 |
| R3 | 大商所套利单优先级低于单边委托 | 止盈单长期不成交，止盈形同虚设 | 监控挂单存活时间，超阈值告警；必要时切回 `two_leg` |
| R4 | 手续费仅交易所标准，无期货公司加收 | 实际成本被低估，止盈目标偏低 | `fee_buffer` 参数加余量；拿到账户实际费率后替换 `fee_table.json` |
| R5 | 回测撮合偏乐观（触价即成交） | 回测收益虚高 | 记录实盘成交率校准；报告中标注偏差 |
| R6 | 一档深度不足以支撑 5 层同时成交 | 实盘滑点、成交失败 | 提高 `min_depth` 或加深度压力测试，实盘先用小 `max_positions` |
| R7 | 组合合约 `VolumeMultiple = 0` | 若误用组合合约字段会算出 0 费用/0 金额 | 强制所有计算使用单腿 `ContractData`，代码中加断言 |
| R8 | `py`/`vnpy` 升级破坏 `algo_class` 替换 | 执行层失效 | `run_strategy.py` 里对替换结果做断言，启动自检 |
| R9 | 逐层止盈与整体平仓并发 | 平仓后残留裸单或反向开仓 | 严格「先撤后平」，撤单超时 3s 未确认则告警并中止该次平仓 |

---

## 11. 已确认与仍需确认

### 11.1 已确认（参考实现实证 + 用户决策，不再是假设）

| # | 结论 | 依据 |
| --- | --- | --- |
| 1 | `min_exit_pnl` 单位是**元** | 参考版 `ExitParams.min_exit_pnl: float` 按元使用，且 5 品种数据反推自洽 |
| 2 | `fee_oc` = **四条腿**费之和 × 手数 | 参考版 `trade_fee`：`开近 + 开远 + 平近 + 平远` 再乘 volume |
| 3 | 取整 **多向上 / 空向下** | 参考版 `preplace_exit_price` 同向实现，注释写明"保住净利" |
| 4 | 平今/平昨按**交易日**判断 | 参考版 `close_kind = "today" if open_trading_day == trading_day` |
| 5 | 止损**逐层** | 用户决策；参考版 `stop_loss_per_tier = stop_loss / max_positions` |
| 6 | 预挂止盈**只给最新层一张** | 用户决策；参考版 `tier = self.pos.tiers[-1]` |
| 7 | 预挂单**不撤单追价** | 用户确认；参考版 `timeout_seconds=0`，仅加仓/平仓前撤 |
| 8 | 信号价差用 **mid** | 用户决策（推荐文档口径；参考版实盘用 last，回测用 mid） |
| 9 | 组合单**可直接下单**，持仓按**单腿**登记 | 使用者实盘经验（`SP a2611&a2701`）；与 `SpreadData.calculate_pos` 的单腿反算模型天然兼容 |
| 10 | 组合代码**两腿都带品种前缀**，但程序不拼接 | 实盘确认格式与交易所文档一致；不拼接是为了不推断 `SP`/`SPD` 与 3/4 位月份的差异 |

### 11.2 仍需确认 / 按默认处理

| # | 事项 | 默认处理 | 影响 |
| --- | --- | --- | --- |
| 1 | 平仓费用按**开仓价**估算 | 取开仓时的腿价 | 万分之费率下误差 < 0.1% |
| 2 | 止盈单**当日有效** | 新交易日归档旧单后重挂 | 与参考版一致 |
| 3 | 时间止损按**层开仓交易日**起算 | 按交易日数比较 | 参考版用「自然日 × 5/7」近似 |
| 4 | 换月判断 | 用合约月份推：`距当月自然日 × 5/7 ≈ 交易日数` | **不需要交易日历**，比原方案的 `last_trading_date` 更简单（参考版做法） |
| 5 | 一档深度 `min_depth = 1` | 沿用文档 | ⚠️ 参考版 `depth_ok` **恒返回 True**（replay 数据无一档挂量），即 D1 实际空转；我们接 CTP 真实盘口后应真正启用 |
| 6 | 手续费是否含期货公司加收 | 只含交易所标准 | 用 `fee_buffer` 加余量；拿到账户实际费率后替换 |

---

## 12. 实施步骤

| 阶段 | 内容 | 产出 | 可验证 |
| --- | --- | --- | --- |
| P1 | `tools/build_fee_table.py` + `FeeCalculator` | `fee_table.json`、`fees/calculator.py` | T1–T3 |
| P2 | `PositionLayer` + 目标价差计算 + 取整 | `execution/base.py` | T4 |
| P3 | 策略主类（信号/分层/过滤器/平仓体系） | `strategies/cross_period_mean_reversion.py` | T5 |
| P4 | `CrossPeriodAlgo` 两腿实现 | `execution/two_leg.py` | T6 |
| P5 | `run_strategy.py` 接入（替换 `algo_class`） | 实盘入口 | T8（`trading=False`） |
| P6 | 回测入口 | `run_backtest.py` | T7（需 tick 数据） |
| P7 | `exchange_spread` 执行器 + `SpreadCtpGateway` | `execution/exchange_spread.py`、`gateway/` | **需账号** |
| P8 | 柜台实测与校准 | 实测报告 | **需账号** |

P1–P6 可在无账号的情况下完整交付并验证；P7–P8 依赖实盘账号，与上一阶段的 CTP 登录问题同源。

---

## 13. 与推荐文档 / 参考实现的差异对照

| 项 | 推荐文档 | 参考实现（SDRTrader2） | 本方案 | 依据 |
| --- | --- | --- | --- | --- |
| 回归平仓 | 有（第 4 优先级） | **保留**，与预挂止盈并存 | **删除**，只留预挂止盈 | 用户决策 |
| 止损 | 「单笔亏损达 stop_loss」 | **逐层** = `stop_loss ÷ max_positions` | 同参考实现 | 用户决策 |
| 信号价差 | mid | 实盘 last / 回测 mid | **mid** | 用户决策 |
| 预挂止盈 | 无（后期加） | 只给**最新层一张**，不撤单 | 同参考实现 | 用户决策 |
| 平仓单形态 | `order_type = market` | 对手价限价 + 5s 撤单重发 | 同参考实现 | 实盘更可控 |
| `max_positions` | 5 | **3** | 3（起步） | 实盘收敛 |
| `max_hold_days` | 10 | **999（等效关闭）** | 999（起步） | 实盘收敛 |
| 手续费 | 模板值 | 合约级 slug + 未命中回落 2 元 | 三层回落（slug → 品种 → 2 元） | 融合两者 |
| 合约选择 | 自动选前两大 | 用户注册表维护 | 用户维护 | 用户决策 |
| 层身份 | — | `tier_id`（uuid） | `tier_id` | 防"索引因平仓错位"平错层 |
| 平仓节流 | — | 每 tick 至多平一层 | 同 | 防一拍连平多层 |
| 换月判断 | 交易日历 | 合约月份近似（自然日 × 5/7） | 合约月份近似 | 免依赖日历 |
| 深度过滤 D1 | 启用 | **恒 True（空转）** | 接真实盘口后启用 | 参考版受数据限制 |

> 一句话概括取舍：**结构照参考版（它跑过实盘），口径按你的决策（mid 信号、只留预挂止盈、逐层止损）。**

---

## 14. 实现回填（v4）

### 14.1 交付物与验证

| 阶段 | 产出文件 | 验证 |
| --- | --- | --- |
| P1 | `tools/build_fee_table.py`、`fees/fee_table.json`、`fees/calculator.py` | T1–T3 ✅ |
| P2 | `execution/base.py`（`PositionLayer` / 目标价差 / 保守取整 / 两个协议） | T4 ✅ |
| P3 | `strategies/cross_period_mean_reversion.py`、`utils/*` | T5 ✅ |
| P4 | `execution/cross_period_algo.py`、`two_leg.py`、`exchange_spread.py` | T6 ✅ |
| P5 | `run_strategy.py`、`config/spreads.json` | `--list` 自检 ✅ |
| P6 | `backtest/engine.py`、`run_backtest.py` | T7 ✅（注入数据端到端；真实 tick 未就位） |
| P7 | `gateway/ctp_spread_gateway.py` | 容错函数有单测；真实回报待账号 |
| P8 | — | ⛔ 需账号 |

`python -m unittest discover -s tests -t .`：**80 个用例全部通过**
（`test_fee.py` / `test_target.py` / `test_filters.py` / `test_strategy.py` / `test_backtest.py` / `test_wiring.py`）。
其中 `test_wiring.py` 覆盖"策略类被发现 + `algo_class` 替换成功 + 价差能注册 + 策略能初始化"，
这四项失败时都是**静默**的（不会抛异常），必须靠断言兜住。

### 14.2 与方案的差异（逐条）

| # | 方案原文 | 实际实现 | 原因 |
| --- | --- | --- | --- |
| 1 | 8.1：tick 模式下「`load_tick` 按 `vt_symbol` 分别读取两腿 tick」 | 自带引擎读的是**价差名字 + `Exchange.LOCAL`** 的合成 tick；改为在 `backtest/engine.py` 自己读两腿 tick 并内存合成 | 已核对 `vnpy_spreadtrading/base.py::load_tick_data`，原文与源码不符；自己合成后也省掉"先落库合成 tick" |
| 2 | 4.1：转换脚本可用 pandas/openpyxl | `build_fee_table.py` 用标准库 `zipfile + xml.etree` 解析 xlsx | 环境无 openpyxl；README 第 3 节要求以 lock 为准，不为工具新增依赖 |
| 3 | 6：`tests/` 未指定框架 | 标准库 `unittest` | 同上，不新增 pytest |
| 4 | 7：参数 `trading`（默认 False = 只算不发单） | 改名 `live_trading`，`setting["trading"]` 作为别名接受 | `SpreadStrategyTemplate.trading` 已被"策略已启动"占用 |
| 5 | 2.4：D1 深度沿用文档 | 接真实盘口后**真正启用**；回测里合成 tick 会还原两腿挂量，过滤器同样生效 | 参考版 `depth_ok` 恒 True 属数据缺失下的空转 |
| 6 | 3.4：新交易日归档旧单后重挂 | 追加 `filters.reset()` 与 `daily_realized` 归零 | 避免脏数据跳变基准与当日盈亏跨日残留 |
| 7 | 5.1：`LayerExecutor` 三方法 | 增加 `close_layer` / `cancel`（共四个），`exit_layer` 接收 `PositionLayer` | 平仓与撤单也要走同一层抽象 |
| 8 | 5.3.3：子类覆盖 `send_order` 处理两位 `CombOffsetFlag` | 默认 1 位；两位仅在 `组合单偏移位数=2` 时启用（写在 `ctp_connect.json`，不进 GUI 表单） | 不破坏已文档化的 8 项连接配置 |
| 9 | 11.2 第 4 条：夜盘映射下一交易日 | 额外**跳过周末**（周五夜盘 → 下周一） | 否则 `open_trading_day` 落在周六，平今/平昨与持仓天数都算错 |
| 10 | 8.3：回测手续费「净额近似」 | 覆写 `calculate_result`，按单腿 `FeeCalculator` 逐笔计费（区分平今/平昨） | 与实盘同口径，才能校准 `min_exit_pnl` |
| 11 | 5.4：按 `spread_engine.algo_engine.algo_class = CrossPeriodAlgo` 接入即可 | 还需**显式**再加载一次策略目录：`SpreadStrategyEngine` 只在构造时按 `Path.cwd()/strategies` 发现策略类，而 `MainEngine.__init__` 会把 cwd 切到 `~/.vntrader` | 否则 `add_strategy` 静默报"找不到策略类"；`run_strategy.py` 现在做显式加载 + 断言（`tests/test_wiring.py` 覆盖） |

### 14.3 仍需账号实测的四项（R1 的残余）

1. 组合单成交回报的 `InstrumentID` 是组合代码还是单腿代码 —— 两种都支持，第一次成交后日志会明示；
2. `CombOffsetFlag` 是否需要两位；
3. 组合单被拒时的错误码与 `StatusMsg` 原文；
4. 组合合约是否出现在 CTP 合约查询结果里（不出现时会自动补建 `ContractData`，`size=0`）。

排错步骤见 `docs/跨期策略使用说明.md` 第 3 节。
