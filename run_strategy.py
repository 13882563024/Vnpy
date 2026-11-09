"""跨期均值回归策略 · 实盘启动入口。

做的事情（按顺序）：

1. 把工作目录切到项目根（``SpreadStrategyEngine`` 用 ``Path.cwd()/strategies`` 发现策略类）；
2. 只用 vnpy 的标准引擎栈：``MainEngine`` + ``SpreadTradingApp`` + ``SpreadCtpGateway``；
3. 按 ``config/spreads.json`` 注册价差（已有同名价差则复用并核对 legs）；
4. **替换** ``SpreadAlgoEngine.algo_class = CrossPeriodAlgo`` 并断言替换成功（方案 R8 自检）；
5. 为每个价差添加 ``CrossPeriodMRStrategy``，初始化并启动；
6. 若给出 ``--live`` 且能读到 CTP 连接配置，则真实报单；否则只计算不发单（日志打印每层目标价）。

常用命令::

    # 离线自检：不连柜台，只校验配置/策略加载/参数，然后退出
    .venv\\Scripts\\python.exe run_strategy.py --no-connect --list

    # 只计算不发单（连柜台拿行情，日志打印每层止盈目标价）
    .venv\\Scripts\\python.exe run_strategy.py --spread A_2611_2701

    # 真实报单
    .venv\\Scripts\\python.exe run_strategy.py --spread A_2611_2701 --live
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vnpy.event import EventEngine                                    # noqa: E402
from vnpy.trader.constant import Exchange                             # noqa: E402
from vnpy.trader.engine import MainEngine                             # noqa: E402
from vnpy.trader.utility import extract_vt_symbol, load_json           # noqa: E402
from vnpy_spreadtrading import SpreadTradingApp                       # noqa: E402

from execution.cross_period_algo import CrossPeriodAlgo               # noqa: E402
from gateway.ctp_spread_gateway import SpreadCtpGateway               # noqa: E402
from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy  # noqa: E402
from utils.engine_setup import (                                      # noqa: E402
    attach_tick_recorder,
    ensure_strategy_classes,
    flush_tick_recorder,
    install_algo_class,
    register_spreads as setup_register,
    setup_spread_trading,
    strategy_setting_for,
    unknown_strategy_keys,
)
from utils.spread_config import (                                     # noqa: E402
    EXECUTOR_EXCHANGE_SPREAD,
    SpreadConfig,
    load_raw,
    parse_spread_config,
)

STRATEGY_CLASS = "CrossPeriodMRStrategy"
CONNECT_FILENAME = "ctp_connect.json"
CONTRACT_WAIT_SECONDS = 60.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨期均值回归策略实盘启动")
    parser.add_argument("--spread", action="append", default=[],
                        help="要运行的价差名（可多次指定）；默认运行 config/spreads.json 全部")
    parser.add_argument("--setting", action="append", default=[],
                        help="覆盖策略参数，格式 key=value（可多次）")
    parser.add_argument("--live", action="store_true",
                        help="真实报单；缺省只计算不发单（live_trading=False）")
    parser.add_argument("--md-only", action="store_true",
                        help="只连行情前置（不连交易端、无法报单）：纸面观察 + 采集 tick")
    parser.add_argument("--record", dest="record", action="store_true", default=None,
                        help="强制开启 tick 落库（默认按 config/runtime.json）")
    parser.add_argument("--no-record", dest="record", action="store_false",
                        help="本次不落库 tick")
    parser.add_argument("--close-only", action="store_true", help="只平不开")
    parser.add_argument("--no-connect", action="store_true", help="不连接柜台（离线自检）")
    parser.add_argument("--minutes", type=float, default=0.0,
                        help="运行时长（分钟）；0 = 一直跑到 Ctrl+C")
    parser.add_argument("--list", action="store_true", help="只打印配置与自检结果，然后退出")
    parser.add_argument("--wait", type=float, default=CONTRACT_WAIT_SECONDS,
                        help=f"等待合约查询完成的秒数（默认 {CONTRACT_WAIT_SECONDS:g}）")
    return parser.parse_args(argv)


def parse_overrides(items: list[str]) -> dict:
    overrides: dict = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"参数覆盖格式应为 key=value：{item}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = _coerce(value.strip())
    return overrides


def _coerce(text: str):
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    for caster in (int, float):
        try:
            return caster(text)
        except ValueError:
            continue
    return text


def load_spread_table(config_path: Path) -> dict[str, SpreadConfig]:
    raw = load_raw(config_path)
    table: dict[str, SpreadConfig] = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            continue
        config = parse_spread_config(name, item)
        table[name] = config
        for problem in config.validate():
            print(f"[配置告警] {name}：{problem}")
    return table


def self_check(configs: dict[str, SpreadConfig], selected: list[str]) -> int:
    """离线自检：注册表完整性 + 策略类可实例化（不涉及任何网关）。"""
    from vnpy_spreadtrading.template import SpreadStrategyTemplate

    problems = 0
    if not configs:
        print("[错误] config/spreads.json 为空或无法解析")
        return 1

    for name, config in configs.items():
        if selected and name not in selected:
            continue
        print(f"\n价差 {name}")
        print(f"  腿        : {config.near_symbol} / {config.far_symbol}")
        print(f"  主动腿    : {config.active_symbol}")
        print(f"  执行器    : {config.executor}（组合合约：{config.exchange_spread or '未配置'}）")
        print(f"  乘数/跳   : {config.size:g} / {config.pricetick:g}")
        print(f"  时段/止损 : {config.trading_sessions or '不限'} / {config.stop_loss:g} 元")
        if config.strategy:
            keys = ", ".join(sorted(config.strategy))
            print(f"  策略覆盖  : {keys}")
        if config.executor == EXECUTOR_EXCHANGE_SPREAD and not config.exchange_spread_vt_symbol:
            print("  [告警] exchange_spread 未配置交易所或组合代码 → 运行时会降级 two_leg")
            problems += 1
        for problem in config.validate():
            print(f"  [问题] {problem}")
            problems += 1

    for problem in unknown_strategy_keys(configs):
        print(f"[告警] {problem}")
        problems += 1

    if not issubclass(CrossPeriodMRStrategy, SpreadStrategyTemplate):
        print("[错误] CrossPeriodMRStrategy 未继承 SpreadStrategyTemplate")
        problems += 1
    print(f"\n策略类 {CrossPeriodMRStrategy.__name__}：{len(CrossPeriodMRStrategy.parameters)} 个参数")
    print(f"算法类 {CrossPeriodAlgo.__name__}：mode 分派 two_leg / exchange_spread")
    return problems


def warn_off_session(configs: dict[str, SpreadConfig], selected: list[str]) -> bool:
    """非交易时段提前提示（否则只会看到"行情登录超时"，容易误判成账号问题）。

    返回 True 表示当前处于**可交易时段**。配置里没写 ``trading_sessions`` 的价差视为不限时段。
    """
    from datetime import datetime as _dt

    from utils.calendar import TradingSessions

    now = _dt.now()
    open_names: list[str] = []
    for name, config in configs.items():
        if selected and name not in selected:
            continue
        if TradingSessions(config.trading_sessions).is_open(now):
            open_names.append(name)

    if open_names:
        return True

    print(
        f"[提示] 当前 {now:%Y-%m-%d %H:%M} 不在任何价差的交易时段内（{', '.join(selected) if selected else '全部'}）。\n"
        f"       非交易时段通常订阅成功也没有 tick；有些柜台在结算/非交易时段还会拒绝登录，\n"
        f"       表现为「行情登录超时」——那不是账号或前置地址错了。"
    )
    return False


def wait_for_contracts(main_engine: MainEngine, symbols: list[str], seconds: float) -> bool:
    """等合约查询完成（策略的 size/pricetick 都来自单腿合约）。"""
    deadline = time.time() + seconds
    pending = list(symbols)
    while time.time() < deadline:
        pending = [symbol for symbol in pending if main_engine.get_contract(symbol) is None]
        if not pending:
            return True
        time.sleep(0.5)
    main_engine.write_log(f"合约查询超时，仍缺失：{pending}", "StrategyRunner")
    return False


def wait_md_login(gateway, seconds: float) -> bool:
    """等行情登录（纯行情模式下没有交易端，只能看 md_api）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if getattr(getattr(gateway, "md_api", None), "login_status", False):
            return True
        time.sleep(0.3)
    return False


def selected_symbols(configs: dict[str, SpreadConfig], selected: list[str]) -> list[str]:
    """收集这次要跑的策略所涉及的腿合约（去重）。"""
    symbols: list[str] = []
    for name, config in configs.items():
        if selected and name not in selected:
            continue
        symbols.extend(config.legs)
    return list(dict.fromkeys(symbols))


def register_md_only_contracts(gateway, configs: dict[str, SpreadConfig],
                               symbols: list[str]) -> list[str]:
    """纯行情模式下自行登记合约（乘数/最小变动取 spreads.json 的值）。

    只连行情时拿不到柜台合约表，而 ``SpreadDataEngine.add_spread`` 与行情回调都依赖
    合约存在，所以必须先登记，否则既不会订阅也收不到 tick。
    """
    lookup: dict[str, SpreadConfig] = {}
    for config in configs.values():
        for vt_symbol in config.legs:
            lookup.setdefault(vt_symbol, config)

    registered: list[str] = []
    for vt_symbol in symbols:
        try:
            symbol, exchange = extract_vt_symbol(vt_symbol)
        except ValueError:
            continue
        if not isinstance(exchange, Exchange):
            continue
        config = lookup.get(vt_symbol)
        gateway.register_contract(
            symbol=symbol,
            exchange=exchange,
            size=(config.size if config and config.size else 1.0),
            pricetick=(config.pricetick if config else 0.0),
        )
        registered.append(vt_symbol)
    return registered


def register_spreads(spread_engine, configs: dict[str, SpreadConfig],
                     selected: list[str]) -> None:
    """按 spreads.json 注册价差（与 GUI 启动共用同一实现）。"""
    created, existing, conflicts = setup_register(
        spread_engine, configs, names=selected or None
    )
    for name in created:
        spread_engine.write_log(f"价差 {name} 已注册")
    for name in existing:
        spread_engine.write_log(f"价差 {name} 已存在，复用")
    if conflicts:
        spread_engine.write_log(f"价差注册异常：{conflicts}（见上方日志）")


def check_exchange_spread_contracts(main_engine: MainEngine,
                                    configs: dict[str, SpreadConfig],
                                    selected: list[str]) -> None:
    """组合合约前置校验（方案 5.3.4）：查不到就提示降级 two_leg。"""
    for name, config in configs.items():
        if selected and name not in selected:
            continue
        if config.executor != EXECUTOR_EXCHANGE_SPREAD:
            continue
        vt_symbol = config.exchange_spread_vt_symbol
        if not vt_symbol:
            main_engine.write_log(
                f"价差 {name}：executor=exchange_spread 但缺少组合合约配置 → 将降级 two_leg",
                "StrategyRunner",
            )
            continue
        contract = main_engine.get_contract(vt_symbol)
        if contract is None:
            main_engine.write_log(
                f"价差 {name}：组合合约 {vt_symbol} 不在合约列表（该月份对可能未挂牌）"
                f"→ 将降级 two_leg",
                "StrategyRunner",
            )
            continue
        main_engine.write_log(
            f"价差 {name}：组合合约 {vt_symbol} 已就绪 "
            f"（size={contract.size:g} pricetick={contract.pricetick:g}；"
            f"size=0 属正常，费用按单腿计算）",
            "StrategyRunner",
        )


def add_strategies(spread_engine, configs: dict[str, SpreadConfig], selected: list[str],
                   overrides: dict) -> list[str]:
    """为每个价差建/更新策略实例，参数取该价差自己的 ``strategy`` 段。"""
    strategy_engine = spread_engine.strategy_engine
    names: list[str] = []
    for name, config in configs.items():
        if selected and name not in selected:
            continue
        strategy_name = f"{STRATEGY_CLASS}_{name}"
        # 策略类默认值 → spreads.json 的 strategy 段 → 命令行 --setting
        setting = strategy_setting_for(config, overrides)
        if strategy_name in strategy_engine.strategies:
            # 复用已加载的实例（来自 spread_trading_strategy.json），参数以配置为准
            strategy_engine.edit_strategy(strategy_name, setting)
        else:
            strategy_engine.add_strategy(STRATEGY_CLASS, strategy_name, name, setting)
        names.append(strategy_name)
    return names


def detect_stopped_strategies(spread_engine, strategy_names: list[str],
                              main_engine) -> list[str]:
    """发现被引擎静默停掉的策略并告警。

    ``SpreadStrategyEngine.call_strategy_func`` 捕获到策略异常时会直接把
    ``strategy.inited`` / ``strategy.trading`` 置为 False（只留一行"触发异常已停止"日志）。
    进程本身还在正常收行情，很容易误以为"策略没信号"，所以这里定期兜一下。
    """
    stopped: list[str] = []
    for name in strategy_names:
        strategy = spread_engine.strategy_engine.strategies.get(name)
        if strategy is None:
            continue
        if not strategy.inited or not strategy.trading:
            stopped.append(name)

    if stopped:
        main_engine.write_log(
            f"警告：以下策略已被引擎停止（通常是策略内部抛异常，请在上方日志里搜"
            f"「触发异常已停止」）→ {stopped}",
            "StrategyRunner",
        )
    return stopped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = PROJECT_ROOT / "config" / "spreads.json"
    configs = load_spread_table(config_path)
    overrides = parse_overrides(args.setting)

    problems = self_check(configs, args.spread)
    if args.list or args.no_connect:
        return 0 if problems == 0 else 1

    if args.md_only and args.live:
        print("[错误] --md-only 只用于观察（不连交易端、无法报单），不能与 --live 同时使用")
        return 1

    setting: dict = dict(overrides)
    setting["live_trading"] = bool(args.live and not args.md_only)
    if args.close_only:
        setting["close_only"] = True

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    if args.md_only:
        from gateway.ctp_md_gateway import CtpMdOnlyGateway

        gateway = main_engine.add_gateway(CtpMdOnlyGateway)
        gateway_name = gateway.gateway_name
    else:
        gateway = main_engine.add_gateway(SpreadCtpGateway)
        gateway_name = gateway.gateway_name
        gateway = gateway

    spread_engine = main_engine.add_app(SpreadTradingApp)

    # vnpy 的 MainEngine 初始化会把工作目录切到 ~/.vntrader，而 SpreadStrategyEngine
    # 只在构造时按 ``Path.cwd()/strategies`` 发现策略类 —— 那一步已经找不到本项目目录了。
    os.chdir(PROJECT_ROOT)

    connect_setting = load_json(CONNECT_FILENAME)
    if not connect_setting:
        main_engine.write_log(
            f"未找到 {CONNECT_FILENAME}（界面「交易 → 连接 CTP」保存后会生成），"
            f"仅做本地自检后退出",
            "StrategyRunner",
        )
        main_engine.close()
        return 1

    symbols = selected_symbols(configs, args.spread)
    warn_off_session(configs, args.spread)

    if args.md_only:
        # 纯行情模式：交易端完全不连，合约用 spreads.json 的乘数/最小变动自行登记。
        # 必须在注册价差之前登记，否则 SpreadDataEngine 不会订阅这些合约。
        registered = register_md_only_contracts(gateway, configs, symbols)
        main_engine.write_log(
            f"纯行情模式：已登记 {len(registered)} 个合约（不连接交易前置，无法报单）",
            "StrategyRunner",
        )

    # 接线：加载策略类 → 换掉吃单算法 → 启动价差引擎（注册 EVENT_TICK）→ 注册价差。
    # 其中「启动价差引擎」是命令行入口最容易漏的一步，漏了价差永远是 0。
    result = setup_spread_trading(
        spread_engine,
        names=args.spread or None,
        log=lambda msg: main_engine.write_log(msg, "StrategyRunner"),
    )
    if STRATEGY_CLASS not in result["strategy_classes"]:
        main_engine.close()
        return 1

    if args.md_only:
        main_engine.connect(connect_setting, gateway_name)
        if not wait_md_login(gateway, args.wait):
            main_engine.write_log(f"行情登录超时（{args.wait:g}s）", "StrategyRunner")
            main_engine.close()
            return 1
    else:
        main_engine.connect(connect_setting, gateway_name)
        if not wait_for_contracts(main_engine, symbols, args.wait):
            main_engine.write_log("警告：部分合约未加载，策略乘数/最小变动可能为 0", "StrategyRunner")
        check_exchange_spread_contracts(main_engine, configs, args.spread)

    strategy_names = add_strategies(spread_engine, configs, args.spread, setting)
    if not strategy_names:
        main_engine.write_log("没有匹配到任何价差，请检查 --spread 参数", "StrategyRunner")
        main_engine.close()
        return 1

    # 边跑边落库：与策略共用同一份行情订阅（开关在 config/runtime.json，可用 --record/--no-record 覆盖）
    recorder = None
    if args.record is not False:
        recorder = attach_tick_recorder(
            main_engine,
            event_engine,
            symbols=symbols,
            log=lambda msg: main_engine.write_log(msg, "StrategyRunner"),
        )
        if args.record is True and recorder is None:
            main_engine.write_log(
                "已要求 --record，但 config/runtime.json 里 record_ticks=false", "StrategyRunner"
            )

    for strategy_name in strategy_names:
        spread_engine.init_strategy(strategy_name)
    time.sleep(1.0)
    for strategy_name in strategy_names:
        spread_engine.start_strategy(strategy_name)

    mode = "真实报单" if setting["live_trading"] else "只计算不发单"
    connection = "仅行情前置" if args.md_only else "交易+行情前置"
    main_engine.write_log(
        f"已启动策略：{', '.join(strategy_names)}（{mode}，{connection}）；Ctrl+C 退出",
        "StrategyRunner",
    )

    try:
        deadline = time.time() + args.minutes * 60 if args.minutes > 0 else None
        next_health_check = time.time() + 10
        while True:
            time.sleep(1)
            if time.time() >= next_health_check:
                next_health_check = time.time() + 10
                detect_stopped_strategies(spread_engine, strategy_names, main_engine)
            if deadline and time.time() >= deadline:
                main_engine.write_log("运行时长已到，准备退出", "StrategyRunner")
                break
    except KeyboardInterrupt:
        main_engine.write_log("收到退出信号，停止策略", "StrategyRunner")
    finally:
        flush_tick_recorder(recorder, lambda msg: main_engine.write_log(msg, "StrategyRunner"))
        for strategy_name in strategy_names:
            spread_engine.stop_strategy(strategy_name)
        time.sleep(1.0)
        main_engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
