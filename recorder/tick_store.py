"""已采集 tick 的只读查询层（命令行工具与 GUI 面板共用）。

直接读 vnpy 的 SQLite 库（默认 ``~/.vntrader/database.db``），**不经过 vnpy 引擎**：
查询是只读的，没必要为一个"看看数据"的界面去初始化 MainEngine。

表结构（由 ``vnpy_sqlite`` 建）：

- ``dbtickdata``：tick 明细，主键 ``(symbol, exchange, datetime)``
- ``dbtickoverview``：每合约汇总。**注意 ``count`` 是累计写入条数**（``stream=True`` 下
  同键覆盖也算），所以这里一律以 ``dbtickdata`` 的 ``count(*)`` 为准。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path(os.path.expanduser("~")) / ".vntrader" / "database.db"

TICK_COLUMNS = (
    "datetime", "last_price", "bid_price_1", "ask_price_1",
    "bid_volume_1", "ask_volume_1", "volume",
)


@dataclass(frozen=True)
class SymbolSummary:
    """一个合约在库里的数据概况。"""

    symbol: str
    exchange: str
    count: int
    start: str
    end: str

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange}"

    def describe(self) -> str:
        return f"{self.vt_symbol}  {self.count} 条  {self.start} ~ {self.end}"


@dataclass(frozen=True)
class DaySummary:
    """按天汇总的一行。"""

    day: str
    symbol: str
    exchange: str
    count: int


class TickStore:
    """对已采集 tick 的只读查询。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path: Path = Path(db_path) if db_path else DEFAULT_DB_PATH

    # ------------------------------------------------------------ 元信息
    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    @property
    def file_size_mb(self) -> float:
        try:
            return self.db_path.stat().st_size / 1024 / 1024
        except OSError:
            return 0.0

    def describe(self) -> str:
        return f"{self.db_path}（{self.file_size_mb:.1f} MB）"

    # ------------------------------------------------------------ 查询
    def _connect(self) -> sqlite3.Connection | None:
        if not self.exists:
            return None
        try:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return None

    def list_symbols(self) -> list[SymbolSummary]:
        """按合约汇总（条数以 dbtickdata 实际行数为准）。"""
        connection = self._connect()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                "select symbol, exchange, count(*), min(datetime), max(datetime) "
                "from dbtickdata group by symbol, exchange order by symbol"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()
        return [
            SymbolSummary(
                symbol=str(symbol),
                exchange=str(exchange),
                count=int(count),
                start=str(start or ""),
                end=str(end or ""),
            )
            for symbol, exchange, count, start, end in rows
        ]

    def total_ticks(self) -> int:
        connection = self._connect()
        if connection is None:
            return 0
        try:
            return int(connection.execute("select count(*) from dbtickdata").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            connection.close()

    def load_recent(self, symbol: str, exchange: str = "", limit: int = 200) -> list[tuple]:
        """某合约最近 ``limit`` 条 tick（时间倒序）。"""
        connection = self._connect()
        if connection is None:
            return []
        query = (
            "select datetime, last_price, bid_price_1, ask_price_1, "
            "bid_volume_1, ask_volume_1, volume from dbtickdata where symbol = ?"
        )
        params: list = [symbol]
        if exchange:
            query += " and exchange = ?"
            params.append(exchange)
        query += " order by datetime desc limit ?"
        params.append(max(1, int(limit)))
        try:
            return connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def list_days(self) -> list[DaySummary]:
        """按天 × 合约汇总。"""
        connection = self._connect()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                "select substr(datetime, 1, 10) as day, symbol, exchange, count(*) "
                "from dbtickdata group by day, symbol, exchange order by day, symbol"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()
        return [
            DaySummary(day=str(day), symbol=str(symbol), exchange=str(exchange), count=int(count))
            for day, symbol, exchange, count in rows
        ]
