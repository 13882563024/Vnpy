# 价差核对工作台

独立的 PySide6 离线应用。查看、刷新、编辑和预览均不写入文件；只有显式导出才新建参数草案。只查看价差配置也能使用，不需要 CTP、vn.py 主窗口、账号、网络或任何数据库。所有输入均为本地文件；重新读取按钮只刷新内存中的视图。

## 启动

仓库根目录（Windows PowerShell，已有项目虚拟环境）：

```powershell
.\.venv\Scripts\python -B run_workbench.py
.\.venv\Scripts\python -B run_workbench.py --help
# 下列路径是占位示例，必须换成已有、已关闭的快照
.\.venv\Scripts\python -B run_workbench.py --config config/spreads.json --facts 'F:\snapshots\facts.sqlite' --shadow 'F:\snapshots\shadow.sqlite' --day 20261119
```

也可用 `python -B main.py --workbench` 启动同一独立工作台（支持以上筛选参数，不初始化主引擎或柜台）。已打开主终端时，选择「离线核对 → 价差核对工作台」；该窗口不访问终端的引擎或账号。

在「价差配置」选择「编辑参数草案」，修改白名单字段后点「预览变更」。核对源值和新值，再点「导出新文件…」，选择未占用的 `*.draft.json`。文件包含完整价差配置，不是仅含差异的补丁。关闭未导出的草案会丢弃修改。源文件变化则需要重新读取；不提供应用草案、覆盖文件、签发或切换实盘许可的操作。

默认从项目的价差配置读取，不探测用户目录或尝试打开默认数据库。`--account` 严格匹配账本的完整账户键（不是账户别名），`--day` 匹配柜台交易日，均可省略。若事实库包含 `paper_session` 标记，即使当前筛选不包含标记所在日，整库仍标为「纸面柜台（模拟，非真实账户）」。要检查登录凭证文件的存在性，显式传 `--credentials <路径>`；仅查询文件属性，不打开文件，不验证有效性，不显示密钥。报单许可是进程内一次性凭证，离线不能核实。

### 跟随纸面运行

`python -B run_workbench.py --follow <纸面运行目录>` 每秒检查发布清单，可在运行开始前打开，也可查看已结束的运行。它不读取目录中的活动 `facts.sqlite`、`strategy.sqlite`，不接受额外数据库或凭证路径，不开放草案编辑或导出。`--account`、`--day` 如指定必须匹配纸面会话。日志区分首次载入与后续增量；两个库的序号不是同一个时钟。跟随的是**发布进度**，不是实盘行情：资金与成交仍是采集时的历史事实，更新中不代表当前余额或交易许可。

生产约定：单次运行使用独占目录。生产者先将同一输入边界的两个 SQLite backup 副本切为 DELETE journal 模式并关闭，按 `facts-<generation>.sqlite`、`strategy-<generation>.sqlite` 发布，再原子替换 UTF-8 `workbench.json`。已公布的副本禁止修改、同名重用或链接；代号为严格递增正整数，账本历史前缀只追加。清单示例：

```json
{"version":1,"generation":3,"state":"running","published_at":1795062600.0,"account":"[\"paper\",\"demo\"]","day":"20261119"}
```

`published_at` 是实际发布时的 Unix 秒，不是场景时钟。状态有 `running`、`finished`、`failed`，后二者为终态。生产者在业务变化或持续输入时发布；没有新输入不伪造心跳。超过 15 秒未更新标为过期，最终版标为历史。清单缺失、库缺失、WAL、会话不符、版本回退或更新不完整时，只保留上次核对的两库数据并标过期；没有上次数据则等待。生产者至少保留最近两代，读者可跳过中间发布，从上次各库序号追到最新发布。

## 怎么读

- **价差配置**：用左栏按品种名、合约、价差标识搜索，读近远腿、公式、乘数、层限、保证金等当前配置。配置不证明运行时仓位或可用资金。
- **层级与委托**：显示匹配当前组合代码的已记录意图、层号、决策号、版本、原因、回报及配对数量、价格、残腿。影子配对和终结状态是影子输入，不是柜台成交。未绑定意图不能当作已报；柜台委托也可能暂时没有可归属的决策，因此另列组合代码匹配的原始委托。行情输入未持久化的“不下单原因”不反推。无意图/未收齐/有冲突不等于空仓。仅有无合约归属的 layer_event 时，不借用策略名猜价差，从日志核对原记录。
- **收益与资金**：只对选定来源、账户、策略、层内的唯一开层、可归属配对成交和决策时记录的乘数计算毛收益。缺失或不一致不计；不拿申请限价、预计手续费或当前配置替代实际值。仅代表筛选内证据，默认跨日；指定 `--day` 会排除前一日，可能无法投影完整层。无实付费用和当前退出报价时，净收益/浮动收益未知。资金展示 account_snapshot 的交易日、采集时刻、币种、序号、可用与权益，账户与币种不加总；这些数值是历史记录，不赋予交易许可。
- **日志与来源**：保留 new/duplicate/conflict；每页 50 条，各库分别按序号倒序。日志是持久化的事件索引，不是运行时文本日志。登录凭证未知与文件不存在分开。即使配置没有匹配，账户快照与来源页仍可查看。

## 数据接口

```python
from pathlib import Path
from workbench import Sources, load_snapshot
from workbench.model import scoped_decisions, tier_returns

snap = load_snapshot(Sources(facts=Path('closed-facts.sqlite')))
rows = scoped_decisions(snap, next(iter(snap.spreads))) if snap.spreads else []
layer_results = tier_returns(rows)
# 需要 UI 时再导入，并在 QApplication 的 GUI 线程中使用：
# from workbench.window import Window
# window = Window(snap); window.show()
```

`Sources(config, facts, shadow, credentials, account, day)` 与 CLI 字段同名，纯模型导入不依赖 Qt。`load_snapshot` 返回 `Snapshot`（配置、来源路径、各源状态与记录、凭证存在性）。`scoped_decisions` 每项含 `feed / record / state / issues / refs / paired / avg / near_avg / far_avg / residual_near / residual_far`；`None` 表示未知。`tier_returns` 返回 `(来源账户策略层级键, 已见剩余手数, 已见毛收益, 证据序号)`。金额为 Decimal 或 None。

兼容现有 Journal 的 `facts(seq,account,day,kind,identity,payload,outcome,...)` 表，使用同一交易日、账户的决策跟踪与配对投影。手动静态快照以 `mode=ro&immutable=1` 与 `query_only` 打开；遇任何 WAL / SHM / 回滚日志（包括空文件）一律拒绝。跟随模式单独使用 `mode=ro`，并先验证关闭副本的回滚日志格式文件头，不用 immutable。非普通文件、非 facts 表、读取期间文件变化、缺失或损坏均拒绝当作完整证据。**请提供正常关闭或由专用工具制作的静态快照；不要复制正在写入的单个主库文件，不要强行删除事务文件。** 当前版本按筛选条件同步载入记录，较大的归档优先指定账户与交易日。

## 验证

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python -B -m unittest discover -s tests -p 'test_workbench*.py' -v
```

测试用临时合成快照构造证据，应用本身从不建库。离屏测试逐页抓取、保存并解码真实 Qt PNG，同时覆盖窄/宽窗口、搜索和刷新。预览来自默认项目配置、未提供流水，不是模拟盈利或成交。
