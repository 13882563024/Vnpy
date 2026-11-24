# VeighNa 策略交易终端（CTP 最小可运行版）

基于 VeighNa 4.5.0 的期货交易终端，接入上期技术 CTP 接口，挂载 CTA 策略、CTA 回测、价差交易、风控管理四个应用模块，数据落库 SQLite。

## 跨期交易重写：先看纸面工作台

新版使用独立的策略层账、柜台事实流水和逐笔发单核验。无需凭据即可完成[三层纸面交易验收](docs/plan/纸面交易切换验收.md)，或直接打开[核对工作台](workbench/README.md)。运行目录必须是新目录；以下命令不连接柜台。

```powershell
.\.venv\Scripts\python.exe -B main.py --paper --output bean-paper-001 --fee-table fees/fee_table.json --pace 0.5
# 在另一终端打开；如已结束，会展示最后一次发布而不冒充实时
.\.venv\Scripts\python.exe -B main.py --workbench --follow bean-paper-001
```

普通启动先进入**交易总览**：账户与资金口径、11 个价差的层级/双腿/顶层止盈/开仓状态一张表看全，异常与未决排在最前；点中一行看该价差的委托与详情。右上「专业终端」或 `Alt+1` 回到原来的 vn.py 停靠面板与手动下单界面，退出时不会把隐藏面板当成你的布局保存。普通行情 CLI 不支持 `--live`。工作台只读查看，可单独导出参数草案；纸面账户、回放信号和收益都不代表实际账户。历史仓位须有证据才能归属；隔夜核对不会自动恢复实盘许可。

交易总览在取得真实 CTP 回报后，会把最近有数据的交易日的资金、已返回的腿仓与活动委托快照，以及可用的价差行情缓存到 vn.py 数据目录的 `overview_history_v1/`。未登录时，仅按本机保存的柜台环境、服务器及账户匹配缓存，并标上「历史」及采集时间；没有匹配缓存就保留未知，不生成示例数据。缓存不含连接密码或授权码，不作为当前持仓、挂单状态或交易权限的证据。

交易和行情已连接时可盘外点击「开始交易」并确认，策略会持续待命，直至有效两腿报价和柜台核对均通过；停止、断线或交易会话变化会取消待命。运行中会限频复核持仓与当日委托：仅在柜台回报完整、两腿仓位可明确归属且没有相关在途委托或算法时自动对齐本地层账。顶层缺少止盈时，仍通过原有交易时段、行情、仓量与逐笔报单门禁补挂，不会因一次查询缺单就撤销或重复发出状态未明的委托。

## 1. 环境要求

**必须使用 Python 3.13。** 不是偏好，是硬约束。

`vnpy_ctp` 是封装上期技术 CTP API 的 C++ 扩展，PyPI 上只发布特定 CPython ABI 的 wheel：

| vnpy_ctp 版本 | 提供的 wheel |
| --- | --- |
| 6.7.11.4（本项目使用） | `cp313-win_amd64` |
| 6.7.11.3 / 6.7.11.2 | `cp310-win_amd64`、`cp313-win_amd64` |
| 更早版本 | 同上 |

**没有任何 cp311 / cp312 wheel。** 用 Python 3.12 装 `vnpy_ctp` 只能从 sdist 本地编译（需 MSVC + CTP 静态库），成本高且极易失败。因此本项目固定为 **Python 3.13 + vnpy_ctp 6.7.11.4**。

当前实测环境：Python 3.13.15（`cp313-win_amd64`）、Windows `amd64`。

## 2. 安装

### 步骤 1 · 安装 Python 3.13（关键：阻止 PATH 被前置）

```powershell
winget install --id Python.Python.3.13 --exact --scope user --silent `
  --accept-package-agreements --accept-source-agreements `
  --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0"
```

`--override` 不可省略。winget manifest 的默认参数是 `PrependPath=1`，会把 `Python313` 插到用户 PATH 最前面，抢走系统中已有的 `python` 命令，影响其他项目。

安装后**三项都要复核**：

```powershell
py -0p                 # ① 应出现 -V:3.13，且原有的 3.12 仍在列表中
py -3.13 -V            # ② 应输出 3.13.x
where.exe python       # ③ 应仍指向原有的 Python312（PATH 未被前置）
```

### 步骤 2 · 创建虚拟环境

```powershell
py -3.13 -m venv F:\workspace\Vnpy\.venv
```

### 步骤 3 · 安装依赖

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -U pip
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -r F:\workspace\Vnpy\requirements.lock.txt `
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 首次搭建环境（还没有 lock 文件）时才用 `requirements.txt`；一旦生成了 `requirements.lock.txt`，重建环境一律用 lock。语义区别见第 3 节。

下载量约 500 MB，大头是 PySide6。

**镜像失败兜底**：若报 `No matching distribution` / 404（清华镜像可能尚未同步最新版），对报错的那个包去掉 `-i` 改用官方源单独重试：

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install vnpy_ctp==6.7.11.4
```

### 启动

双击 `run.bat`，或：

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe F:\workspace\Vnpy\main.py
```

## 3. 两个 requirements 文件的语义（必须严格遵守）

| 文件 | 角色 | 用途 | 可否作重建依据 |
| --- | --- | --- | --- |
| `requirements.txt` | **意图声明**（范围 / 天花板 / 钉死原因） | 人工审阅、升级时看"为什么钉这个版本" | ❌ 不可以 |
| `requirements.lock.txt` | **事实快照**（`pip freeze` 全量精确版本） | 重建环境的**唯一依据** | ✅ 唯一入口 |

硬规则：

```powershell
# ✅ 重建环境只用 lock
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -r F:\workspace\Vnpy\requirements.lock.txt

# ❌ 禁止用 requirements.txt 重建
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -r F:\workspace\Vnpy\requirements.txt
```

原因：`requirements.txt` 里 `numpy>=2.2.3,<3` 这类是范围约束，未来几个月可能漂到 2.6/2.7；PySide6 未显式钉死，靠 `vnpy 4.5.0` 的 `pyside6==6.8.2.1` 精确约束和 lock 兜住。绕过 lock 直接装 requirements，等于放弃可复现性。

`requirements.txt` 仅在**有意升级**时修改，改完重新验证 → 重新 `pip freeze` 生成新 lock：

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip freeze > F:\workspace\Vnpy\requirements.lock.txt
```

## 4. 维护

### 4.1 Python 补丁版升级必须带 `--override`

`--override` **不会持久化**。winget manifest 里的 `PrependPath=1` 是包定义的一部分，当 3.13.15 升到 3.13.16 时，裸跑 `winget upgrade` 会重新走 manifest 默认参数，**PATH 又被前置**。

```powershell
# ✅ 正确升级方式（upgrade 本身支持 --override）
winget upgrade --id Python.Python.3.13 --exact --silent `
  --accept-package-agreements --accept-source-agreements `
  --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0"

# 升级后必须复核
where.exe python       # 仍应指向原有的 Python312
py -3.13 -V
```

```powershell
# ❌ 禁止裸跑，会把 PATH 打回前置状态
winget upgrade --id Python.Python.3.13
```

> 若 `--override` 被 winget 拒绝，退路是装完后手工把 `...\Programs\Python\Python313\` 和 `...\Python313\Scripts\` 从用户级 PATH 中删除。

### 4.2 升级 Python 前先确认 vnpy_ctp 有对应 wheel

`vnpy_ctp` 与 CPython 版本强绑定（见第 1 节）。升到 3.14 前，先确认 PyPI 上已发布 `cp314-win_amd64` wheel，否则 CTP 网关会失效。

### 4.3 升级 vnpy_ctp 后重新导出配置字段

CTP 连接配置的字段清单以源码为准，升级后请重新执行第 5.1 节的命令核对。

### 4.4 注意 `py` 命令的默认版本

安装 3.13 后，`py -0p` 中带 `*` 的默认版本会变成 3.13，即 **不带版本号的 `py script.py` 会用 3.13 执行**。`python` 命令不受影响（仍指向 Python312）。若其他项目依赖 `py` 的默认版本，请显式写成 `py -3.12`。

### 4.5 修改 `run.bat` 时必须保持 GBK 编码

`run.bat` 含中文提示，**必须以 GBK（CP936）保存、CRLF 换行、无 BOM，且文件内不得出现 `chcp` 切换代码页**。

原因：cmd 逐行读取批处理文件时按当前代码页计算行偏移，脚本中途 `chcp 65001` 会让偏移错乱，后续中文行被截断成半行当成命令执行，实测报 `'错误]' is not recognized as an internal or external command` 并使脚本直接退出（GUI 根本不启动）。

| 保存方式 | 结果 |
| --- | --- |
| GBK + 无 `chcp` | ✅ 中文正常显示，脚本正常 |
| UTF-8 无 BOM | ❌ 中文乱码（cmd 按 936 解码 UTF-8 字节） |
| UTF-8 有 BOM | ❌ 首行被 BOM 污染，报 `'﻿@echo' is not recognized` |
| UTF-8 + `chcp 65001` | ❌ 行偏移错乱，脚本崩溃 |

用编辑器改写后若发现中文异常，在 PowerShell 里检查编码：

```powershell
[System.Text.Encoding]::GetEncoding(936).GetString([IO.File]::ReadAllBytes("F:\workspace\Vnpy\run.bat"))
```

## 5. CTP 连接配置

### 5.1 字段清单（共 8 项）

**权威来源是源码本身**，升级 `vnpy_ctp` 后请重新执行：

```powershell
.venv\Scripts\python.exe -c "from vnpy_ctp import CtpGateway; import json; print(json.dumps(CtpGateway.default_setting, ensure_ascii=False, indent=2))"
```

`vnpy_ctp 6.7.11.4` 实测快照：

| # | 字段 | 类型 | 源码中的用途 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 用户名 | 文本 | `userid` | InvestorID，期货账户 |
| 2 | 密码 | 文本 | `password` | 交易密码 |
| 3 | 经纪商代码 | 文本 | `brokerid` | BrokerID，SimNow 为 `9999` |
| 4 | 交易服务器 | 文本 | `td_address` | 形如 `tcp://x.x.x.x:port`；不写协议前缀时自动补 `tcp://` |
| 5 | 行情服务器 | 文本 | `md_address` | 形如 `tcp://x.x.x.x:port`；同上自动补前缀 |
| 6 | 产品名称 | 文本 | `appid` | AppID，穿透式监管认证用 |
| 7 | 授权编码 | 文本 | `auth_code` | AuthCode，穿透式监管认证用 |
| 8 | 柜台环境 | 下拉（实盘 / 测试） | `production_mode = (柜台环境 == "实盘")` | 影响认证模式 |

> 第 6–8 项是穿透式监管（看穿式监管）要求，缺任一项会被柜台拒绝登录。
>
> 上表按 `vnpy_ctp 6.7.11.4` 实测导出，**共 8 项**。旧版构建计划曾把「产品信息」也计入（共 9 项），该字段在本版本 `default_setting` 与 `connect()` 中均不存在，多传会被静默忽略。

### 5.2 连接步骤

1. 启动程序，菜单 **交易 → 连接 CTP**
2. 填入上表字段，点击「连接」
3. 配置自动保存，下次连接可复用

当前 **SimNow 常见前置地址实测全部不可达**（`180.168.146.187` 的 10113 / 10131 / 10161 / 10211 / 10231 均 TCP 不通），真实登录需向期货公司索取最新前置地址，或使用 SimNow 官网当前公布的地址。

## 6. 数据落盘位置

VeighNa 的数据与配置**不在本仓库内**，默认落在 `C:\Users\<用户名>\.vntrader\`：

| 文件 | 内容 |
| --- | --- |
| `database.db` | SQLite 数据库（K 线、Tick、回测结果） |
| `ctp_connect.json` | CTP 连接配置（含账号密码，勿外传） |
| `vt_setting.json` | 全局设置 |
| `log/` | 运行日志 |
| `records/<交易日>/orders.csv` | **委托记录**（每次状态变化一行：提交中/未成交/全部成交/拒单…） |
| `records/<交易日>/trades.csv` | **成交记录**（一笔成交一行） |

该目录不在版本控制范围内。

### 6.1 委托 / 成交记录

运行期实时追加写，**崩溃、重启都不丢**；夜盘（21:00 之后）按
`utils.calendar.trading_day_of` 归到次日交易日。开关在 `config/runtime.json`：

```json
{ "record_orders": true, "record_trades": true }
```

| 文件 | 列 |
| --- | --- |
| `orders.csv` | `datetime, trading_day, vt_orderid, vt_symbol, gateway, direction, offset, type, price, volume, traded, status, reference` |
| `trades.csv` | `datetime, trading_day, tradeid, vt_orderid, vt_symbol, gateway, direction, offset, price, volume` |
| `spread_trades.csv` | `datetime, trading_day, spread, vt_orderid, direction, offset, near_symbol, far_symbol, near_price, far_price, spread_price, volume, gateway` |

**`spread_trades.csv` 就是"开仓价"所在**：把同一组合单的两条腿成交合成一行，
`spread_price = near_price - far_price`，`direction` 标成
`开多价差 / 开空价差 / 平多价差 / 平空价差`，一眼能看出每笔的开仓/平仓价差。
只有 `spreads.json` 里登记的两腿价差才会合成；手动单腿会只出现在 `trades.csv` 里。

去重口径：委托按 `(vt_orderid, status, traded, datetime)`——CTP 重连后会重推历史
委托，重复的不会写第二行；成交按 `tradeid`。

导出/汇总有两种方式：

**界面**：「功能 → 行情数据」面板顶部的「**导出交易记录 CSV…**」——选起止交易日、可选按价差/合约过滤，点「导出」；
默认输出到 `exports\trading_records\<日期>\`（起止不同则用 `<起>_<止>`），点「打开记录目录」可直接看原始落盘文件。

**命令行**：

```powershell
# 看有哪些交易日、各自多少行
.venv\Scripts\python.exe tools\export_trading_records.py --list

# 导出某交易日到 exports\trading_records\<日期>\
.venv\Scripts\python.exe tools\export_trading_records.py --date 2026-11-12

# 合并一段区间
.venv\Scripts\python.exe tools\export_trading_records.py --from 2026-11-01 --to 2026-11-13

# 只看某个价差（子串匹配价差名或合约代码）
.venv\Scripts\python.exe tools\export_trading_records.py --date 2026-11-12 --match PK
```

> 注意：程序**启动后**才开始记录，之前的历史委托/成交查不到（vnpy 的 OMS 只在内存里）。
> 需要历史数据请到柜台结算单里取。

## 7. 已知风险

| 编号 | 风险 | 应对 |
| --- | --- | --- |
| 1 | `vnpy_ctp` 与 Python 版本强绑定，升级 Python 会导致 CTP 失效 | 依赖全量钉死 + lock；升级前先确认官方 wheel（见 4.2） |
| 2 | SimNow 旧前置 IP 已不通，无法完成实盘层验证 | 联系期货公司要最新前置，或用 SimNow 官网当前地址 |
| 3 | 防病毒软件拦截 CTP DLL 加载 | 将 `F:\workspace\Vnpy\.venv` 加入白名单 |
| 4 | 本机若残留 PyQt5 / PySide2 等 Qt 运行时，可能与 PySide6 冲突导致 GUI 打不开 | 卸载或隔离其他 Qt 绑定 |
| 5 | 裸跑 `winget upgrade` 会把 PATH 打回前置状态 | 升级必须带完整 `--override`（见 4.1），升级后复核 `where.exe python` |
| 6 | 有人绕过 lock 用 `requirements.txt` 重建，导致依赖漂移 | 见第 3 节硬规则 |
| 7 | TA-Lib 安装失败会拖垮整个 vnpy 安装 | `TA-Lib 0.8.2` 有 `cp313-win_amd64` wheel；万一失败降级到 0.6.x |

## 8. 目录结构

```
F:\workspace\Vnpy\
├── .venv\                     虚拟环境（不入版本控制）
├── docs\plan\                 构建计划文档
├── strategies\                价差策略（CrossPeriodMRStrategy）
├── execution\                 执行层（两腿 / 交易所组合单）
├── gateway\                   CTP 网关（组合单回报容错 / 纯行情）
├── fees\                      费率表与手续费计算
├── recorder\                  行情采集（tick 落库 + 只读查询层）
├── tickbrowser\               GUI「功能 → 行情数据」面板
├── utils\                     交易日历 / 统计窗口 / 过滤器 / 价差配置 / 引擎接线
├── backtest\                  价差回测引擎（两腿 tick 合成）
├── tools\                     fee_table 生成脚本、tick 查看工具
├── tests\                     单元测试（python -m unittest）
├── config\spreads.json        用户维护的跨期合约对注册表
├── config\runtime.json        运行开关（是否边跑边落库 tick）
├── requirements.txt           依赖意图（不可用于重建）
├── requirements.lock.txt      依赖事实（重建唯一依据）
├── main.py                    GUI 启动器
├── run_strategy.py            跨期策略实盘入口
├── run_recorder.py            行情采集入口（tick 落库）
├── run_backtest.py            跨期策略回测入口
├── run.bat                    双击启动（含环境检查）
├── .gitignore
└── README.md                  本文件
```

## 9. 跨期均值回归策略

`vnpy_spreadtrading` 之上实现的分层均值回归策略（跨期价差），详细说明见
`docs/跨期策略使用说明.md`，设计口径见 `docs/plan/PLAN-spreadtrading-跨期均值回归策略实现.md`。

| 入口 | 用途 |
| --- | --- |
| **GUI** `run.bat` | 连柜台看行情/委托/持仓/资金、风控；**功能 → 行情数据** 浏览已采集的 tick 与合成价差；价差面板已接入 `CrossPeriodMRStrategy`，并按 `config/runtime.json` 边跑边落库 tick（详见使用说明 1.1 / 6.6） |
| `run_strategy.py --md-only --record --minutes 100` | **在线闭环**：只连行情前置，采 tick + 拼价差 + 策略纸面观察（不碰交易端） |
| `run_strategy.py --list` | 离线自检：校验 `config/spreads.json`、策略类与参数，不连柜台 |
| `run_strategy.py --spread <名字>` | 连柜台取行情，**只计算不发单**（日志打印每层止盈目标价） |
| `run_strategy.py --spread <名字> --live` | 真实报单 |
| `run_recorder.py --list` / `--minutes 30` | 纯采集：只连行情前置把 tick 落库（不跑策略） |
| `tools/inspect_ticks.py` | 查看已采集的 tick：概况 / 单合约明细 / 按天汇总 / **合成价差预览** |
| `tools/export_spread_csv.py --date 2026-11-12` | 按**交易日**导出价差 tick 为 CSV（`exports\spread_csv\<日期>\`；GUI「功能 → 行情数据 → 导出价差 CSV…」里可勾选价差、后台执行，导出时不影响看盘下单） |
| `run_backtest.py --list` / `--spread <名字> --start .. --end ..` | 价差回测（需两腿 tick 已入库） |

单元测试（不引入 pytest）：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

费率表由 `tools\build_fee_table.py` 从 `docs\全品种手续费表_交易所标准_20260919(1).xlsx`
生成 `fees\fee_table.json`（纯标准库解析，不新增依赖）。拿到期货公司实际加收后，直接覆盖
该 json 即可，代码无需改动。

## 10. 定时代理（交易日自动连接 / 核对 / 启停交易）

收盘后 CTP 断开、策略随之停止，次日需要人工重连、重新核对才能再开。
定时代理把这三步自动化：**连接 → 核对并纠正 → 开始交易**。

配置在 `config/schedule.json`，也可以在价差交易窗口点「定时任务」用表格编辑。
默认计划（周一到周五）：

```json
{
  "enabled": true,
  "tasks": [
    {"time": "08:49", "days": [1, 2, 3, 4, 5], "action": "purge",     "note": "清理隔夜委托缓存"},
    {"time": "08:50", "days": [1, 2, 3, 4, 5], "action": "connect",   "note": "连接 CTP"},
    {"time": "08:55", "days": [1, 2, 3, 4, 5], "action": "reconcile", "note": "核对并纠正本地账"},
    {"time": "08:58", "days": [1, 2, 3, 4, 5], "action": "start",     "note": "开始交易"}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `time` | `HH:MM`；内部每 10 秒扫描一次，命中当分钟后执行 |
| `days` | ISO 周几：1=周一 … 7=周日；支持 `"1-5"`、`"1,3,5"`、`"一,三,五"` |
| `action` | `purge` / `connect` / `reconcile` / `start` / `stop` |
| `enabled` | 单条任务开关；顶层 `enabled` 是总开关 |

行为约定：

- **`purge` 必须排在 `connect` 之前**：CTP 委托当日有效，程序跨日不重启时引擎 OMS 里
  还留着昨天的"未成交"记录。不清掉的话，`_bind_existing_exit_orders` 会误以为止盈单
  还挂在柜台，该层再也不补挂（2026-11-17 实测：11 个价差全天无单）。
  清理只丢**非当前交易日**的委托/成交，并清空策略的柜台占位；交易运行中会自动跳过。
- **同一任务当天只执行一次**，重启程序也不会重复触发（按「日期 + 时间 + 动作」去重）。
- `connect` 读 `~/.vntrader/ctp_connect.json`；文件缺失时跳过并写日志，不会瞎连。
- `reconcile` 依次：查资金 → 查持仓 → 确认核对 → `sync_all` 批量纠正。
  **任何一步失败就停住**，不会带着未核对的账去开始交易。
- `start` 前再次确认交易端与行情端都已连接，未连接则跳过。
- 已在交易中时不会重复 `start`；未在交易中时不会 `stop`。
- 只按周几判断，**不识别法定节假日**——长假需要手动关掉总开关或停用相关任务。

