"""把 ``spreads.json`` 的配置变成可计算的 ``SpreadData``（回测、录制、预览共用）。

放在 ``utils`` 里而不是 ``backtest.engine``：录制器（``recorder.tick_recorder``）也要
用同一套合成口径，但它不应该为此拉起整个回测引擎（``vnpy_ctastrategy`` 那条重链路）。

合成口径与 ``SpreadData.calculate_price`` 完全一致：做多价差按 near_ask − far_bid、
做空按 near_bid − far_ask。
"""

from __future__ import annotations

from vnpy_spreadtrading.base import LegData, SpreadData

from utils.spread_config import SpreadConfig

# 合成价差入库时用的交易所名：vnpy 自带的价差回测（``load_tick_data(spread.name,
# Exchange.LOCAL, ...)``）正是按这个口径查库，所以存量数据可以直接喂回测。
SPREAD_EXCHANGE_LOCAL = "LOCAL"


def build_spread_data(config: SpreadConfig) -> SpreadData:
    """从 ``spreads.json`` 条目构造 ``SpreadData``（不需要真实合约）。"""
    legs: list[LegData] = []
    multipliers: dict[str, int] = {}
    variable_symbols: dict[str, str] = {}
    variable_directions: dict[str, int] = {}

    for index, vt_symbol in enumerate(config.legs):
        leg = LegData(vt_symbol)
        leg.pricetick = config.pricetick
        leg.size = config.size
        leg.min_volume = config.min_volume
        legs.append(leg)

        is_near = index == 0
        multipliers[vt_symbol] = 1 if is_near else -1
        variable_symbols["near" if is_near else "far"] = vt_symbol
        variable_directions["near" if is_near else "far"] = 1 if is_near else -1

    return SpreadData(
        name=config.name,
        legs=legs,
        variable_symbols=variable_symbols,
        variable_directions=variable_directions,
        price_formula=config.price_formula,
        trading_multipliers=multipliers,
        active_symbol=config.active_symbol or config.near_symbol,
        min_volume=config.min_volume,
    )


def build_spreads(configs) -> dict[str, SpreadData]:
    """批量构造：``{价差名: SpreadData}``，跳过配置不完整的条目。"""
    spreads: dict[str, SpreadData] = {}
    for config in configs:
        if len(config.legs) != 2:
            continue
        try:
            spreads[config.name] = build_spread_data(config)
        except Exception:      # noqa: BLE001 - 单条配置坏了不该影响其它价差
            continue
    return spreads
