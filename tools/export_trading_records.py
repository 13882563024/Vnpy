"""导出委托/成交记录：按交易日复制或合并 ``~/.vntrader/records`` 下的 CSV。

用法::

    # 看有哪些交易日、各自多少行
    python tools\\export_trading_records.py --list

    # 导出单个交易日到 exports/trading_records/<日期>/
    python tools\\export_trading_records.py --date 2026-11-12

    # 合并一段区间（含两端）到 exports/trading_records/<起>_<止>/
    python tools\\export_trading_records.py --from 2026-11-01 --to 2026-11-13

    # 只看某个价差（按 vt_symbol 或价差名做子串匹配，如 PK_701_703 / PK701）
    python tools\\export_trading_records.py --date 2026-11-12 --match PK

记录由 ``recorder.trade_recorder.TradeRecorder`` 实时写入，程序运行期间就有了；
本工具只做汇总/过滤，不连接柜台、不访问数据库。GUI 里对应「导出交易记录 CSV…」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from recorder.trade_recorder import DEFAULT_RECORD_ROOT  # noqa: E402
from utils.record_export import describe_days, export_range, list_days  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="导出委托/成交记录（按交易日 CSV 汇总）")
    parser.add_argument("--root", default=str(DEFAULT_RECORD_ROOT),
                        help="记录根目录（默认 ~/.vntrader/records）")
    parser.add_argument("--out", default=None,
                        help="输出目录（默认 exports/trading_records/<日期>）")
    parser.add_argument("--list", action="store_true", help="列出已记录的交易日")
    parser.add_argument("--date", default=None, help="只导出该交易日（YYYY-MM-DD）")
    parser.add_argument("--from", dest="start", default=None, help="起始交易日")
    parser.add_argument("--to", dest="end", default=None, help="结束交易日")
    parser.add_argument("--match", default="",
                        help="按子串过滤（价差名或合约代码，如 PK_701_703）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser()

    if args.list or (not args.date and not args.start):
        rows = list_days(root)
        print(describe_days(rows))
        return 0 if rows else 1

    if args.date:
        try:
            start = end = date.fromisoformat(args.date)
        except ValueError:
            print(f"日期格式非法：{args.date!r}（应为 YYYY-MM-DD）")
            return 2
    else:
        try:
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end or args.start)
        except ValueError:
            print("日期格式非法（--from / --to 应为 YYYY-MM-DD）")
            return 2

    try:
        result = export_range(start, end, root=root, out_dir=args.out,
                              match=args.match, output=print)
    except (ValueError, FileNotFoundError) as exc:
        print(f"导出失败：{exc}")
        return 2
    if not any(result.values()):
        print("该区间没有匹配的记录")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
