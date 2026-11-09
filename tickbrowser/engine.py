"""tick 浏览面板的引擎：给 GUI 提供只读查询与价差预览。"""

from __future__ import annotations

from datetime import datetime, timedelta

from vnpy.event import EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine

from recorder.tick_store import DaySummary, SymbolSummary, TickStore

APP_NAME = "TickBrowser"


class TickBrowserEngine(BaseEngine):
    """数据查询后端（不订阅行情、不发单，纯只读）。"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, APP_NAME)
        self.store: TickStore = TickStore()

    # ------------------------------------------------------------ tick 数据
    def describe_store(self) -> str:
        if not self.store.exists:
            return f"数据库不存在：{self.store.db_path}"
        return f"{self.store.describe()}，合计 {self.store.total_ticks()} 条 tick"

    def list_symbols(self) -> list[SymbolSummary]:
        return self.store.list_symbols()

    def list_days(self) -> list[DaySummary]:
        return self.store.list_days()

    def load_recent(self, symbol: str, exchange: str, limit: int = 200) -> list[tuple]:
        return self.store.load_recent(symbol, exchange, limit)

    # ------------------------------------------------------------ 价差预览
    def spread_names(self) -> list[str]:
        from utils.spread_config import load_spread_configs

        return list(load_spread_configs().keys())

    def preview_spread(self, name: str, limit: int = 200) -> tuple[list[tuple], str]:
        """把两腿 tick 合成价差，返回 (表格行, 摘要文本)。

        复用回测引擎的合成逻辑（``backtest/engine.py``），保证"界面看到的价差"与
        "策略/回测吃到的价差"是同一套口径。
        """
        from backtest.engine import build_spread_data, synthesize_spread_ticks
        from utils.spread_config import load_spread_configs

        config = load_spread_configs().get(name)
        if config is None:
            return [], f"config/spreads.json 里没有价差 {name}"

        spread = build_spread_data(config)
        start = datetime(2000, 1, 1)
        end = datetime.now() + timedelta(days=1)
        ticks, _frames = synthesize_spread_ticks(spread, start, end, output=lambda *_: None)

        if not ticks:
            return [], (
                f"价差 {name} 还没有可合成的数据 —— 先采集 "
                f"{config.near_symbol} / {config.far_symbol} 的 tick"
            )

        rows: list[tuple] = []
        for tick in ticks[-max(1, limit):]:
            mid = (tick.bid_price_1 + tick.ask_price_1) / 2
            row = (
                str(tick.datetime),
                float(tick.bid_price_1),
                float(tick.ask_price_1),
                float(mid),
                float(min(tick.bid_volume_1, tick.ask_volume_1)),
            )
            rows.append(row)

        summary = (
            f"价差 {name} = {config.near_symbol} − {config.far_symbol}"
            f"（乘数 {config.size:g}、最小变动 {config.pricetick:g}）；"
            f"共合成 {len(ticks)} 条，{ticks[0].datetime} ~ {ticks[-1].datetime}，"
            f"下表为最后 {len(rows)} 条"
        )
        return rows, summary
