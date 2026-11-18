"""把 ``~/.vntrader/records`` 下的委托/成交 CSV 汇总导出。

与 :mod:`utils.spread_export`（按交易导出价差 tick）对称：这里是**交易侧**的导出，
数据源是 :class:`recorder.trade_recorder.TradeRecorder` 实时落盘的 CSV，不连柜台、
不读数据库，所以可以在 GUI 里同步执行（毫秒级）。
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from recorder.trade_recorder import (
    DEFAULT_RECORD_ROOT,
    ORDER_HEADER,
    SPREAD_TRADE_HEADER,
    TRADE_HEADER,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "exports" / "trading_records"

#: (文件名, 表头, 汇总键) —— 三个文件一起导，缺的跳过
FILES = (
    ("orders.csv", ORDER_HEADER, "orders"),
    ("trades.csv", TRADE_HEADER, "trades"),
    ("spread_trades.csv", SPREAD_TRADE_HEADER, "spread_trades"),
)

FILE_LABELS = {
    "orders": "委托记录",
    "trades": "成交明细",
    "spread_trades": "价差成交",
}


def count_data_lines(path: Path) -> int:
    """数据行数（不含表头）。"""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def list_days(root: Path | str = DEFAULT_RECORD_ROOT) -> list[tuple[str, int, int, int]]:
    """→ [(交易日, 委托行数, 成交行数, 价差成交行数)]，按日期倒序。"""
    base = Path(root)
    if not base.exists():
        return []
    rows: list[tuple[str, int, int, int]] = []
    for item in sorted(base.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        rows.append((
            item.name,
            count_data_lines(item / "orders.csv"),
            count_data_lines(item / "trades.csv"),
            count_data_lines(item / "spread_trades.csv"),
        ))
    return rows


def _read_rows(path: Path, header: tuple[str, ...], match: str) -> list[tuple]:
    if not path.exists():
        return []
    found: list[tuple] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if match and match.lower() not in " ".join(
                    str(value) for value in row.values()).lower():
                continue
            found.append(tuple(row.get(name, "") for name in header))
    return found


def export_range(
    start: date,
    end: date,
    *,
    root: Path | str = DEFAULT_RECORD_ROOT,
    out_dir: Path | str | None = None,
    match: str = "",
    output=print,
) -> dict[str, int]:
    """把 ``[start, end]`` 的委托/成交合并写出；返回 {"orders": n, "trades": n, ...}。

    ``match`` 按子串过滤（价差名或合约代码都行）。没有记录的日子直接跳过。
    """
    if end < start:
        raise ValueError("结束日期早于开始日期")
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"没有记录目录：{base}；程序运行并产生委托/成交后才会生成")

    target = Path(out_dir) if out_dir else DEFAULT_OUTPUT_ROOT / (
        start.isoformat() if start == end else f"{start.isoformat()}_{end.isoformat()}")
    target.mkdir(parents=True, exist_ok=True)

    result = {"orders": 0, "trades": 0, "spread_trades": 0}
    for filename, header, key in FILES:
        rows: list[tuple] = []
        for day_dir in sorted(base.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            rows.extend(_read_rows(day_dir / filename, header, match))
        if not rows:
            continue
        path = target / filename
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(rows)
        result[key] = len(rows)
        output(f"  {filename:<18} {len(rows):>6} 行 → {path}")
    return result


def describe_days(rows: list[tuple[str, int, int, int]]) -> str:
    """把 :func:`list_days` 的结果排成一段可读文本（GUI 里直接显示）。"""
    if not rows:
        return "还没有任何记录（程序运行并产生委托/成交后才会生成）"
    lines = [f"{'交易日':<12}{'委托':>8}{'成交':>8}{'价差成交':>10}"]
    lines.append("-" * 40)
    for day, orders, trades, spreads in rows:
        lines.append(f"{day:<12}{orders:>8}{trades:>8}{spreads:>10}")
    return "\n".join(lines)
