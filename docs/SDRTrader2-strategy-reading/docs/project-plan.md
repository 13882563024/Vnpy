# SDRTrader2 跨期均值回归策略系统 · 详细规划方案

- 版本：v0.1（草案，待评审）
- 状态：规划阶段
- 依据：
  - `cross_period_strategy_recommendations.md`（策略推荐与回测结论，价差方向已更新为 `near − far`）
  - BMRTrader2 制作规范：`docs/strategy-state-machine-design.md`（状态机设计）、`docs/implementation-blueprint.md`（v2 蓝图）、`docs/SESSION_SUMMARY.md`（工程教训）
  - 参考实现：`D:\WORK\python\BMRTrader2`

---

## 1. 目标与范围

### 1.1 目标

构建**数据管理 → 回测验证 → 实时交易**一体化的跨期均值回归策略系统，将推荐文档中验证过的策略（J/OI/A/B/RM 五个品种）工程化落地：

1. 建立可复现的回测平台：以本地 tick 数据 `data/hf_all_full_dedup` 为输入，walk-forward 结果与推荐文档对齐（作为回归基线）；
2. 完成推荐文档「后续建议」中的五项改进（真实手续费、深度压力测试、滑点、组合相关性、RM 扫参）；
3. 参照 BMRTrader2 规范实现实时交易引擎（状态机、事件队列、Broker 抽象），支持模拟盘与 CTP 实盘。

### 1.2 范围

| 模块 | 是否纳入 |
|---|---|
| 本地 tick 数据读取/清洗/组织 | ✅ |
| 回测框架（walk-forward、参数扫描、绩效） | ✅ |
| 策略引擎（信号 + 状态机） | ✅ |
| 模拟撮合（SimulatedBroker） | ✅ |
| Web 控制与展示 | ✅ |
| CTP 实盘接入 | ✅（M5，结构先行，联调后置） |
| 多策略并发风控（组合级敞口等） | ⏸ 暂缓，接口预留 |

### 1.3 核心原则（沿用 BMRTrader2 规范）

1. 事件 → 状态 → 动作：无隐式钩子，规则表驱动；
2. 状态是投影，记录是真实：每次事件处理时根据真实持仓/报单记录决策；
3. 决策幂等：同义单不重复发；
4. 单进程 asyncio + 单一事件队列串行，无并发竞态；
5. 策略层只面向 `Broker` 抽象接口，实盘/模拟切换透明。

---

## 2. 策略规格基线（来自推荐文档）

> 以下为回测验证后的推荐规格，是回测平台与实盘引擎**共用**的单一事实来源（SSoT），任何改动须同步两处。

### 2.1 合约选择与价差

- 每日按上一交易日成交量选前两大**非当月**合约，按月份排序为 `near`（近月）、`far`（远月）；不交易当月；
- 夜盘按中国期货交易日映射到下一交易日；集合竞价时段不参与交易；
- 价差定义（**已按用户要求更新**）：

```text
spread = near_mid - far_mid
mid = (bid_price_1 + ask_price_1) / 2
```

### 2.2 信号参数

```text
N = 3 分钟    mean = 最近 N 分钟 spread 均值
M = 30 分钟   sd   = 最近 M 分钟 spread 标准差
X = 1 倍标准差
```

### 2.3 开仓逻辑

```text
spread < mean - threshold * sd   → 做多价差：买 near（ask）/ 卖 far（bid）
spread > mean + threshold * sd   → 做空价差：卖 near（bid）/ 买 far（ask）
```

### 2.4 分层加仓

```text
max_positions = 5   同一时间只允许同方向持仓
layer_step   = 0.3  第 n 手阈值 = X + 0.3 * (n - 1)
```

### 2.5 过滤器

| 过滤器 | 参数 | J | OI | A | B | RM |
|---|---|---|---|---|---|---|
| 盘口宽度 | `width_ticks=1` | W1 | W1 | W1 | W1 | W1 |
| 报价同步 | `sync_s=0.5` | S0.5 | S0.5 | S0.5 | S0.5 | S0.5 |
| 一档深度 | `min_depth=1` | D1 | D1 | D1 | D1 | D1 |
| 成交量比例 | `volume_ratio_max` | VR0 | VR0 | VR0 | VR0 | **VR10** |
| 临近到期 | `REC3 + REX1` | ✅ | ✅ | ✅ | ✅ | ✅ |
| 动量过滤 | `momentum_min` | MOM0 | MOM0 | MOM0 | MOM0 | MOM0 |
| 脏数据过滤 | 内置清洗 | ✅ | ✅ | ✅ | ✅ | ✅ |

### 2.6 平仓规则（优先级从高到低）

```text
1. 下一交易日任一腿进入当月 → 强平（roll_exit_days = 1）
2. 单笔亏损达到 stop_loss → 止损（J: 20000；OI/A/B/RM: 10000）
3. 持仓超过 max_hold_days → 时间止损（H10）
4. 回到入场均值且 pnl >= 自适应阈值 → 回归平仓
```

```text
min_exit_pnl = max(4 × 单腿单边手续费, 1 × tick_size × 合约乘数)
```

| 品种 | J | OI | A | B | RM |
|---|---:|---:|---:|---:|---:|
| 回归平仓阈值 | 50 | 10 | 10 | 10 | 10 |
| stop_loss | 20000 | 10000 | 10000 | 10000 | 10000 |

```text
order_type = market    stop_spread_ticks = 0
```

### 2.7 组合推荐优先级

OI（最稳）> J（收益最高）> A（低回撤）> B（收益好、风险高）> RM（弱候选，需继续优化）。

---

## 3. 总体架构

### 3.1 进程模型

单进程 asyncio：FastAPI 与策略引擎共享一个事件循环；CTP 回调线程经 `loop.call_soon_threadsafe` 入队；所有业务事件经 EventQueue 串行消费。

```
本地 tick 文件 → data 加载 → 回测驱动（时间推进）
CTP/演示行情  → market 接入 → 跨期合成（near − far）→ K线 → mean/sd
Broker 回报   →（同一拍应用）
Web 控制指令  → ControlEvent 直投
        ↓
EventQueue 串行消费
  → 过滤器链（W1/S0.5/D1/VR/REC/MOM/脏数据）
  → StateMachine.evaluate(ctx) → Action[]
  → ActionExecutor（幂等）→ Broker（实盘/模拟可插拔）
```

### 3.2 目录结构（草案）

```
SDRTrader2/
├── app.py                  # 入口：asyncio.run 启动引擎 + FastAPI（回测/实盘双模式）
├── core/
│   ├── events.py           # EventQueue：Tick/Heartbeat/OrderUpdate/KlineClosed/Control
│   ├── persistence.py      # 文件夹 JSON/JSONL（按 slug 隔离；sim_/real_ 前缀）
│   └── config.py           # 全局配置
├── data/                   # 本地 tick 数据（hf_all_full_dedup）
├── data_loader/            # 数据层（实盘引擎不依赖回测文件格式）
│   ├── reader.py           # tick 文件读取（parquet/csv 适配）
│   ├── cleaner.py          # 脏数据过滤（§5.2 规则）
│   ├── contract_selector.py# 前两大非当月合约选择、near/far、换月
│   └── calendar.py         # 交易日映射、夜盘归属、集合竞价剔除
├── market/                 # 采集层（实时模式）
│   ├── ctp_market.py       # CTP 行情接入（vnpy_ctp）
│   ├── demo_feed.py        # 演示行情源
│   ├── spread_engine.py    # 跨期合成（near − far；腿共享）
│   ├── kline.py            # K线聚合
│   └── stat_window.py      # N 分钟 mean / M 分钟 sd 滚动窗口
├── strategy/               # 策略层
│   ├── engine.py           # 每 slug 一个 StrategyEngine（事件入口）
│   ├── state_machine.py    # evaluate(ctx) → Action[]（纯函数转移表）
│   ├── filters.py          # 过滤器链（W1/S0.5/D1/VR/REC/MOM）
│   ├── models.py           # 持仓/报单模型、状态投影
│   ├── exit_rules.py       # 平仓优先级：强平/止损/时间/回归（MINADAPT）
│   └── config.py           # 策略配置（§2 规格）
├── backtest/               # 回测框架
│   ├── engine.py           # 时间推进 + 事件驱动（复用 strategy 层）
│   ├── matcher.py          # 一档成交撮合（可扩展深度/滑点）
│   ├── walkforward.py      # walk-forward 流程（hf_cross_period_walkforward 工程化）
│   ├── parameter_scan.py   # 参数扫描
│   ├── performance.py      # 绩效：净利润/年化/回撤/Calmar/Sharpe/胜率/PF
│   └── report.py           # 结果输出 results/ + 报告
├── trading/
│   ├── broker.py           # Broker 抽象接口
│   └── ctp_broker.py       # 实盘实现（CTP 交易 API）
├── simulation/
│   ├── simulated_broker.py # 模拟撮合（Broker 接口实现）
│   └── storage.py          # 模拟层独立文件存储
├── web/
│   ├── server.py           # FastAPI + WebSocket
│   ├── api.py              # REST 端点
│   └── static/             # 原生 JS（ECharts）
├── config/
│   ├── global.json         # 全局配置
│   └── contracts.json      # 合约注册表（与 data/ 分离）
├── results/                # 回测结果输出
├── tests/
└── docs/
```

### 3.3 分层职责

| 层 | 职责 | 依赖 |
|---|---|---|
| `core` | 事件队列、持久化、配置 | 无 |
| `data_loader` | 本地 tick 读取/清洗/合约选择/日历 | core |
| `market` | CTP/演示行情、跨期合成、K线、mean/sd 窗口 | core |
| `strategy` | 过滤器链、状态机、平仓规则 | core, trading(Broker 接口) |
| `backtest` | 撮合、walk-forward、扫描、绩效 | core, strategy, data_loader |
| `trading` | Broker 接口 + CtpBroker | core |
| `simulation` | SimulatedBroker（接口与实盘一致） | core, market |
| `web` | REST + WS、控制入口 | core, strategy, market |

**关键复用原则**：`backtest` 与实时引擎**共用同一套 `strategy` 层**（过滤器 + 状态机 + 平仓规则），回测即实盘逻辑的确定性复演——避免"回测一套、实盘一套"的偏差。

---

## 4. 回测框架规划

### 4.1 撮合模型（分级实现）

| 等级 | 内容 | 状态 |
|---|---|---|
| L0 | 一档 bid/ask 全量成交（当前推荐文档口径） | M1 基线 |
| L1 | 一档挂量限制：5 手吃单时挂量不足则部分成交/拒单 | M2 |
| L2 | 额外滑点：每腿 +0.5 tick / +1 tick（参数化） | M2 |
| L3 | 两腿成交延迟/一腿失败概率模型 | 暂缓 |

### 4.2 walk-forward 规范

- 按交易日滚动切分（训练/验证/测试窗口），与 `hf_cross_period_walkforward.py` 语义一致；
- 结果输出到 `results/<实验名>/`：逐品种绩效表 + 资金曲线 + 成交明细；
- **回归基线**：M1 验收须复现推荐文档指标（如 J 净利润 ≈ 1,385,024、OI Calmar ≈ 26.16，误差阈值 ±5%）。

### 4.3 参数扫描

- 待扫描维度：N/M/X、layer_step、max_positions、VR（RM）、REC、SL、H、MINADAPT 开关；
- 输出：参数热力图 + 稳健性分析（相邻参数邻域绩效方差）；
- RM 专项：扫 VR/H/REC/止损参数（推荐文档「后续建议」第 5 项）。

### 4.4 组合分析

- J/OI/A/B 组合级资金曲线：相关性矩阵、组合 Calmar/回撤、权重敏感性（「后续建议」第 4 项）。

---

## 5. 数据规范

### 5.1 数据来源与格式

- 本地 tick 数据：`data/hf_all_full_dedup`（回测输入，只读不修改）；
- 实时模式行情：CTP（`md_front` 配置）→ 否则演示行情源 → 否则无行情；
- 手续费：当前 `fee_table_template.csv`（模板）→ M2 替换为真实费率并全量重跑（推荐文档「注意」项）。

### 5.2 脏数据过滤（内置清洗，全品种启用）

```text
bid <= 0 或 ask <= 0：过滤
ask < bid：过滤
bid/ask 超出涨跌停：过滤
盘口宽度超过 max_clean_width_ticks：过滤
同合约 mid 瞬时跳变超过 max_clean_jump_ticks：过滤
品种前缀不精确匹配：过滤
```

### 5.3 交易日与时段

- 夜盘数据归属下一交易日；集合竞价不参与；换月规则见 §2.1。

---

## 6. 实时交易引擎规划

### 6.1 状态机（参照 BMRTrader2 规范，按均值回归逻辑适配）

持仓状态（以最高持仓层投影）：

| 状态 | 含义 |
|---|---|
| 无仓 | 无持仓 |
| 层 n（1–5） | 已建第 n 层（开仓方向唯一：同一时间只允许同方向） |

报单状态（全局报单集合形状投影）：

| 状态 | 含义 |
|---|---|
| 无报单 / 单边 / 双边 / 成对 | 与 BMRTrader2 §4.2 一致 |

转移表覆盖场景（每事件重估）：

- 开仓：`spread < mean − threshold*sd` 且过滤器全通过且未达 `roll_entry_cutoff_days` → 挂市价单（买 near 卖 far / 反向）；
- 加仓：触发更高层阈值且方向一致 → 加仓到下一层；
- 平仓优先级：进当月强平 > 止损 > H10 时间止损 > 回归平仓（MINADAPT）；
- 撤单/超时/熔断：沿用 BMRTrader2 的 cancelling 独立字段、5s 撤单兜底、15s TIMEOUT、订单失败熔断（§11.1，含持久化与人工复位）。

> 差异说明：BMRTrader2 是"挂限价单等成交 + 状态机收敛"，SDRTrader2 推荐规格为**市价单**（`order_type=market`）+ 分层加仓。状态机需支持市价单立即成交语义，收敛路径相应简化（无挂单漂移问题），但保留撤单/异常分支。

### 6.2 Broker 接口（沿用 BMRTrader2 定义）

```python
class Broker(ABC):
    async def send_order(self, spec: OrderSpec) -> OrderRef: ...
    async def cancel_order(self, ref: OrderRef) -> None: ...
    async def poll_reports(self) -> list[Report]: ...
    async def query_position(self) -> list[Position]: ...
    async def query_order(self) -> list[OrderStatus]: ...
    async def query_trading_day(self) -> str: ...
```

- OrderSpec 合约 = 跨期组合（SPD 风格，如 `SPD FG609&FG701`），CTP 原生支持；
- `SimulatedBroker` 与 `CtpBroker` 回报格式完全一致（策略层无感知）；
- 模拟撮合：跨期 tick 差价判断成交（买单：tick ≤ 报单价全量成交；卖单反之），独立文件存储、重启可恢复。

### 6.3 对账与恢复

- 启动采集/断线重连强制对账：持仓只比对（警告不修改）；报单本地有 CTP 无 → 移除，CTP 有本地无 → 提示性警告；
- 对账期间只平不开；对账不受冷却限制；
- 本地层级/均价记账为权威，成交明细必须持久化。

### 6.4 风控

| 维度 | 状态 |
|---|---|
| 订单失败熔断（连续失败阈值默认 10，持久化，人工复位） | ✅ 纳入 |
| 止损 / 时间止损 / 临近换月强平 | ✅ 策略规则内 |
| 组合级（跨合约）敞口 / 当日回撤熔断 | ⏸ 暂缓，接口预留 |
| 两腿成交失败风险 | ⚠️ 实盘前需 L3 撮合模型评估 |

---

## 7. 配置体系

```text
config/global.json      全局：CTP 前置/账号、行情源开关、Web 端口、默认参数
config/contracts.json   合约注册表（主副本，与 data/ 分离，防清理丢失）
data/<slug>/config.json 每合约策略配置：N/M/X、分层、过滤器、SL/H、MINADAPT、mode(real/simulate)
data/<slug>/            sim_/real_ 前缀隔离的 K线/报单/持仓/历史
```

每品种默认配置（§2 规格）写入 `config/contracts.json`，含 J/OI/A/B/RM 五品种参数与过滤器开关。

---

## 8. 里程碑与验收

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| M0 | 项目脚手架：目录、core（EventQueue/持久化/配置）、数据读取与清洗 | 单测：清洗规则、日历映射、合约选择 |
| M1 | 回测框架基线：撮合 L0 + walk-forward + 绩效 | **复现推荐文档指标（±5%）**：J 净利 1,385,024 / OI Calmar 26.16 / A 回撤 3,610 / B 净利 341,718 / RM 净利 79,948 |
| M2 | 回测改进：真实手续费重跑、L1 深度压力、L2 滑点、组合相关性、RM 扫参 | 五项「后续建议」全部产出报告；手续费差异导致的指标变化单独说明 |
| M3 | 策略引擎（状态机 + 过滤器链 + 平仓规则） | 转移表逐格单测；过滤器与推荐文档逐项对齐 |
| M4 | SimulatedBroker + Web 控制/展示 | 模拟撮合端到端联调（无实盘可跑）；REST/WS 冒烟 |
| M5 | CtpBroker 实盘 + 对账 + 熔断 | 结构完成；真实环境联调（登录、SPD 下单参数、回报/持仓映射） |

---

## 9. 风险与待办

| # | 风险/待办 | 说明 | 处置 |
|---|---|---|---|
| 1 | 手续费为模板值 | 非真实费率 | M2 替换重跑 |
| 2 | 成交深度偏乐观 | 一档成交未模拟 5 手吃单 | M2 L1 压力测试 |
| 3 | 两腿成交风险 | 同步 ≤0.5s 但真实仍可能一腿成交一腿失败 | M5 实盘小仓验证；L3 模型评估 |
| 4 | RM 偏弱 | 年化仅 2.60%，对手续费/滑点敏感 | 专项扫参；不做第一批实盘核心 |
| 5 | CTP 实盘未联调 | 非交易时段前置不可达等（参照 BMRTrader2 教训） | M5 交易时段验证 |

---

## 10. 与 BMRTrader2 的复用与差异

| 项 | BMRTrader2 | SDRTrader2 | 处理 |
|---|---|---|---|
| 策略 | 布林带(26,2.0) 入场 + 逆势摊低 + 回归止盈 | N/M 均值回归 + 分层加仓(MP5/LS0.3) + MINADAPT 平仓 | 状态机框架复用，转移表重写 |
| 下单 | 限价挂单（挂出直到成交/撤） | 市价单 | 引擎适配市价语义 |
| 过滤器 | 无 | W1/S0.5/D1/VR/REC/MOM/脏数据 7 类 | SDR 新增模块 `filters.py` |
| 回测 | 无（仅实时） | 本地 tick 回测 + walk-forward | SDR 新增 `backtest/` 层 |
| 数据 | CTP 实时 | 本地 tick 文件 + CTP | 双数据源适配 |
| 复用 | — | core/事件模型、Broker 接口、对账/熔断/持久化规范、工程教训 | 直接沿用 |

---

## 11. 待评审问题

1. 回测撮合 L1/L2 的优先级：先深度压力（5 手挂量）还是先滑点？
2. 实盘是否必须等回测 L2 完成，还是 M5 可与 M2 并行推进？
3. 状态机是否需要支持"同一时间多方向持仓"（推荐文档规定同方向，保持单向）；
4. 是否需要把 BMRTrader2 现有代码直接拷贝复用（如 core/events.py、trading/broker.py），还是按本方案重写（推荐：拷贝核心文件 + 按 SDR 规格改造）。
