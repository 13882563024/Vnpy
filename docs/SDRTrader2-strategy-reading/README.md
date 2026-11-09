# SDRTrader2 — 跨期均值回归策略系统

依据 `docs/project-plan.md`（规划方案）与 `cross_period_strategy_recommendations.md`
（策略推荐文档）实现的跨期均值回归策略系统，功能结构对齐 BMRTrader2
（仅策略层不同：SDR 均值回归 vs BMR2 布林带状态机）。

## 策略核心

- 价差：`spread = near − far`（近月 − 远月，两腿均为非当月合约；采集端按 last 价合成对齐 BMR2，回测 replay 按 mid 口径）
- 信号：`spread` 偏离最近 N=3 分钟均值超过 `X=1` 倍（M=30 分钟）标准差时开仓
- 统计口径：`mean` = 最近 N 分钟 spread 均值、`sd` = 最近 M 分钟 spread 标准差，
  直接用最近几分钟内采集到的 tick 数据集计算（按秒滚动窗口，不做 m1 K 线聚合；
  m1 K 线仅用于图表显示/缓存）
- 分层加仓：最多 3 手（总手数上限），阈值逐层 +0.3 个标准差（同一时间只允许单方向）
- 过滤器：W1 盘口宽度 / S0.5 报价同步 / D1 一档深度 / VR 成交量比例（仅 RM=10）/
  REC3+REX1 临近到期 / MOM 动量（默认关闭）/ 脏数据过滤
- 平仓（逐层仓位管理，每 tick 至多平一层）：进当月整仓强平 → 逐层止损
  （单层浮亏 ≤ stop_loss÷max_positions）→ 时间止损（整仓）→ 逐层回归止盈
  （最新层优先，该层回到建仓时 mean 方向且**净利** ≥ min_exit_pnl）
- 预挂止盈（`exit_preplace` 开关，默认关闭；网页/注册表可配）：开/加仓成交后立即按目标价
  挂最新层的平仓限价单，不必等均值回归；目标价由「净利 = min_exit_pnl」反解：
  `目标价 = 层均价 ± (min_exit_pnl + 该层手续费) ÷ (乘数 × 手数)`，按 tick 取整且方向保证净利不缩水。
  预挂单持续等待成交，不参与挂单超时撤单；加仓或市价平仓（止损/强平/全平）前先撤预挂单，避免裸单。
- 手续费按 `config/fee_table.csv` 的**完整合约 slug** 匹配（未命中回落 2 元/手/边）：
  `元` 为每手固定费用，`万分之` 按成交金额比例；开/平/平今分列，同一交易日开平按平今计费。
  该费率同时用于结算（Trade.fee）与预挂目标价反解；网页可编辑 min_exit_pnl 即时生效。
- 跨交易日：CTP 报单只在一个交易日内存活（日终由柜台作废），新交易日**直接归档移除**
  旧交易日未终态报单（不查询柜台、不发撤单；重启时旧单也不再恢复进实时报单区，
  避免幽灵单常驻并因「同 slug 单挂单」互斥阻塞新挂单），再按当前持仓重新预挂止盈；
  非交易时段只清理不重挂。
- 同交易日重启：本地实时报单按 `order_sys_id`（交易所报单编号，**跨会话唯一**）与 CTP 对齐——
  `ctp_ref` 是 CTP 会话内编号（新会话从 2 重新计数，会与旧单撞号），故回报匹配以 sysid 优先、
  ref 仅作本会话回退（撞号且 sysid 不符时丢弃）；恢复的单会登记给交易通道，使柜台侧旧会话单的
  成交/撤单回报能命中本地单。引擎登录后（启动交易/启动采集/心跳兜底）按 CTP 当日报单对账一次并
  以柜台状态修正本地（成交→建层/平层、撤单→归档，释放挂单槽），查询不到的只告警不自动改；
  撤单未确认（30s）自动重发（带 order_sys_id），最多 5 次后告警停手。
- 挂单撮合（模拟通道，移植自 BMRTrader2）：开/加仓与回归止盈为**限价挂单**（同 slug 同时
  最多一个 ACTIVE 单）；止损/强平/时间止损/全平为**市价单**：实盘走真实报单（按平仓对手价
  穿价挂出，发单前先撤在途的预挂止盈单，撤单未确认时等下一拍再发，避免与止盈单同时成交
  造成超平），持仓由成交回报结算（整仓单按手数逐层落账）；模拟/无通道为本地记账即时成交，
  但**实盘模式而交易通道未就绪时拒绝记账**（否则会造出柜台不存在的假报单/假持仓），
  全平指令未发出会保留重试。市价平仓单按较短超时撤单重发；
  报单状态 ACTIVE（挂单中）/TRADED（已成交）/CANCELLED（已撤）在网页报单记录可见
- 全部参数可在 Web 配置面板热更新（统计窗口重建，持仓/成交保留）

## 功能（对齐 BMRTrader2）

| 模块 | 功能 |
|---|---|
| 合约管理 | 添加跨期合约（`SPD FG609&FG701` 自动解析腿/生成 slug/实时预览）、移除、注册表动态化（重启保留） |
| 采集行情 | 启动/停止采集（配置 `md_front` → CTP 实时行情，ctypes 直连 DLL 照搬 BMR2；否则演示行情）；跨期合成（腿共享，last 价口径）、tick 落盘、事件队列串行分发 |
| 双开关门禁 | 采集/交易开关（红绿切换）、递进门禁（未采集不能开交易）、关闭采集联动停止交易、批量操作 |
| 交易控制 | 全平、撤全部、熔断复位（持久化 + 人工确认）、1s 心跳兜底、采集启动对账门（只平不开） |
| 持仓校准 | 按 CTP **单腿**持仓反推跨期组合持仓并与本地比对（近多+远空=组合多、近空+远多=组合空，成本=近均价−远均价），网页预览差异 → 确认后校准本地记账：手数/方向以柜台为准、层结构尽量保留、均价本地缺失才补；不发单/不撤单（依据见 `docs/position-calibration.md`） |
| 目标权益全平 | 账户级止盈保护：全局设置页「开关 + 目标金额」（`risk.target_equity_enabled` + `risk.target_equity`，默认关闭），打开后每秒按「权益基数 + 当日已实现净利 + 各持仓按当前对手价平仓净盈（毛利−开+平手续费）」估算，预计权益 ≥ 目标即**自动发出全部持仓的平仓单**并锁成只平不开；热生效（依据见 `docs/target-equity.md`） |
| 配置 | 策略参数可视化编辑（含只平仓 close_only 开关：开启后不执行任何自动开仓/加仓，仅平仓）+ 保存热更新、模拟/实盘模式切换 |
| 数据存储 | 模拟/实盘数据独立前缀（sim_/real_）：成交/报单/持仓/K线/tick 按模式落盘；实时持仓与当日成交/报单启动自动加载，重启不丢失；历史回看面板 |
| 非交易时间过滤 | CTP 行情入口 + 演示行情源 + 心跳兜底均按品种时段模板过滤（含午休/夜盘/周末归属） |
| 前端 | 暗色监控面板（仿 BMR2）：左栏合约列表 + 状态徽章、价差图（ECharts）、持仓/成交、决策日志（过滤器阻止/开平仓，可清空）、历史回看、全局设置页；合约一律以**完整组合代码**显示（如 `SP a2611&a2701` / `SPD OI611&OI701`）并带**产品中文名**（按品种自动：a→豆一、OI→菜油…，可在合约配置里用 `name_cn` 覆盖），slug（`a2611-a2701`）仅作内部标识（数据目录/接口/文件名） |
| 回测 | walk-forward 驱动、绩效（净利润/年化/回撤/Calmar/Sharpe/胜率/PF）、滑点参数、参数扫描、组合相关性 |

## 目录

```
core/         事件队列（EventQueue 串行分发）/ 持久化 / 全局配置
data_loader/  replay tick 读取（仅回测工具用）/ 脏数据清洗 / 合约选择 / 交易日历
market/       跨期合成（last 价）/ 演示行情 / CTP 行情（ctypes 直连 DLL，照搬 BMR2）/
              K线补充导入（SQL）
strategy/     统计窗口(mean/sd) / 过滤器链 / 平仓规则 / 状态机 / 引擎 / 熔断 / 对账 /
              持仓校准（CTP 单腿持仓反推跨期组合）/ 目标权益全平（账户级止盈）
backtest/     回测驱动 / 绩效 / 参数扫描 / 相关性 / CLI（读旧 replay 数据，独立于采集）
trading/      Broker 抽象 / CtpBroker 实盘(vnpy_ctp/SPD 组合单)
simulation/   SimulatedBroker 模拟撮合
web/          FastAPI REST + WS + 前端（index/settings）
config/       global.json / contracts.json 注册表 / fee_table.csv 手续费
tests/        单测（463 项）
docs/         规划方案 / 策略推荐文档 / 持仓校准反推依据 / 目标权益口径
```

## 运行

```bash
# 启动 Web（双击 start.bat 亦可；首次自动建 .venv 装依赖）
python -m web.server          # http://127.0.0.1:8010

# 回测（当前注册表仅 A 豆一；--slug 指定品种）
python -m backtest.run
python -m backtest.run --slug A --slippage 1.0

# 导出复盘记录（信号 tick / 报单 / 成交 → CSV/JSON，Excel 可直接打开）
python export_records.py                            # 全部合约 × 各自最近交易日 → results/export/
python export_records.py --slug a2611-a2701 --date 2026-10-20
python export_records.py --slug a2611-a2701 --date 2026-10-20 --days 5   # 含该日往前共 5 天
python export_records.py --slug a2611-a2701 --kind order,trade
python export_records.py --split                     # 改为按类拆三份（signals/orders/trades）
python export_records.py --list                      # 列出可导出的合约与交易日

# 复盘记录查看（双击 records.bat 亦可）：选范围 → 选合约 → 控制台显示三类记录
# 明细默认全量（与导出 CSV 一致）；信号 tick 一天可达数万行，可选只看最近 200/20 条
python records_view.py

# 参数扫描（推荐文档「后续建议」第 5 项）
python -m backtest.parameter_scan --slug A --data <replay目录> --param x_sd --values 0.5,1.0,1.5

# 组合相关性
python -m backtest.correlation

# 测试
python -m pytest tests/ -q    # 463 passed, 1 skipped
```

Web 操作流程：添加合约（或使用现有 A）→ ▶ 启动采集 → 启动交易 →
观察价差图/持仓/成交/决策日志；配置面板可热更新策略参数与模式。
实盘重启后需与柜台对齐持仓时，点详情页「⚖ 持仓校准」：先预览 CTP 单腿反推差异
（方向/手数/均价/层结构/告警），确认后才改本地记账（不发单、不撤单）。
需要"赚到某个权益就收工"时，在「全局设置 → 目标权益」打开开关并填目标金额：
每秒按当前对手价估算全平后权益，达标即**自动**发出全部持仓的平仓单并锁成只平不开
（手动「启动交易」恢复）。开关与金额独立保存，关闭时金额保留。

## 迁移到另一台机器（除 CTP 账户外配置一致）

代码、报单引擎（`bin/`）、CTP DLL（`ctp_api/`）、注册表（`config/contracts.json`）、
每合约策略配置（`data/<slug>/config.json`）与前端资源均已入库，新机器只需：

1. 装 **Python 3.10 64 位**（与现有机器一致；`vnpy_ctp` 只有 cp310 win_amd64 wheel）
   并加入 PATH；
2. `git clone https://github.com/13882563024/SDRTrader2.git`（或整目录拷贝，
   排除 `.venv/` 与 `data/` 下的运行数据）；
3. 手工创建 `config/global.json` —— 唯一未入库的配置（含账号密码/auth_code，
   故不入库）。照抄旧机器文件，只把 `ctp.*`（`md_front` / `td_front` / `user_id` /
   `password` / `broker_id` / `app_id` / `auth_code`）换成新账户，其余字段保持一致；
4. 双击 `start.bat`：首次自动建 `.venv` 并装依赖（需联网），Web 在
   http://127.0.0.1:8010 ；
5. 验证：`python -m pytest tests/ -q` → `463 passed, 1 skipped`。

**不迁移**（属本机本账户运行状态）：`data/<slug>/` 下的 tick / 信号 / 持仓 / 报单 /
成交 / K线缓存 / `state.json`，以及 `flow/`（CTP 流文件，程序自建）。新机器首日
图表无历史 K 线，持仓以 CTP 查询与对账为准。

## 数据与口径说明

- 采集系统（完全照搬 BMRTrader2）：
  - CTP 行情：`market/ctp_manager.py` 状态机（IDLE→…→RUNNING，断线重连/Standby 兜底/
    动态订阅）+ `market/ctp_wrapper.py`（ctypes 直连 `ctp_api/thostmduserapi_se.dll`，
    vtable 调用，非 vnpy_ctp）+ `market/market_data_handler.py`/`session_profile.py`
    （交易时段过滤 + 标准化 MarketTick）；
  - 聚合：`SpreadEngine` 按 last 价合成价差（`spread = near_last − far_last`）→
    tick 落盘 `data/<slug>/<mode>ticks_<交易日>.jsonl`（含两腿快照）→ `EventQueue`
    串行分发 → 引擎；K 线闭合落盘 `<mode>klines_cache.json`（心跳/停采强制闭合兜底），
    引擎启动时从缓存恢复历史 K 线（对齐 BMR2 `_load_state`）；
  - 补充 K 线：`POST /api/klines/upload` 上传 SQL 导出 → `sql_kline_loader` 按腿+周期
    合成价差 K 线 → `supplement_klines`（合并/落盘/注入 K 线聚合器，`/klines` 图表可见）；
  - 演示行情：`market/demo_feed.py`（无 md_front 且 `market.demo_feed=true` 时）。
- 引擎口径：事件队列只传价格（照搬 BMR2）→ 盘口过滤器（W1/S0.5/D1）恒通过、
  对手价成交退化为按合成价差成交（`bid=ask=last`）；实时链路走本地模拟撮合
  （限价单穿越成交，`SimulatedBroker` 移植自 BMRTrader2，报单状态 ACTIVE/TRADED/CANCELLED）；
  回测工具（backtest/）仍读旧版 SDRTrader 的 replay 数据（含真实盘口），口径不变；
- 手续费 `config/fee_table.csv` 为模板值（由 MINADAPT 阈值反推），实盘前必须
  替换为真实费率并全量重跑；
- 复盘记录（三类，均为「每次决策一行 / 每笔报单一行 / 每笔成交一行」，按交易日
  落盘 `data/<slug>/<sim_|real_>{signals,orders_hist,trades}_<交易日>.jsonl`）：
  信号 tick = 每次决策的 Signal（`spread`/`mean`/`sd`/开平对手价
  `long_cross_price`/`short_cross_price`/平仓价 `exit_price`/持仓方向 `pos_side`
  + 输出 `actions`），报单 = `Order` 全字段 + 归档信息，成交 = 开→平一笔（含
  毛利/手续费/净利）。三者由 `strategy/records.py` 归一化成同一套公共字段
  （`kind`/`ts`/`time`/`trading_day`/`slug`/`mode`/`side`/`volume`/`price`/
  `reason`）+ 各自专属字段，口径与对齐规则见该模块 docstring；三种查看方式口径一致：
  双击 `records.bat`（选范围 → 选合约 → 控制台显示三类记录，明细**默认全量**，
  与导出的 CSV 内容一致；信号 tick 一天可达数万行，可选只看最近 200/20 条或收起）、
  `python export_records.py`（导出文件）、
  `GET /api/export/{slug}?date=YYYYMMDD&mode=simulate&format=json|csv`
  （JSON 分三类返回，CSV 单表 `kind` 列区分、缺列留空、带 BOM）。
  导出文件规则：**每个合约一个 CSV**（三类记录同表、`kind` 列区分、`time` 列排序，
  文件名 `<slug>_<交易日>_<模式>_records.csv`，多天为 `<起日>_<止日>`），不同合约各自
  一个文件、全部落在 `results/export/`；单文件超 80 万行（Excel 上限留余量）自动按
  交易日拆开；`--split` 可改回按类拆三份；
- 持仓校准（口径见 `docs/position-calibration.md`）：CTP 只按**单腿**登记组合单持仓，
  反推 = 近月多 + 远月空 → 组合多、近月空 + 远月多 → 组合空（手数取配对 min），
  组合成本 = 近月腿均价 − 远月腿均价（腿均价 = `PositionCost ÷ (手数 × 乘数)`）；
  该成本是**盯市成本**（昨仓按昨结算价重算）≠ 原始建仓价，故本地已有均价保留并只告警差异；
  残腿 / 双向对锁（取净手数）/ 净持仓模式 / 成本不可用均按文档 §5 处理；
  **共享腿**（一条腿同属多个组合，如 `a2701` 既是 `a2611-a2701` 的远月又是 `a2701-a2703`
  的近月）按账户级联合反推：同通道合约的腿一起配对、本地有持仓的优先，剩余腿归 `unused`
  告警；并用**腿总量守恒校验**拦截「柜台净持仓抵消 / 其他策略占用」→ `blocked` 时拒绝应用，
  避免误清本地持仓（文档 §7）；
  仅实盘通道且 CTP 已登录时可校准（模拟/未登录一律拒绝，避免空查询误清空本地）。
- 目标权益全平（口径见 `docs/target-equity.md`）：账户级止盈保护，**开关 + 金额**双条件
  （`risk.target_equity_enabled=true` 且 `risk.target_equity>0`；默认关闭，关闭时金额保留），判据 =
  权益基数（CTP `balance`，回退 `risk.account_equity`）+ 当日已实现净盈亏（`Trade.net_pnl`）
  + Σ 各持仓按当前对手价平仓净盈（毛利 − 「开+平」手续费，与结算同口径）；
  预计权益 ≥ 目标 → 自动发出全部持仓的平仓单并锁成只平不开。
  安全门：交易已激活、处于交易时段、各持仓行情 ≤60s、任一持仓无法估算则整体不触发；
  全平复用人工「全平」的底层路径（先撤挂单 → 对手价市价平 → 失败重试），执行需有 tick；
  熔断期间不执行；跨日持仓毛利按建仓价，与柜台昨结算重估口径有偏差。
- 绩效口径：本金 100 万；年化 = 总收益 × 252/交易日（线性）；Calmar = 年化%/最大回撤%
  （用推荐文档 5 品种数据反推验证一致）；回撤 = 相对回撤起点峰值。

## 里程碑状态

| 里程碑 | 状态 |
|---|---|
| M0 脚手架 + core | ✅ |
| M1 回测框架基线 | ✅ 撮合 L0 + walk-forward + 绩效（数据受限，指标为流程验证） |
| M2 回测改进 | ✅ 滑点参数化 / 参数扫描 / 组合相关性；⏳ 真实手续费 / L1 深度压力（依赖外部数据） |
| M3 策略引擎状态机 | ✅ filters + state_machine + exit_rules + engine + 热更新 |
| M4 模拟 + Web | ✅ Broker 抽象 / SimulatedBroker / EventQueue 串行分发 / FastAPI+WS+前端（BMR2 对齐） |
| M5 CTP 实盘 | ✅ 采集：CtpManager+ctp_wrapper（ctypes 直连 v6.6.1 DLL，照搬 BMR2）+ 熔断 + 对账 + 门禁；⚠️ 交易通道（CtpBroker/CppBroker）真实前置联调待做 |
