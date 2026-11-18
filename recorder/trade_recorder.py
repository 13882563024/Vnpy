"""委托与成交记录按交易日追加写 CSV。

设计要点（与 :mod:`recorder.tick_recorder` 保持同一套约定）：

- **崩溃/重启不丢**：每批事件立即追加写，不依赖退出时收尾。
- **按交易日分目录**：``<root>/<交易日>/orders.csv`` 与 ``trades.csv``，
  夜盘（21:00 之后）按 :func:`utils.calendar.trading_day_of` 归到次日。
- **去重**：CTP 在重连/重启后会重推历史委托，按
  ``(vt_orderid, status, traded, datetime)`` 去重；成交按 ``tradeid`` 去重。
- **委托记事件流、成交记明细**：同一笔委托会有"提交中→未成交→全部成交"多行，
  这正是复盘需要的；成交本身不可变，一笔一行。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vnpy.trader.constant import Direction
from vnpy.trader.object import OrderData, TradeData
from vnpy.trader.utility import get_file_path

from utils.calendar import trading_day_of

#: 默认根目录：与 database.db 同处，属运行期数据，不进仓库
DEFAULT_RECORD_ROOT = Path(get_file_path("records"))

ORDER_HEADER = (
    "datetime", "trading_day", "vt_orderid", "vt_symbol", "gateway",
    "direction", "offset", "type", "price", "volume", "traded", "status",
    "reference",
)
TRADE_HEADER = (
    "datetime", "trading_day", "tradeid", "vt_orderid", "vt_symbol", "gateway",
    "direction", "offset", "price", "volume",
)
#: 价差级成交：把同一组合单的两条腿合成一行，直接给出开仓/平仓的价差
SPREAD_TRADE_HEADER = (
    "datetime", "trading_day", "spread", "vt_orderid", "direction", "offset",
    "near_symbol", "far_symbol", "near_price", "far_price", "spread_price",
    "volume", "gateway",
)


@dataclass
class RecordStats:
    """录制计数（日志里报告进度用）。"""

    orders_written: int = 0
    trades_written: int = 0
    spread_trades_written: int = 0
    orders_skipped: int = 0
    trades_skipped: int = 0

    def describe(self) -> str:
        return (f"委托 {self.orders_written} 行（去重跳过 {self.orders_skipped}）"
                f"、成交 {self.trades_written} 行（去重跳过 {self.trades_skipped}）"
                f"、价差成交 {self.spread_trades_written} 行")


def _format_moment(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _enum_text(value) -> str:
    return str(getattr(value, "value", value) or "")


@dataclass
class TradeRecorder:
    """监听 EVENT_ORDER / EVENT_TRADE，按交易日追加写 CSV。"""

    root: Path = DEFAULT_RECORD_ROOT
    record_orders: bool = True
    record_trades: bool = True
    batch_size: int = 50
    output: object = None                      # 日志函数，缺省不输出
    stats: RecordStats = field(default_factory=RecordStats)
    #: 价差定义：{价差名: (near_vt_symbol, far_vt_symbol)}；给了才会合成价差成交
    spreads: dict[str, tuple[str, str]] = field(default_factory=dict)

    _order_rows: list[tuple] = field(default_factory=list, repr=False)
    _trade_rows: list[tuple] = field(default_factory=list, repr=False)
    _spread_rows: list[tuple] = field(default_factory=list, repr=False)
    _seen_orders: set[tuple] = field(default_factory=set, repr=False)
    _seen_trades: set[str] = field(default_factory=set, repr=False)
    _path_day: str = field(default="", repr=False)
    #: 缓存：vt_orderid -> {vt_symbol: 腿成交}，两条腿齐了就合成一行价差成交
    _pending_legs: dict[str, dict[str, dict]] = field(default_factory=dict, repr=False)

    def _leg_index(self) -> dict[str, tuple[str, str, str]]:
        """腿合约 → (价差名, near, far)，用于把单腿成交反查到所属价差。"""
        index: dict[str, tuple[str, str, str]] = {}
        for name, legs in (self.spreads or {}).items():
            try:
                near, far = legs
            except (TypeError, ValueError):
                continue
            index[near] = (name, near, far)
            index[far] = (name, near, far)
        return index

    # ------------------------------------------------------------ 事件入口
    def on_order(self, order: OrderData) -> None:
        if not self.record_orders or order is None:
            return
        moment = getattr(order, "datetime", None) or datetime.now()
        key = (order.vt_orderid, _enum_text(order.status),
               float(order.traded or 0), _format_moment(moment))
        if key in self._seen_orders:
            self.stats.orders_skipped += 1
            return
        self._seen_orders.add(key)
        day = trading_day_of(moment).isoformat()
        self._maybe_rotate(day)
        self._order_rows.append((
            _format_moment(moment), day, order.vt_orderid, order.vt_symbol,
            getattr(order, "gateway_name", ""),
            _enum_text(order.direction), _enum_text(order.offset),
            _enum_text(order.type), f"{float(order.price or 0):g}",
            f"{float(order.volume or 0):g}", f"{float(order.traded or 0):g}",
            _enum_text(order.status), getattr(order, "reference", "") or "",
        ))
        if len(self._order_rows) >= self.batch_size:
            self.flush_orders()

    def on_trade(self, trade: TradeData) -> None:
        if not self.record_trades or trade is None:
            return
        tradeid = getattr(trade, "tradeid", "") or ""
        key = tradeid or f"{trade.vt_orderid}|{trade.vt_symbol}|{trade.price}|{trade.volume}"
        if key in self._seen_trades:
            self.stats.trades_skipped += 1
            return
        self._seen_trades.add(key)
        moment = getattr(trade, "datetime", None) or datetime.now()
        day = trading_day_of(moment).isoformat()
        self._maybe_rotate(day)
        self._trade_rows.append((
            _format_moment(moment), day, tradeid, trade.vt_orderid, trade.vt_symbol,
            getattr(trade, "gateway_name", ""),
            _enum_text(trade.direction), _enum_text(trade.offset),
            f"{float(trade.price or 0):g}", f"{float(trade.volume or 0):g}",
        ))
        self._maybe_emit_spread(trade, moment, day)
        if len(self._trade_rows) >= self.batch_size:
            self.flush_trades()

    def _maybe_emit_spread(self, trade: TradeData, moment: datetime, day: str) -> None:
        """两条腿都到齐时合成一行价差成交（含开仓价差）。"""
        index = self._leg_index()
        info = index.get(trade.vt_symbol)
        if info is None:
            return
        name, near, far = info
        # 组合单的两条腿共享同一个 vt_orderid，等齐了再合成
        legs = self._pending_legs.setdefault(trade.vt_orderid, {})
        legs[trade.vt_symbol] = {
            "price": float(trade.price or 0),
            "volume": float(trade.volume or 0),
            "direction": trade.direction,
            "offset": _enum_text(trade.offset),
            "datetime": moment,
            "gateway": getattr(trade, "gateway_name", ""),
        }
        if near not in legs or far not in legs:
            if len(self._pending_legs) > 500:      # 防止配不上对的腿无限堆积
                self._pending_legs.clear()
            return
        near_leg = legs.pop(near)
        far_leg = legs.pop(far)
        self._pending_legs.pop(trade.vt_orderid, None)

        volume = min(near_leg["volume"], far_leg["volume"])
        if volume <= 0:
            return
        near_price, far_price = near_leg["price"], far_leg["price"]
        near_buy = near_leg["direction"] == Direction.LONG
        if near_leg["offset"] == "开":
            direction = "开多价差" if near_buy else "开空价差"
        else:
            direction = "平空价差" if near_buy else "平多价差"
        latest = max(near_leg["datetime"], far_leg["datetime"])
        self._spread_rows.append((
            _format_moment(latest), trading_day_of(latest).isoformat(), name,
            trade.vt_orderid, direction, near_leg["offset"],
            near, far, f"{near_price:g}", f"{far_price:g}",
            f"{near_price - far_price:g}", f"{volume:g}",
            near_leg["gateway"] or far_leg["gateway"],
        ))
        if len(self._spread_rows) >= self.batch_size:
            self.flush_spread_trades()

    # ------------------------------------------------------------ 落盘
    def _maybe_rotate(self, day: str) -> None:
        """跨交易日切换目录前先把缓冲写干净。"""
        if self._path_day and self._path_day != day:
            self.flush()
        self._path_day = day

    def _day_dir(self, day: str) -> Path:
        path = Path(self.root) / day
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _append(self, path: Path, header: tuple[str, ...], rows: list[tuple]) -> int:
        if not rows:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(header)
            writer.writerows(rows)
        return len(rows)

    def flush_orders(self) -> int:
        rows, self._order_rows = self._order_rows, []
        if not rows:
            return 0
        day = rows[0][1]
        written = self._append(self._day_dir(day) / "orders.csv", ORDER_HEADER, rows)
        self.stats.orders_written += written
        return written

    def flush_trades(self) -> int:
        rows, self._trade_rows = self._trade_rows, []
        if not rows:
            return 0
        day = rows[0][1]
        written = self._append(self._day_dir(day) / "trades.csv", TRADE_HEADER, rows)
        self.stats.trades_written += written
        return written

    def flush(self) -> int:
        """把三个缓冲区都写掉；退出前必须调一次。"""
        return (self.flush_orders() + self.flush_trades()
                + self.flush_spread_trades())

    def flush_spread_trades(self) -> int:
        rows, self._spread_rows = self._spread_rows, []
        if not rows:
            return 0
        day = rows[0][1]
        written = self._append(self._day_dir(day) / "spread_trades.csv",
                               SPREAD_TRADE_HEADER, rows)
        self.stats.spread_trades_written += written
        return written

    # ------------------------------------------------------------ 查询
    def ensure_day(self, moment: datetime | None = None) -> Path:
        """预创建交易日目录与三个 CSV（带表头），让文件位置立刻可见。

        不做这一步的话，没有委托/成交的日子整个目录都不会出现，看起来像"没输出"。
        """
        day = trading_day_of(moment or datetime.now()).isoformat()
        path = self._day_dir(day)
        for filename, header in (
            ("orders.csv", ORDER_HEADER),
            ("trades.csv", TRADE_HEADER),
            ("spread_trades.csv", SPREAD_TRADE_HEADER),
        ):
            target = path / filename
            if target.exists() and target.stat().st_size > 0:
                continue
            with target.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(header)
        return path

    def recorded_days(self) -> list[str]:
        """已落盘的交易日（倒序）。"""
        root = Path(self.root)
        if not root.exists():
            return []
        days = [item.name for item in root.iterdir()
                if item.is_dir() and (item / "orders.csv").exists()]
        return sorted(days, reverse=True)
