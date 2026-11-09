"""价差回测：把两腿 tick 合成为价差 tick，并用 ``FeeCalculator`` 精确计费。

为什么要自己合成：``vnpy_spreadtrading`` 的 ``BacktestingEngine.load_data`` 走的是
``load_tick_data(spread.name, Exchange.LOCAL, ...)``，也就是**要求库里已经存在以价差
名字（如 ``A_2611_2701``，交易所 LOCAL）入库的合成 tick**，而不是两腿各自的 tick。
本模块直接读两腿 tick 在内存里合成，省掉"先落库再回测"这一步。

合成口径与 ``SpreadData.calculate_price`` 完全一致（做多价差按 ask = near_ask − far_bid
成交、做空按 bid = near_bid − far_ask），回放时把腿 tick 也塞回 ``spread.legs``，
让策略的过滤器（宽度/同步/深度/成交量比）在回测中**真正生效**，而不是恒为 True。

手续费：引擎自带的 ``turnover × rate`` 无法表达"元/手"计费与平今/平昨，因此覆盖
``calculate_result``，改按单腿 ``FeeCalculator`` 逐笔计费（与实盘同口径）。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Callable

from pandas import DataFrame

from vnpy.trader.constant import Direction, Exchange
from vnpy.trader.database import get_database
from vnpy.trader.object import TickData
from vnpy.trader.utility import extract_vt_symbol
from vnpy_spreadtrading.backtesting import BacktestingEngine, BacktestingMode
from vnpy_spreadtrading.base import SpreadData

from fees.calculator import KIND_CLOSE, KIND_OPEN, KIND_TODAY, FeeCalculator
from utils.spread_synth import build_spread_data  # noqa: F401 - 供 tickbrowser 等复用

GATEWAY_NAME = "SPREAD_SYNTH"


def load_leg_ticks(spread: SpreadData, start: datetime, end: datetime,
                   output: Callable = print,
                   database=None) -> dict[str, list[TickData]]:
    database = database or get_database()
    result: dict[str, list[TickData]] = {}
    for vt_symbol in spread.legs:
        symbol, exchange = extract_vt_symbol(vt_symbol)
        ticks = database.load_tick_data(symbol, exchange, start, end)
        result[vt_symbol] = ticks
        output(f"  载入 {vt_symbol}：{len(ticks)} 条 tick")
    return result


def synthesize_spread_ticks(
    spread: SpreadData,
    start: datetime,
    end: datetime,
    output: Callable = print,
    database=None,
) -> tuple[list[TickData], list[dict[str, TickData]]]:
    """合成价差 tick；同时返回每帧的腿 tick 快照（供回放时还原腿级盘口）。"""
    leg_ticks = load_leg_ticks(spread, start, end, output, database)

    missing = [symbol for symbol, ticks in leg_ticks.items() if not ticks]
    if missing:
        output(f"[警告] 以下腿没有 tick 数据，回测无法进行：{missing}")

    events: list[tuple[datetime, str, TickData]] = []
    for vt_symbol, ticks in leg_ticks.items():
        for tick in ticks:
            events.append((tick.datetime, vt_symbol, tick))
    events.sort(key=lambda item: (item[0], item[1]))

    spread_ticks: list[TickData] = []
    frames: list[dict[str, TickData]] = []
    current: dict[str, TickData] = {}

    for moment, vt_symbol, tick in events:
        current[vt_symbol] = tick
        spread.legs[vt_symbol].update_tick(tick)

        if not spread.calculate_price():
            continue

        spread_tick = TickData(
            symbol=spread.name,
            exchange=Exchange.LOCAL,
            datetime=moment,
            name=spread.name,
            last_price=(spread.bid_price + spread.ask_price) / 2,
            bid_price_1=spread.bid_price,
            ask_price_1=spread.ask_price,
            bid_volume_1=spread.bid_volume,
            ask_volume_1=spread.ask_volume,
            gateway_name=GATEWAY_NAME,
        )
        spread_ticks.append(spread_tick)
        frames.append(dict(current))

    output(f"合成价差 tick：{len(spread_ticks)} 条（来自 {len(events)} 条腿 tick）")
    return spread_ticks, frames


class CrossPeriodBacktestingEngine(BacktestingEngine):
    """在标准回测引擎上加两层：两腿合成、按单腿精确计费。"""

    def __init__(self) -> None:
        super().__init__()
        self.frame_legs: list[dict[str, TickData]] = []
        self.trade_leg_prices: dict[str, dict[str, float]] = {}
        self.fee_calc: FeeCalculator = FeeCalculator()
        self.fee_spec = self.fee_calc.default_spec()
        self.near_symbol: str = ""
        self.far_symbol: str = ""
        self._frame_index: int = 0

    # ------------------------------------------------------------ 数据
    def load_data(self) -> None:
        self.output("开始加载历史数据（两腿 tick → 合成价差）")
        if not self.end:
            self.end = datetime.now()
        if self.start >= self.end:
            self.output("起始日期必须小于结束日期")
            return

        self.mode = BacktestingMode.TICK
        ticks, frames = synthesize_spread_ticks(self.spread, self.start, self.end, self.output)
        self.history_data = ticks
        self.frame_legs = frames

        self.near_symbol = self.spread.variable_symbols.get("near", "")
        self.far_symbol = self.spread.variable_symbols.get("far", "")
        self.fee_spec = self.fee_calc.spec(self.near_symbol, self.far_symbol)

        self.output(f"历史数据加载完成，数据量：{len(self.history_data)}")

    def run_backtesting(self) -> None:
        import traceback

        self.strategy.on_init()
        self.strategy.inited = True
        self.output("策略初始化完成")

        self.strategy.on_start()
        self.strategy.trading = True
        self.output("开始回放历史数据")

        for index, data in enumerate(self.history_data):
            self._frame_index = index
            try:
                self.new_tick(data)
            except Exception:
                self.output("触发异常，回测终止")
                self.output(traceback.format_exc())
                return

        self.output("历史数据回放结束")

    def new_tick(self, tick: TickData) -> None:
        frame = self.frame_legs[self._frame_index] if self._frame_index < len(self.frame_legs) else {}
        for vt_symbol, leg_tick in frame.items():
            leg = self.spread.legs.get(vt_symbol)
            if leg is not None:
                leg.update_tick(leg_tick)

        known_trades = set(self.trades)
        super().new_tick(tick)

        for tradeid in set(self.trades) - known_trades:
            self._record_leg_prices(self.trades[tradeid], frame)

    def _record_leg_prices(self, trade, frame: dict[str, TickData]) -> None:
        near_tick = frame.get(self.near_symbol)
        far_tick = frame.get(self.far_symbol)
        if near_tick is None or far_tick is None:
            return
        if trade.direction == Direction.LONG:      # 买价差：买 near(ask) + 卖 far(bid)
            prices = {self.near_symbol: near_tick.ask_price_1,
                      self.far_symbol: far_tick.bid_price_1}
        else:                                      # 卖价差：卖 near(bid) + 买 far(ask)
            prices = {self.near_symbol: near_tick.bid_price_1,
                      self.far_symbol: far_tick.ask_price_1}
        self.trade_leg_prices[trade.vt_tradeid] = prices

    # ------------------------------------------------------------ 结算
    def calculate_result(self) -> DataFrame | None:
        self.output("开始计算逐日盯市盈亏（手续费按单腿 FeeCalculator 精确计）")

        if not self.trades:
            self.output("回测成交记录为空")

        for trade in self.trades.values():
            day = trade.datetime.date()
            daily_result = self.daily_results[day]
            daily_result.add_trade(trade, self.trade_values[trade.vt_tradeid])

        fees = self._compute_commissions()

        pre_close = 0.0
        start_pos = 0.0
        for day, daily_result in self.daily_results.items():
            daily_result.calculate_pnl(pre_close, start_pos, self.size, 0.0, self.slippage)
            daily_result.commission = fees.get(day, 0.0)
            daily_result.net_pnl = daily_result.total_pnl - daily_result.commission - daily_result.slippage
            pre_close = daily_result.close_price
            start_pos = daily_result.end_pos

        results: defaultdict = defaultdict(list)
        for daily_result in self.daily_results.values():
            for key, value in daily_result.__dict__.items():
                results[key].append(value)

        if results:
            self.daily_df = DataFrame.from_dict(results).set_index("date")

        self.output("逐日盯市盈亏计算完成")
        return self.daily_df

    def _compute_commissions(self) -> dict:
        """按成交顺序逐笔计费；平仓区分平今（当日开仓）与平昨。"""
        fees: defaultdict = defaultdict(float)
        signed_position = 0.0
        open_day = None

        for trade in sorted(self.trades.values(), key=lambda item: item.datetime):
            day = trade.datetime.date()
            delta = trade.volume if trade.direction == Direction.LONG else -trade.volume

            is_close = (signed_position > 0 and delta < 0) or (signed_position < 0 and delta > 0)
            if is_close:
                kind = KIND_TODAY if open_day == day else KIND_CLOSE
            else:
                kind = KIND_OPEN

            fees[day] += self._trade_fee(trade, kind)

            signed_position += delta
            if abs(signed_position) < 1e-12:
                signed_position = 0.0
                open_day = None
            elif open_day is None:
                open_day = day

        return fees

    def _trade_fee(self, trade, kind: str) -> float:
        prices = self.trade_leg_prices.get(trade.vt_tradeid, {})
        near_price = prices.get(self.near_symbol) or abs(trade.price)
        far_price = prices.get(self.far_symbol) or abs(trade.price)
        fee = self.fee_spec.edge_fee(kind, near_price, self.size, trade.volume)
        fee += self.fee_spec.edge_fee(kind, far_price, self.size, trade.volume)
        return fee
