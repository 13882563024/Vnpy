# 目标权益全平（账户级止盈保护）

本文说明「目标权益全平」策略的口径、触发条件、执行链路与已知边界。
实现：`strategy/target_equity.py`（纯计算）+ `strategy/engine.py`（估算/全平请求）
+ `web/server.py`（账户级判定与心跳接线）。

## 1. 目的与语义

逐层回归止盈是**单层价格**口径（该层回到建仓 mean 且净利达标）。本策略是
**账户级权益**口径：把「按当前对手价把账户全部持仓平掉，权益能到多少」作为判据，
一旦 ≥ 用户设定的目标权益，就把**所有持仓合约整仓全平**，并锁成「只平不开」。

- 开启方式：**独立开关** `risk.target_equity_enabled` + 目标金额 `risk.target_equity`（元）；
  两者同时满足才生效（开关打开且金额 > 0）；关闭时金额保留，便于再次打开；
- 默认关闭（`target_equity_enabled=false`、`target_equity=0`），不改变原有行为；
- 只影响本系统持仓相关的自动平仓，不改柜台挂单以外的任何东西。

> 全局设置页「目标权益（账户级止盈全平）」：一个开关 + 一个金额输入框。
> 打开开关且金额 > 0 后，**达到目标会自动发出全部持仓的平仓单**（不需要人工点「全平」）。

## 2. 判据公式（本地记账口径）

单个持仓合约（逐层累加）：

```
平仓对手价 exit_px = 平多 near.bid − far.ask
                     平空 near.ask − far.bid          （与执行口径一致）
毛利 gross = 方向 × (exit_px − tier.avg_price) × 乘数 × tier.volume
手续费 fee  = 该持仓「开 2 腿 + 平 2 腿」合计
              （config/fee_table.csv；同一交易日开平按平今，否则按平昨）
```

账户级：

```
预计平仓后权益 exit_equity = 权益基数
                           + 当日已实现净盈亏（Σ Trade.net_pnl）
                           + Σ 各合约持仓 gross − Σ 各合约持仓 fee
```

- **权益基数**：实盘优先 CTP `query_account` 的 `balance`（`MarginManager.equity`，
  即昨结算权益）；查询不到时回退 `risk.account_equity`；模拟模式直接用 `risk.account_equity`；
- **当日已实现净盈亏**取 `Trade.net_pnl`：结算时手续费是「开+平」一次性计入平仓那笔
  `Trade.fee`，因此已实现部分**不再重复扣费**；
- **持仓手续费**用与结算完全相同的 `engine.trade_fee` 口径，避免"目标权益"与实际
  结算两套账。

触发：

```
target_equity > 0  且有持仓  且  全部持仓合约均可估算  且  exit_equity >= target_equity
（target_equity 为「生效值」：开关 target_equity_enabled 打开且金额 > 0 时才取金额，否则为 0）
```

> 等于目标即触发（`>=`），便于把目标设成整百/整千的数。

## 3. 判定时机与安全门

判定在 `App` 的 1s 心跳里执行（`App._check_target_equity`，节流 1s），不绑定单个
合约的 tick —— 任一合约有行情即可推进判定。**以下任一不满足则不判定/不触发**：

| 安全门 | 说明 |
|---|---|
| 开关 + 金额 | `risk.target_equity_enabled=true` 且 `risk.target_equity > 0`（关闭时金额保留但不生效） |
| 有持仓 | 空仓时无平仓意义 |
| 交易已激活 | `engine.trading_active`；用户关掉交易不会被自动平 |
| 处于交易时段 | `engine.in_session(ts)`；非交易时段不按过期盘口决策 |
| 行情新鲜 | `_last_tick` 距今 ≤ 60s（`TARGET_EQUITY_TICK_MAX_AGE_S`） |
| 全部持仓可估算 | 任一持仓合约无法估算（无 tick / 无有效对手价 / 上述门禁）→ **整体不触发**，不拿残缺数据做全平决策 |

逐合约的不可估算原因会写进估算结果的 `unestimated`（前端/日志可见，如
`a2611-a2701(行情过期)`）。

## 4. 执行链路

```
1s 心跳 → App._check_target_equity
        → 各 engine.estimate_position_exit()（按各自最新 tick 的对手价）
        → estimate_exit_equity(...)（账户级汇总 + hit 判定）
   hit → 对每个持仓合约 engine.request_flatten(reason)
        → engine._close_requested = True（挂起）+ close_only = True（只平不开）
        → 下一个 tick 走既有全平路径：
             撤在途挂单 → 按对手价市价全平 → 撤单未确认/发单失败下拍重试
             → 持仓以柜台成交回报结算
```

为什么复用既有全平路径：它与人工「全平」按钮完全同一条链路（含撤单互斥、发单失败
重试、实盘不本地记账），避免新增分支造成裸单/超平。

锁定与恢复：

- 触发后 `close_only=True`：自动开仓/加仓全部被过滤，平仓不受影响；
- 同一交易日不再重复发起请求（`App._target_equity_fired`），避免刷日志；
- **手动点「启动交易」** 会解锁（`close_only=False`），若持仓仍在则下一拍重新判定
  （即"解锁后可再次触发"，不是一次性永久失效）。

## 5. 配置与可见性

| 入口 | 说明 |
|---|---|
| `config/global.json → risk.target_equity_enabled` | 开关（`true` 才生效；默认 `false`） |
| `config/global.json → risk.target_equity` | 目标金额（元；需 > 0） |
| 网页「全局设置 → 目标权益（账户级止盈全平）」 | 开关（点击即保存）+ 金额输入框（保存金额），状态行显示「已开启 / 已关闭（金额已保留）」 |
| `PUT /api/global/risk` | `{"target_equity_enabled": true, "target_equity": 1030000}`；同时支持 `margin_ratio_limit` / `account_equity`；保存后 `reload_risk_settings()` 热生效。开关打开但金额为 0 时返回 `warning` 提示不会触发 |
| 快照 `margin` 块 | `target_equity_enabled` / `target_equity_config`（配置金额）/ `target_equity`（生效值）/ `exit_equity` / `target_equity_hit` / `target_equity_fired` |
| 监控页顶部资金条 | `目标 X（平仓后预计 Y）`；已触发标注「· 已自动全平（只平不开）」并变红；开关打开但金额为 0 时提示「不会触发」 |
| 决策日志 | `action=平`，detail 含权益基数、当日已实现、持仓毛利、开平手续费与目标值 |

## 6. 已知边界

1. **全平执行依赖 tick**：心跳只负责"发起请求"；撤单 + 市价全平在 `engine.on_tick`
   的全平分支执行。行情中断（无 tick）时请求会挂起到有 tick 才执行。
2. **熔断优先**：`on_tick` 中熔断门位于全平分支之前，熔断期间不会执行目标权益全平
   （沿用现有「熔断停手」语义）。
3. **跨日口径偏差**：权益基数 `balance` 是昨结算权益，已实现取本地当日 `trades`，
   而持仓毛利按**建仓均价**计算；跨过结算日后，柜台已按昨结算价重估过持仓，
   两者会有偏差（本策略是本地记账口径的近似，不是柜台动态权益）。
4. **账户级聚合**：多合约同时持仓时按账户合计判定；任一持仓合约不可估算即整体
   不触发（宁可不动，不拿残缺数据全平）。
5. **不覆盖他人持仓**：只用本系统登记的持仓（`engine.pos`）；手工单/其他策略的
   持仓不参与估算，也不会被本策略平掉。
6. **模拟模式可用**：模拟模式用 `risk.account_equity` 作权益基数，便于先用模拟
   验证触发与全平链路。

## 7. 代码位置

| 位置 | 职责 |
|---|---|
| `strategy/target_equity.py` | 纯计算：`PositionEstimate` / `EquityEstimate` / `estimate_exit_equity`（净盈、exit_equity、hit、reason） |
| `strategy/engine.py` | `estimate_position_exit()`（单合约按对手价估算）、`request_flatten()`（挂起全平 + 只平不开） |
| `web/server.py` | `App._check_target_equity()`（账户级判定与安全门）、`target_equity_config()`（开关+金额）、`target_equity_setting()`（生效值）、`reload_risk_settings()`、心跳接线、快照字段 |
| `web/api.py` | `PUT /api/global/risk`（开关/金额/保证金参数热更新；开关开但金额为 0 时返回 warning） |
| `web/static/settings.html` / `app.js` | 全局设置页：开关（点击即保存）+ 金额输入 + 状态行；监控页资金条展示「目标 / 平仓后预计」与已触发标注 |
| `core/config.py` | `DEFAULT_GLOBAL.risk.target_equity_enabled`（默认 false）/ `target_equity`（默认 0） |
| `tests/test_target_equity.py` | 估算公式（多/空、负价差、多层、手续费）、阈值边界、安全门、触发/锁定/解锁、开关语义、配置 API |
