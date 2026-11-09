# VeighNa 策略交易软件 · 构建计划

> 状态：**已执行 · 修订版 v4**（2026-10-27 执行完毕，V1–V5 全部通过，执行期实测偏差见 9.5）
> 工作区：`F:\workspace\Vnpy`
> 修订日期：2026-10-27
> 变更历史：
> - v1 初版
> - v2 纳入第一轮审阅意见，逐条实测核实（3 条被实测推翻，见 9.1）
> - v3 纳入第二轮增量意见 7 条，全部核实采纳（见 9.4）
> - v4 执行完毕；按执行期实测修正 3 处：run.bat 编码方案（5.3）、CTP 字段数 9→8（5.6）、V5 菜单判定标准（6 节）。详见 9.5

---

## 0. 目标与范围

### 本次交付边界（重要）

本次交付定义为**可启动、可加载网关、可落库**：

| 能力 | 本次是否交付 |
| --- | --- |
| 主程序 GUI 启动、菜单完整 | ✅ 交付 |
| CTP 网关成功装载、CTP 底层 DLL 正常加载 | ✅ 交付 |
| SQLite 数据库可读写、K 线/Tick 可落库 | ✅ 交付 |
| CTP **真实登录、查持仓、下单** | ❌ 不交付，需有效账号与前置地址 |

> 之所以划这条线：SimNow 常见前置 IP 实测全部不通（见 1 节），真实登录无法在本次闭环验证，避免交付时出现预期落差。

### 要做

- 一套可双击启动的 VeighNa 主程序（GUI）
- 接入 CTP 期货接口，可连接实盘 / SimNow 仿真
- 挂载 4 个应用模块：CTA 策略、CTA 回测、价差交易、风控管理
- SQLite 本地数据库（K 线、Tick、回测结果落库）
- 版本钉死 + 生成 lock 文件，保证环境可复现

### 不做（本次范围外）

- 不写具体策略逻辑
- 不写历史数据下载器
- 不配置除 SQLite 以外的数据库 / 中间件
- 不改动系统现有的 Python 3.12 环境（含不改其 PATH 优先级）
- 不初始化 git 仓库

---

## 1. 环境勘察结论（本机实测，2026-10-27）

| 项目 | 实测结果 |
| --- | --- |
| 工作区 `F:\workspace\Vnpy` | 原为空目录，非 git 仓库；现仅有 `docs/plan/` 计划文档 |
| 系统 Python | **3.12.10** → `C:\Users\Mesky\AppData\Local\Programs\Python\Python312` |
| 其他 Python | 3.11.16（位于 `F:\DIYAPP\钱包搭子\.tooling\`，别的项目专用，不复用） |
| pip | 25.0.1（可升级至 26.2.2） |
| 网络 · PyPI 直连 | 683 ms，可用 |
| 网络 · 清华镜像 | 220 ms，更快 |
| winget | v1.29.380，**`upgrade` 与 `install` 均支持 `--override`**（实测 `winget upgrade --help`） |
| winget 源内 Python | **3.13.15**，Publisher 为 Python Software Foundation，安装包指向 `python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe`，Release Date **2026-08-05**，SHA256 已核实 |
| winget 安装器参数 | 实测 manifest：`--scope user` 对应 `InstallAllUsers=0 **PrependPath=1**`（会改用户级 PATH，见 7 节 R6） |
| 3.13 残留检查 | **当前无任何残留**：`winget list` 空、`Programs\Python\` 下仅 `Launcher` 与 `Python312`、`py -0p` 仅 3.12 与 uv 的 3.11，`C:\Program Files\Python*` 不存在 |
| 用户级 py launcher | 已存在（`Programs\Python\Launcher`），安装 3.13 会更新它而非新建 |
| F 盘空间 | 3631 GB 空闲 |
| SimNow 前置 `180.168.146.187` | 10113 / 10131 / 10161 / 10211 / 10231 **全部 TCP 不通** |
| vnpy.com / simnow.com.cn | 可达（200 / 202） |

---

## 2. 关键约束：Python 版本被 vnpy_ctp 锁死

`vnpy_ctp` 是 C++ 扩展（封装上期技术 CTP API），PyPI 上的 wheel 覆盖面极窄：

| vnpy_ctp 版本 | 提供的 wheel |
| --- | --- |
| 6.7.11.4（最新） | `cp313-win_amd64` |
| 6.7.11.3 / 6.7.11.2 | `cp310-win_amd64`、`cp313-win_amd64` |
| 更早版本 | 同上，无 cp311 / cp312 |

**结论：没有任何 cp312 wheel。** 当前系统 Python 3.12 **装不上 CTP 网关**，除非从 sdist 本地编译（需 MSVC + CTP 静态库，成本高、易失败）。

因此必须另装 Python，且两条可行路线：

| 路线 | CTP 接口版本 | 评价 |
| --- | --- | --- |
| Python 3.13 + vnpy_ctp 6.7.11.4 | 最新 | ✅ **已选定** |
| Python 3.10 + vnpy_ctp 6.7.11.3 | 次新 | 备选，生态更保守 |

vnpy 4.5.0 官方声明支持 Python 3.10 / 3.11 / 3.12 / 3.13。

---

## 3. 依赖版本方案（已用 pip dry-run 对 cp313/win_amd64 全量解析通过，exit=0）

### 3.1 直接依赖

| 包 | 版本 | 说明 |
| --- | --- | --- |
| vnpy | 4.5.0 | 核心框架 |
| vnpy_ctp | 6.7.11.4 | CTP 网关，绑定 cp313 |
| vnpy_ctastrategy | 1.4.1 | CTA 策略引擎 |
| vnpy_ctabacktester | 1.3.0 | 回测引擎 |
| vnpy_spreadtrading | 1.4.0 | 价差交易 |
| vnpy_riskmanager | 2.0.0 | 风控 |
| vnpy_sqlite | 1.1.3 | 数据库驱动 |

### 3.2 必须钉死版本的间接依赖

pip 不加约束会拉来与 vnpy 4.5.0 未验证过的新大版本（下表"自由解析"列为实测值）：

| 包 | vnpy 4.5.0 的约束 | 自由解析结果 | 钉死为 | 原因 |
| --- | --- | --- | --- | --- |
| pandas | `>=2.2.3` | 3.1.0 | **2.3.3** | pandas 3.x 有破坏性变更，vnpy 未适配 |
| peewee | `>=3.17.9` | 4.5.3 | **3.19.0** | `vnpy_sqlite` 只适配 peewee 3.x，4.x 直接报错 |
| plotly | `>=6.0.0` | 7.1.0 | **6.9.0** | 7.x 跨大版本，影响策略 K 线图 |
| TA-Lib | `>=0.6.4` | 0.8.2 | **0.8.2** | 只有下界约束，不钉会随官方发版漂移 |
| pyqtgraph | `>=0.13.7` | 0.14.0 | **0.13.7** | 0.14 跨小版本未验证，且 vnpy 发布时即为 0.13.7 |
| numpy | `>=2.2.3` | 2.5.4 | **`>=2.2.3,<3`** | 补全下界，同时规避未知的 3.x |

所有钉死版本均已在 PyPI 上实测存在（`pandas 2.3.3` / `peewee 3.19.0` / `plotly 6.9.0` / `TA-Lib 0.8.2` / `pyqtgraph 0.13.7` / `numpy 2.5.4`）。

### 3.3 PySide6 无需重复钉死的依据

`vnpy 4.5.0` 的 `requires_dist` 中已写明 **`pyside6==6.8.2.1`（精确锁定，非范围）**，pip 必然装这个版本，再在 requirements 里写一遍属于冗余。

实测该版本 wheel 标签为 `cp39-abi3`，兼容 Python 3.13。

> 其余纯 Python 间接依赖（deap / loguru / nbformat / qdarkstyle / qrcode / requests / tqdm / tzlocal / pyzmq）风险低，不单独钉死，由 3.4 的 lock 文件兜底。

### 3.4 两个 requirements 文件的语义（必须严格遵守）

| 文件 | 角色 | 用途 | 是否可作重建依据 |
| --- | --- | --- | --- |
| `requirements.txt` | **意图声明**（范围 / 天花板 / 钉死原因） | 人工审阅、升级时看"为什么钉这个版本" | ❌ **不可以** |
| `requirements.lock.txt` | **事实快照**（`pip freeze` 全量精确版本） | 重建环境的**唯一依据** | ✅ 唯一入口 |

**硬规则**：

```powershell
# ✅ 重建环境只用 lock
pip install -r requirements.lock.txt

# ❌ 禁止用 requirements.txt 重建
pip install -r requirements.txt
```

原因：`requirements.txt` 里 `numpy>=2.2.3,<3` 这类范围约束，在未来几个月可能漂到 2.6/2.7；PySide6 未显式钉死，靠 lock 兜住。**绕过 lock 直接装 requirements，等于放弃了可复现性。**

`requirements.txt` 仅在**有意升级**时修改，改完重新验证 → 重新 `pip freeze` 生成新 lock。

---

## 4. 实施步骤

### 阶段 0 · 装 Python 3.13

#### 0.1 安装前检查（幂等性防护）

```powershell
winget list --id Python.Python.3.13          # 预期：No installed package found
Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -ErrorAction SilentlyContinue
py -0p                                       # 预期列表里没有 3.13
```

**若已有 3.13 残留（哪怕已损坏）**：必须先 `winget uninstall --id Python.Python.3.13` 卸载干净再装。否则 `--override` 会走 modify / repair 路径，行为不可控。

> 实测当前环境：三项检查均干净，无残留。

#### 0.2 安装（从源头阻止 PATH 被前置）

```powershell
winget install --id Python.Python.3.13 --exact --scope user --silent `
  --accept-package-agreements --accept-source-agreements `
  --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0"
```

`--override` 覆盖 manifest 默认的 `PrependPath=1`，避免 Python313 被插到用户 PATH 最前面抢走 `python` 命令。

#### 0.3 安装后复核（三项都要过）

```powershell
py -0p                 # ① 应出现 -V:3.13，且 3.12 仍在列表中
py -3.13 -V            # ② 应输出 3.13.15
where.exe python       # ③ 应仍指向 Python312（PATH 未被前置）
```

第 ① 项单独必要：`Include_launcher=1 InstallLauncherAllUsers=0` 装的是**用户级** launcher，若系统里已有旧版系统级 launcher，版本发现可能仍走旧注册表 —— 这一步能提前暴露。

#### 0.4 建虚拟环境

```powershell
py -3.13 -m venv F:\workspace\Vnpy\.venv
```

#### 0.5 补丁版升级的维护规则（长期负担，必读）

`--override` **不会持久化**。winget manifest 里的 `PrependPath=1` 是包定义的一部分，当 3.13.15 升到 3.13.16 时，裸跑 `winget upgrade` 会重新走 manifest 默认参数，**PATH 又被前置**。

**实测 `winget upgrade` 本身支持 `--override`**，所以升级必须带上完整参数：

```powershell
# ✅ 正确升级方式
winget upgrade --id Python.Python.3.13 --exact --silent `
  --accept-package-agreements --accept-source-agreements `
  --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0"

# 升级后必须复核
where.exe python       # 仍应指向 Python312
py -3.13 -V
```

```powershell
# ❌ 禁止裸跑，会把 PATH 打回前置状态
winget upgrade --id Python.Python.3.13
```

此规则同步写入 README 的「维护」章节。

### 阶段 1 · 安装依赖

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -U pip
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install -r F:\workspace\Vnpy\requirements.txt `
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

下载量约 500 MB，大头是 PySide6。

**镜像失败兜底**：若报 `No matching distribution` / 404（最新版可能尚未同步到清华镜像），对报错的那个包去掉 `-i` 参数、改用官方源单独重试：

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip install vnpy_ctp==6.7.11.4
```

### 阶段 2 · 生成启动程序与配套文件

见第 5 节，共 5 个文件。

### 阶段 3 · 验证 + 生成 lock

见第 6 节。验证全部通过后：

```powershell
F:\workspace\Vnpy\.venv\Scripts\python.exe -m pip freeze > F:\workspace\Vnpy\requirements.lock.txt
```

### 阶段 4 · 清理

- 清理临时脚本与 dry-run 产物（`%TEMP%\vnpy_*`）
- pip 缓存位于 `%LOCALAPPDATA%\pip\Cache`，**可选**清理，不影响正确性
- 确认仓库内只有预期文件

---

## 5. 待生成文件清单

```
F:\workspace\Vnpy\
├── .venv\                             虚拟环境（gitignore）
├── docs\plan\
│   └── PLAN-vnpy-ctp最小策略交易终端.md   本文件
├── requirements.txt                   依赖意图（钉死已知风险项，不可用于重建）
├── requirements.lock.txt              依赖事实（pip freeze，重建唯一依据）
├── main.py                            启动器（唯一必需代码文件）
├── run.bat                            双击启动（含环境检查）
├── .gitignore
└── README.md                          安装、维护与连接说明
```

### 5.1 requirements.txt

```
# ===== VeighNa 核心框架 =====
vnpy==4.5.0

# ===== CTP 交易接口（仅提供 cp313 wheel，必须运行在 Python 3.13）=====
vnpy_ctp==6.7.11.4

# ===== 应用模块 =====
vnpy_ctastrategy==1.4.1
vnpy_ctabacktester==1.3.0
vnpy_spreadtrading==1.4.0
vnpy_riskmanager==2.0.0

# ===== 数据库驱动（sqlite，装好即默认生效）=====
vnpy_sqlite==1.1.3

# ===== 关键间接依赖钉死 =====
# vnpy 4.5.0 对这些只给了宽泛下界，放任会被解析到未验证的新大版本
pandas==2.3.3
peewee==3.19.0
plotly==6.9.0
TA-Lib==0.8.2
pyqtgraph==0.13.7
numpy>=2.2.3,<3

# 注 1：PySide6 不在此钉死 —— vnpy 4.5.0 已用 pyside6==6.8.2.1 精确锁定
# 注 2：本文件仅表达意图，重建环境请用 requirements.lock.txt
```

### 5.2 main.py

```python
"""VeighNa 最小可运行交易终端。

主窗口提供：
    交易   —— CTP 网关连接、委托/成交/持仓监控、手动下单
    应用   —— CTA 策略、CTA 回测、价差交易、风控管理
"""

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp

from vnpy_ctp import CtpGateway
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_spreadtrading import SpreadTradingApp
from vnpy_riskmanager import RiskManagerApp


def main() -> None:
    qapp = create_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # CTP 接口：连接参数在界面「交易 -> 连接 CTP」中填写，
    # 保存后可复用，落盘位置 ~/.vntrader/ctp_connect.json
    main_engine.add_gateway(CtpGateway)

    # 应用模块
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(SpreadTradingApp)
    main_engine.add_app(RiskManagerApp)

    window = MainWindow(main_engine, event_engine)
    window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
```

### 5.3 run.bat

> **v4 更正（执行期实测）**：原方案 `chcp 65001 >nul` + UTF-8 中文提示**会导致脚本崩溃**。cmd 逐行读取批处理文件时按当前代码页计算偏移，中途 `chcp` 切到 65001 后偏移错乱，把中文行的后半截当成命令执行，实测报 `'错误]' is not recognized as an internal or external command`，脚本立即退出、GUI 从未启动。
>
> 修正：**文件以系统代码页 GBK(CP936) 保存，不切换代码页**。

```bat
@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 虚拟环境不存在，请先按 README.md 的「安装」步骤操作。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请检查上方报错信息。
    pause
)
```

> **保存编码**：GBK（CP936）+ CRLF + 无 BOM。
> - 存成 UTF-8 无 BOM：cmd 按 936 解码，中文显示乱码
> - 存成 UTF-8 BOM：首行 `@echo off` 被 BOM 污染，报 `'﻿@echo' is not recognized`
> - 保留 `chcp 65001`：见上文，脚本崩溃

### 5.4 .gitignore

```
.venv/
__pycache__/
*.py[cod]
*.db
*.log
```

> 说明：vnpy 的数据默认落在 **`C:\Users\<用户名>\.vntrader\`**（不在本仓库内），故无需忽略 `.vntrader/`；保留 `*.db` 仅为防止有人把数据库误拷进项目目录。

### 5.5 README.md 必备章节

1. **环境要求**：必须 Python 3.13（`vnpy_ctp` 只发 cp313 wheel），并说明 3.12 为什么不行
2. **安装三步**：装 Python（含完整 `--override` 命令）→ 建 venv → `pip install -r requirements.txt`
3. **两个 requirements 文件的语义**：直接引用本文档 3.4 节的表格与硬规则
4. **维护**：补丁版升级必须带 `--override`（见 0.5 节），升级后复核 `where.exe python`
5. **CTP 连接配置**：**字段以源码输出为准**，附下述生成命令 + 当前快照
6. **数据落盘位置**：`~/.vntrader/`（`database.db`、`ctp_connect.json`），不在版本控制内
7. **已知风险**：SimNow 旧前置不通等

### 5.6 CTP 连接配置项（共 8 项）

**权威来源是源码本身**，升级 `vnpy_ctp` 后请重新执行：

```powershell
.venv\Scripts\python.exe -c "from vnpy_ctp import CtpGateway; import json; print(json.dumps(CtpGateway.default_setting, ensure_ascii=False, indent=2))"
```

当前快照（**v4：在装好的 vnpy_ctp 6.7.11.4 上实测导出**）：

| # | 字段 | 说明 |
| --- | --- | --- |
| 1 | 用户名 | InvestorID，期货账户 |
| 2 | 密码 | 交易密码 |
| 3 | 经纪商代码 | BrokerID，SimNow 为 `9999` |
| 4 | 交易服务器 | td_address，形如 `tcp://x.x.x.x:port` |
| 5 | 行情服务器 | md_address，形如 `tcp://x.x.x.x:port` |
| 6 | 产品名称 | 映射到 `appid`，穿透式监管认证用 |
| 7 | 授权编码 | 映射到 `auth_code`，穿透式监管认证用 |
| 8 | 柜台环境 | 下拉选「实盘 / 测试」，映射到 `production_mode = (柜台环境 == "实盘")` |

> 第 6–8 项是穿透式监管（看穿式监管）要求，缺任一项会被柜台拒绝登录。配置文件落盘在 `~/.vntrader/ctp_connect.json`。
>
> **v4 更正**：v3 此处记为 9 项、含「产品信息」，**实测不存在**。`vnpy_ctp 6.7.11.4` 的 `default_setting` 只有上表 8 个键，`CtpGateway.connect()` 也只读取这 8 个键；传「产品信息」会被静默忽略（6.1 节的 V3.5 脚本里多传了该键，实测仍通过，即为证据）。

---

## 6. 验证方案

验证目标：**做到启动 GUI + 模块冒烟测试**（不涉及账号）。

| 序号 | 验证项 | 判定标准 |
| --- | --- | --- |
| V1 | Python 版本 | `.venv\Scripts\python.exe -V` 输出 3.13.x |
| V2 | 关键包导入 + 版本 | 逐个 import `vnpy` / `vnpy_ctp` / `vnpy_sqlite` / `talib` / `PySide6`，**并打印各自版本号**；`vnpy_ctp` 的 import 会触发底层 CTP DLL 加载，此步通过即可证架构与杀软无阻碍 |
| V3 | 引擎装载 | `add_gateway(CtpGateway)` + 4 个 `add_app()` 无异常，`get_all_apps()` 返回 4 项 |
| V3.5 | 网关健壮性（见 6.1） | **15 秒墙钟上限内进程正常退出（returncode=0）且无 traceback** |
| V4 | 数据库可用 | `get_database()` 拿到 sqlite Database 实例，能建表/写读一行 |
| V5 | GUI 启动 | 双击 `run.bat` 弹出主窗口，菜单栏为 **系统 / 功能 / 配置 / 微信 / 帮助**（vnpy 4.5.0 实测结构，v3 写的"交易 / 应用"是旧版假设），「系统 → 连接CTP」存在、「功能」下含 4 个应用，无报错弹窗 |

V1–V4 用临时脚本执行（放 `%TEMP%`，不落盘到仓库）；V5 由人工肉眼确认。

验证通过后追加一步：`pip freeze > requirements.lock.txt`。

CTP 真实登录（连接、查持仓、下单）**不在本次验证范围**，需等提供有效账号与前置地址。

### 6.1 V3.5 的具体实现（必须设墙钟超时）

**为什么不能用"connect 返回错误码"作为判定**：CTP 的 `connect()` 是异步的，打不可达地址时底层不会立刻返回，`CtpGateway` 要等 CTP API 的登录回调超时（常见 30s 以上，甚至更久）。同步等待会直接把验证脚本挂死。

**正确做法：子进程 + 墙钟超时**。子脚本内容（`%TEMP%\v35_ctp_gateway.py`）：

```python
import time
from vnpy.event import EventEngine
from vnpy_ctp import CtpGateway

gateway = CtpGateway(EventEngine(), "CTP_V35")
try:
    gateway.connect({
        "用户名": "test",
        "密码": "test",
        "经纪商代码": "9999",
        "交易服务器": "tcp://127.0.0.1:1",   # 刻意不可达
        "行情服务器": "tcp://127.0.0.1:1",
        "产品名称": "test",
        "授权编码": "test",
        "柜台环境": "测试",
        "产品信息": "",
    })
    time.sleep(8)          # 给异步登录流程留出触发时间
finally:
    gateway.close()        # 必须释放，避免残留 DLL 句柄影响后续 V5
```

外层驱动（超时判定）：

```python
import subprocess, sys, pathlib

script = pathlib.Path(r"%TEMP%\v35_ctp_gateway.py")
try:
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=15,
    )
    ok = proc.returncode == 0 and "Traceback" not in proc.stderr
except subprocess.TimeoutExpired:
    ok = False   # 超时判失败
```

判定口径：**15 秒内进程正常退出且 stderr 无 traceback** 即为通过。不校验"是否连上"（本来就连不上），只校验"不会崩溃、不会挂死"。

---

## 7. 风险与应对

| 编号 | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| R1 | vnpy_ctp 与 Python 版本强绑定 | 升级 Python 会导致 CTP 失效 | requirements 全量钉死；升级前先确认官方 wheel |
| R2 | SimNow 旧前置 IP 已不通 | 无法完成实盘层验证 | 联系期货公司要最新前置，或用 SimNow 官网当前地址 |
| R3 | TA-Lib 若装失败 | vnpy 整个装不上 | 已确认 0.8.2 有 cp313 win_amd64 wheel；万一失败则降级 0.6.x |
| R4 | 首次启动 PySide6 报错 | GUI 打不开 | 检查是否有多个 Qt 运行时（系统残留的 PyQt5/PySide2）冲突 |
| R5 | 防病毒软件拦截 | CTP dll 加载失败 | 将 `F:\workspace\Vnpy\.venv` 加入白名单 |
| R6 | winget 安装会改用户级 PATH | 新终端里 `python` 变成 3.13，影响其他项目 | 安装时显式 `PrependPath=0`；装后 `where.exe python` 复核 |
| R7 | 清华镜像同步滞后 | 最新版包拉不到，安装中断 | 对报错包去掉 `-i` 改用官方源重试 |
| R8 | 传递依赖静默漂移 | 重建环境时行为不一致 | 生成 `requirements.lock.txt`，重建一律用 lock |
| **R9** | **Python 补丁版升级导致 PATH 回退** | `--override` 不持久化，裸跑 `winget upgrade` 会把 `PrependPath=1` 带回，PATH 再次前置 | 升级必须带完整 `--override`（见 0.5）；升级后复核 `where.exe python`。已写入 README「维护」章节 |
| **R10** | **有人绕过 lock 用 requirements.txt 重建** | 范围约束漂移，可复现性失效 | README 与 3.4 节均写明硬规则；`requirements.txt` 文件头也加注释提示 |

---

## 8. 已确认决策记录

| 决策项 | 选择 | 时间 |
| --- | --- | --- |
| Python 版本路线 | **Python 3.13 + vnpy_ctp 6.7.11.4** | 2026-10-27 |
| Python 安装方式 | **winget 装到系统（用户级，PrependPath=0）** | 2026-10-27 |
| 验证深度 | **启动 GUI + 模块冒烟测试**（不含 CTP 真实登录） | 2026-10-27 |
| 计划文档位置 | `docs/plan/` 目录 | 2026-10-27 |
| 计划文档命名规则 | `PLAN-<接口/技术栈>-<计划简述>.md`，文件名本身说明计划内容 | 2026-10-27 |
| v2 修订 | 纳入第一轮审阅意见，逐条实测核实 | 2026-10-27 |
| v3 修订 | 纳入第二轮增量意见（升级路径 / lock 语义 / V3.5 超时） | 2026-10-27 |

---

## 9. 审阅意见核实记录

### 9.1 第一轮：经实测不成立 / 部分不成立的意见

| 意见 | 审阅方主张 | 实测结果 | 证据 |
| --- | --- | --- | --- |
| 第 2 条 | "Python 3.13 补丁版远未到 .15，版本号存疑" | ❌ **不成立** | `winget show` 输出 Version: 3.13.15 / Publisher: Python Software Foundation / 安装包 `python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe` / Release Date **2026-08-05**。审阅方按 2025 年时间线判断，当前系统日期为 2026-10-27 |
| 第 3 条 | "peewee 主线在 3.17.x，3.19.0 是否存在请核实" | ❌ **不成立** | PyPI 实测 `peewee==3.19.0 exists=True`（latest 4.5.3）。pandas 2.3.3 / plotly 6.9.0 / TA-Lib 0.8.2 / pyqtgraph 0.13.7 同样均存在 |
| 第 5 条 | "CTP 连接配置实为 7 项" | ⚠️ **部分不成立，实为 8 项** | vnpy_ctp 6.7.11.4 的 `default_setting` 含：用户名、密码、经纪商代码、交易服务器、行情服务器、产品名称、授权编码、**柜台环境**。审阅方漏掉「柜台环境」一项。**v4 更正**：v3 曾把「产品信息」也计入（共 9 项），执行期实测该字段在 `default_setting` 与 `connect()` 中均不存在，属 v3 误记 |

### 9.2 第一轮：经核实成立、已采纳

| 意见 | 采纳情况 | 落点 |
| --- | --- | --- |
| 第 1 条 | ✅ 成立，比原意见更严重 | manifest 实测 `InstallAllUsers=0 PrependPath=1`，PATH 被**置前**；改为 `--override PrependPath=0` → 阶段 0、R6 |
| 第 4 条 | ✅ 成立 | 风险 R7 + 阶段 1 兜底命令 |
| 第 6 条 | ✅ 部分采纳 | TA-Lib 钉死 `==0.8.2`；PySide6 经查是 vnpy 精确锁定，再钉属冗余，依据见 3.3 |
| 第 7 条 | ✅ 成立 | 3.4 节 lock 策略 + 阶段 3 生成 lock |
| 第 8 条 | ✅ 成立 | `numpy>=2.2.3,<3` |
| 第 9 条 | ✅ 成立 | run.bat 增加 venv 检查与中文报错 |
| 第 10 条 | ✅ 成立 | V2 打印版本号；新增 V3.5 |
| 第 11 条 | ✅ 成立 | 5.4 节说明数据落盘在 `~/.vntrader/` |
| 第 12 条 | ✅ 成立 | 阶段 0 复核三项 |
| 第 13 条 | ✅ 成立 | 决策记录时间列改为实际日期 |
| 第 14 条 | ✅ 成立 | 阶段 4 措辞改为"pip 缓存可选清理" |
| 第 15 条 | ✅ 成立 | 第 0 节交付边界表 |

### 9.3 第一轮之外、实测发现的增补

- **`pyqtgraph` 也需钉死**（审阅未提）：vnpy 约束 `>=0.13.7`，自由解析会装 0.14.0，已钉 `0.13.7`
- **`--override` 兜底方案**：若被 winget 拒绝，给出装后手工清理用户 PATH 的退路
- **vnpy_ctp 9 项配置的穿透式监管说明**：第 6–9 项缺任一项会被柜台拒登

### 9.4 第二轮增量意见核实（7 条，全部成立并采纳）

| # | 意见 | 核实结果 | 落点 |
| --- | --- | --- | --- |
| 1 | `--override` 不持久化，补丁版升级会打回原形 | ✅ **成立**。实测 `winget upgrade --help` 确认 **upgrade 本身就支持 `--override`**，因此无需整包重装，升级时带上同样参数即可 | 新增 0.5 节「补丁版升级维护规则」、风险 R9、README 必备章节第 4 条 |
| 2 | 装前先确认无旧 3.13 残留 | ✅ **成立**。实测当前环境无残留（winget list 空 / `Programs\Python\` 仅 `Launcher` + `Python312` / `py -0p` 仅两项），检查仍写入做幂等防护 | 新增 0.1 节 |
| 3 | 复核不止 `where.exe python` | ✅ **成立**。补充 `py -0p`（确认 launcher 发现新版本，暴露旧注册表问题）与 `py -3.13 -V` | 0.3 节三项复核 |
| 4 | requirements 与 lock 语义要写死 | ✅ **成立**。明确 lock 为重建唯一依据，并警告不可绕过 | 3.4 节表格 + 硬规则、风险 R10、README 必备章节第 3 条 |
| 5 | V3.5 要设墙钟超时 | ✅ **成立且关键**。CTP `connect()` 异步，登录回调超时可达 30s+，同步等待会挂死 | 6.1 节给出子进程 + `timeout=15` 完整实现，判定改为"15 秒内正常退出且无 traceback"，`finally` 中 `gateway.close()` |
| 6 | CTP 字段表直接从源码抄 | ✅ **成立**。改为以 `CtpGateway.default_setting` 输出为权威来源，文档仅存快照 | 5.6 节加入生成命令 |
| 7 | 风险表补"升级导致 PATH 回退" | ✅ **成立**（第二轮意见写时 v2 已有 R6–R8，但确实缺升级路径这一条） | 新增 R9 |

---

### 9.5 执行期记录与实测偏差（v4）

执行日期 2026-10-27，按第 4 节顺序执行完毕。

#### 验收结果

| 项 | 结果 | 实测证据 |
| --- | --- | --- |
| V1 | ✅ | `Python 3.13.15 (tags/v3.13.15:4061bc4, Aug 5 2026) [MSC v.1944 64 bit (AMD64)]` |
| V2 | ✅ | vnpy 4.5.0 / vnpy_ctp 6.7.11.4 / vnpy_sqlite 1.1.3 / TA-Lib 0.8.2 / PySide6 6.8.2.1；CTP 底层 DLL 加载成功 |
| V3 | ✅ | `get_all_apps()` 返回 4 项：CtaStrategyApp / CtaBacktesterApp / SpreadTradingApp / RiskManagerApp |
| V3.5 | ✅ | 子进程 15 s 墙钟内 returncode=0，stderr 为空 |
| V4 | ✅ | `vnpy_sqlite.sqlite_database.SqliteDatabase`，写入/读回/删除 1 根 K 线成功 |
| V5 | ✅ | `run.bat` 拉起 GUI，窗口标题 `VeighNa Trader 社区版 - 4.5.0`；UI Automation 读到菜单 5 项 |

#### 偏差 1 · run.bat 编码导致脚本崩溃（已改 5.3）

v3 给的 UTF-8 + `chcp 65001` 方案**实测直接崩溃**：cmd 按代码页计偏移，中途切换后错位，把 `echo [错误] …` 的后半截当命令，报 `'错误]' is not recognized as an internal or external command`，GUI 从未启动。改为 **GBK(CP936) 编码 + 不切代码页**，中文提示与报错分支实测正常。

#### 偏差 2 · CTP 配置字段是 8 项不是 9 项（已改 5.6）

v3 记的「产品信息」在 `vnpy_ctp 6.7.11.4` 的 `default_setting` 与 `CtpGateway.connect()` 中**均不存在**。已按实测改为 8 项，并回填每个字段在源码中的实际用途。

#### 偏差 3 · V5 菜单判定标准写错（已改 6 节）

v3 判定标准写"菜单含交易 / 功能 / 应用"，是旧版 vnpy 的结构。vnpy 4.5.0 `mainwindow.py` 实测为 **系统 / 功能 / 配置 / 微信 / 帮助**：

- `系统` → `连接CTP`（按 `get_all_gateway_names()` 动态生成，实测为 `['CTP']`）、`退出`
- `功能` → 4 个应用：`CTA策略`、`CTA回测`、`价差交易`、`交易风控`
- `配置`、`微信` → 直接 QAction，非子菜单
- `帮助` → `查询合约`、`还原窗口` 等

判定标准已改为按实测结构核对。

#### 其他实测发现（不改计划，仅知会）

- **依赖解析结果与 3.1/3.2 的钉死值完全一致**：pandas 2.3.3、peewee 3.19.0、plotly 6.9.0、TA-Lib 0.8.2、pyqtgraph 0.13.7、numpy 2.5.4、pyside6 6.8.2.1；lock 共 51 个包。
- **`PrependPath=0` 生效**：安装后用户级 PATH 中无 `Python313`，`where.exe python` 仍指向 `Python312`。
- **`py` launcher 默认版本变成 3.13**：`py -0p` 中 `-V:3.13` 带 `*`。不带版本号的 `py xxx.py` 会用 3.13 执行，`python` 命令不受影响。已写入 README 4.4。
- `~/.vntrader/` 生成 `database.db`、`vt_setting.json`、`risk_manager_setting.json`、`wechat_setting.json`、`log/`；V3.5 产生的 `ctp_v35/` 流文件与 V4 的测试表已在收尾清理。
- winget 与 Python 安装器在 `%TEMP%` 留下的安装日志属安装器自身产物，未纳入清理范围。

---

## 10. 后续可扩展（本次不做）

- 编写自定义 CTA 策略并放入 `strategies/` 目录
- 历史数据批量下载脚本（从 CTP 行情前置或第三方数据源）
- 配置 MySQL / PostgreSQL / MongoDB 替代 SQLite
- 打包成 exe（PyInstaller）便于分发
- 接入 SimNow 7×24 环境做持续仿真验证
