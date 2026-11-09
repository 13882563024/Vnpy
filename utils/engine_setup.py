"""把跨期策略接到 vnpy ``SpreadEngine`` 上的三步接线（GUI 与命令行共用）。

这三步都是**静默失败**的坑，缺任何一步都不会报错，只会行为不对：

1. **加载策略类**：``SpreadStrategyEngine`` 只在构造时按 ``Path.cwd()/strategies`` 扫一次，
   而 ``MainEngine.__init__`` 会把 cwd 切到 ``~/.vntrader`` → 之后再也不会加载本项目目录。
   不补这一步，价差面板的策略类下拉里根本没有 ``CrossPeriodMRStrategy``。
2. **替换算法类**：``SpreadAlgoEngine.algo_class`` 是类属性，默认 ``SpreadTakerAlgo``，
   语义是**立即吃单成交**。不替换就启动策略 = 直接市价成交（真实账号上是事故）。
3. **注册价差**：不注册的话价差面板是空的，用户得手工填 legs / active_symbol / formula。
"""

from __future__ import annotations

from pathlib import Path

from utils.spread_config import SpreadConfig, load_spread_configs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_DIR = PROJECT_ROOT / "strategies"
STRATEGY_MODULE = "strategies"


def ensure_strategy_classes(spread_engine, strategy_dir: Path | None = None) -> list[str]:
    """显式再扫一遍策略目录，返回引擎当前认识的策略类名。"""
    directory = Path(strategy_dir) if strategy_dir else STRATEGY_DIR
    spread_engine.strategy_engine.load_strategy_class_from_folder(
        directory, STRATEGY_MODULE
    )
    return spread_engine.get_all_strategy_class_names()


def install_algo_class(spread_engine) -> None:
    """把 algo_class 换成 ``CrossPeriodAlgo``，替换不成功直接抛错（避免误吃单）。"""
    from execution.cross_period_algo import CrossPeriodAlgo

    engine = spread_engine.algo_engine
    engine.algo_class = CrossPeriodAlgo

    actual = getattr(engine, "algo_class", None)
    if actual is not CrossPeriodAlgo:
        raise RuntimeError(
            f"algo_class 替换失败（当前 {actual!r}）—— 继续运行会退回 SpreadTakerAlgo 立即吃单"
        )
    # 新开的算法实例必须能按 mode 分派（快速自检，不真正发单）
    if not hasattr(CrossPeriodAlgo, "send_leg_order"):
        raise RuntimeError("CrossPeriodAlgo 缺少 send_leg_order，算法类版本不匹配")


def register_spreads(
    spread_engine,
    configs: dict[str, SpreadConfig] | None = None,
    *,
    names: list[str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """按 ``config/spreads.json`` 注册价差；``names`` 非空时只注册这些。

    返回 ``(新建, 已存在, 有问题)`` 三个列表，便于调用方打印。
    """
    table = configs if configs is not None else load_spread_configs()
    wanted = set(names or [])
    created: list[str] = []
    existing: list[str] = []
    conflicts: list[str] = []

    for name, config in table.items():
        if wanted and name not in wanted:
            continue

        current = spread_engine.get_spread(name)
        if current is not None:
            legs = list(current.variable_symbols.values())
            if legs != config.legs:
                conflicts.append(name)
                spread_engine.write_log(
                    f"价差 {name} 已存在但 legs 不一致（{legs} ≠ {config.legs}）："
                    f"请删除 ~/.vntrader/spread_trading_setting.json 中该条目后重启"
                )
            else:
                existing.append(name)
            continue

        problems = config.validate()
        # GUI 新建组合在柜台合约尚未查询完成时仍需订阅两腿并合成行情；
        # size=0 只阻断实盘，不阻断注册。其余字段仍须严格校验。
        if config.contract_status == "pending_verification":
            problems = [item for item in problems if not item.startswith("size 必须 > 0")]
        if problems:
            conflicts.append(name)
            spread_engine.write_log(f"价差 {name} 配置有问题，跳过注册：{problems}")
            continue

        spread_engine.add_spread(
            name=name,
            leg_settings=config.leg_settings(),
            price_formula=config.price_formula,
            active_symbol=config.active_symbol,
            min_volume=config.min_volume,
        )
        created.append(name)

    spread_engine.write_log(
        f"价差注册完成：新建 {len(created)} 个，已存在 {len(existing)} 个"
        + (f"，有问题 {len(conflicts)} 个：{conflicts}" if conflicts else "")
    )
    return created, existing, conflicts


def ensure_engine_started(spread_engine) -> None:
    """启动价差引擎（**命令行入口必须自己调**）。

    ``MainEngine.add_engine`` 只创建引擎、**不调用 ``start()``**；而 ``SpreadDataEngine``
    是在 ``start()`` 里才注册 ``EVENT_TICK`` 的。GUI 里这一步由价差面板
    （``vnpy_spreadtrading/ui/widget.py``）代劳，命令行入口若漏掉：行情能收到、tick 也
    能落库，但 ``SpreadData`` 的 bid/ask 永远是 0 —— 策略一条 ``on_spread_data`` 都收不到，
    表面看"策略在跑却毫无反应"。``SpreadEngine.start()`` 自身幂等，重复调用无副作用。
    """
    spread_engine.start()


def strategy_setting_for(
    config: SpreadConfig | None,
    overrides: dict | None = None,
) -> dict:
    """算出某个价差最终生效的策略参数。

    优先级（后覆盖前）：**策略类默认值 → spreads.json 的 ``strategy`` 段 → 命令行
    ``--setting``**。这样"每个合约对一套独立参数"写在配置文件里，临时调试再用
    ``--setting`` 顶掉。``strategy`` 段里拼错的键会被忽略（``unknown_strategy_keys`` 负责告警）。
    """
    from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy

    allowed = set(CrossPeriodMRStrategy.parameters)

    merged: dict = {}
    if config is not None and config.strategy:
        for key, value in config.strategy.items():
            if key in allowed:
                merged[key] = value

    for key, value in (overrides or {}).items():
        merged[key] = value
    return merged


def unknown_strategy_keys(configs: dict[str, SpreadConfig]) -> list[str]:
    """检查 spreads.json 里 ``strategy`` 段是否有拼错的参数名（否则会静默失效）。"""
    from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy

    allowed = set(CrossPeriodMRStrategy.parameters)
    problems: list[str] = []
    for name, config in configs.items():
        for key in (config.strategy or {}):
            if key not in allowed:
                problems.append(f"{name}: strategy.{key} 不是策略参数（会被忽略）")
    return problems


def create_spread_strategies(spread_engine, *, names: list[str] | None = None,
                             overrides: dict | None = None, log=print) -> list[str]:
    """按 spreads.json 为每个价差创建（或更新）策略实例，参数用各自的 ``strategy`` 段。"""
    from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy

    configs = load_spread_configs()
    strategy_engine = spread_engine.strategy_engine
    class_name = CrossPeriodMRStrategy.__name__
    wanted = set(names or [])
    prepared: list[str] = []

    for name, config in configs.items():
        if wanted and name not in wanted:
            continue
        if spread_engine.get_spread(name) is None:
            continue

        strategy_name = f"{class_name}_{name}"
        setting = strategy_setting_for(config, overrides)

        if strategy_name in strategy_engine.strategies:
            strategy_engine.edit_strategy(strategy_name, setting)
        else:
            strategy_engine.add_strategy(class_name, strategy_name, name, setting)
        prepared.append(strategy_name)

    if prepared:
        log(f"已按 spreads.json 准备策略实例：{prepared}")
    return prepared


def setup_spread_trading(spread_engine, *, names: list[str] | None = None,
                         overrides: dict | None = None, create_strategies: bool = False,
                         log=print) -> dict:
    """一次性完成接线，返回自检结果（GUI 启动与命令行入口都调它）。

    顺序不能颠倒：策略类要先加载（``strategy_engine.start()`` 会按类名恢复策略实例），
    再启动引擎（注册 EVENT_TICK + 加载已保存的价差），最后补齐 spreads.json 里的价差。

    ``create_strategies=True``：按 spreads.json 为每个价差建好策略实例（参数取各自的
    ``strategy`` 段），GUI 里打开价差面板就能直接初始化/启动，不用手工填 30 多个参数。
    """
    from execution.cross_period_algo import CrossPeriodAlgo

    classes = ensure_strategy_classes(spread_engine)
    if "CrossPeriodMRStrategy" not in classes:
        raise RuntimeError(f"策略类加载失败，当前可用：{classes}")

    install_algo_class(spread_engine)
    ensure_engine_started(spread_engine)
    created, existing, conflicts = register_spreads(spread_engine, names=names)

    strategies: list[str] = []
    if create_strategies:
        strategies = create_spread_strategies(
            spread_engine, names=names, overrides=overrides, log=log
        )

    result = {
        "strategy_classes": classes,
        "algo_class": CrossPeriodAlgo.__name__,
        "created": created,
        "existing": existing,
        "conflicts": conflicts,
        "strategies": strategies,
    }

    msg = (
        f"跨期策略接线完成：策略类 {len(classes)} 个（含 CrossPeriodMRStrategy），"
        f"算法类 {result['algo_class']}（maker 挂单，不会立即吃单），"
        f"价差 新建 {len(created)} / 已存在 {len(existing)}，"
        f"价差引擎已启动（实时合成价差）"
        + (f"，策略实例 {len(strategies)} 个" if strategies else "")
    )
    spread_engine.write_log(msg)
    log(msg)
    return result


def attach_tick_recorder(
    main_engine,
    event_engine,
    runtime=None,
    *,
    symbols: list[str] | None = None,
    log=None,
):
    """把 tick 落库挂到运行中的引擎上（策略运行的同时采集，供回测复用）。

    录制与策略共用同一份行情订阅：不需要另开进程、也不会重复连接柜台。
    ``runtime`` 为空时读 ``config/runtime.json``；``record_ticks=False`` 时返回 None。
    """
    from vnpy.trader.event import EVENT_TICK

    from recorder.tick_recorder import TickRecorder
    from utils.runtime_config import load_runtime_config

    config = runtime if runtime is not None else load_runtime_config()
    write = log or main_engine.write_log

    if not config.record_ticks:
        write("tick 落库已关闭（config/runtime.json 的 record_ticks=false）")
        return None

    watch = list(symbols) if symbols is not None else list(config.record_symbols)
    spreads: dict = {}
    if config.record_spreads:
        try:
            from utils.spread_config import load_spread_configs
            from utils.spread_synth import build_spreads

            spreads = build_spreads(load_spread_configs().values())
        except Exception as exc:  # noqa: BLE001 - 合不出价差不该拖累腿的采集
            write(f"价差合成未启用（读取 spreads.json 失败：{exc}）")
            spreads = {}

    recorder = TickRecorder(
        symbols=watch,
        batch_size=config.record_batch_size,
        flush_interval=config.record_flush_interval,
        output=write,
        spreads=spreads,
        record_spreads=config.record_spreads,
        snap_ms=config.record_snap_ms,
    )

    def on_tick(event) -> None:
        recorder.on_tick(event.data)

    event_engine.register(EVENT_TICK, on_tick)
    write(f"tick 落库已启动：{config.describe()}")
    if spreads:
        write(f"价差落库：{len(spreads)} 个（{', '.join(list(spreads)[:5])}"
              + ("…" if len(spreads) > 5 else "") + "）")
    return recorder


def flush_tick_recorder(recorder, log=None) -> int:
    """落库收尾（GUI 关窗、命令行 Ctrl+C 时调）。返回补写条数。"""
    if recorder is None:
        return 0
    try:
        written = recorder.flush()
    except Exception as exc:  # noqa: BLE001 - 收尾失败不该拦住退出
        if log:
            log(f"tick 落库收尾失败：{exc!r}")
        return 0
    if log:
        log(f"tick 落库收尾：补写 {written} 条；{recorder.stats.written} 条已入库")
    return written
