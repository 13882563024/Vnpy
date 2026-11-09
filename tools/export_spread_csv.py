"""按交易日导出价差 tick 为 CSV。

用法：

    # 导出 2026-11-12（交易日）全部价差 → exports/spread_csv/2026-11-12/*.csv
    .venv\\Scripts\\python.exe tools\\export_spread_csv.py --date 2026-11-12

    # 只导某几个价差，指定输出目录
    .venv\\Scripts\\python.exe tools\\export_spread_csv.py --date 2026-11-12 \\
        --spread A_2701_2703 --spread V_2701_2703 --out D:\\tmp\\csv

    # 强制现合成（不用库里已存的价差 tick）
    .venv\\Scripts\\python.exe tools\\export_spread_csv.py --date 2026-11-11 --source synth

说明：

1. 交易日 D 的窗口是「前一工作日晚 20:00 ~ D 当天」，夜盘自动归到 D；
2. 库里已有价差 tick（symbol=价差名、exchange=LOCAL）就直接用，没有就两腿合成；
3. 程序（GUI）正在跑也能导：vnpy 默认把日志写到 ``~/.vntrader/log``，那个文件被
   GUI 占着，同进程再打开会 PermissionError。这里临时把 TRADER_DIR 指到别处再打开
   **真实库文件**，两边互不影响。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按交易日导出价差 CSV")
    parser.add_argument("--date", required=True, help="交易日，如 2026-11-12")
    parser.add_argument("--spread", action="append", default=[],
                        help="只导这个价差（可多次指定）；缺省 = spreads.json 全部")
    parser.add_argument("--out", default="", help="输出根目录；缺省 exports/spread_csv")
    parser.add_argument("--source", choices=("auto", "stored", "synth"), default="auto",
                        help="auto=优先库里的价差 tick、没有就合成（默认）")
    parser.add_argument("--quiet", action="store_true", help="不打印每条腿的载入信息")
    return parser.parse_args(argv)


def real_database_path() -> Path:
    """真实库文件：程序在项目根跑时是 ``./.vntrader/database.db``，否则是用户目录。"""
    local = Path.cwd() / ".vntrader" / "database.db"
    if local.exists():
        return local
    return Path.home() / ".vntrader" / "database.db"


def open_database():
    """打开真实库，同时把 vnpy 的日志目录挪到临时目录（避免和运行中的 GUI 抢文件）。"""
    real = real_database_path()
    if not real.exists():
        raise FileNotFoundError(f"数据库不存在：{real}")

    redirect: Path | None = None
    try:
        redirect = Path(tempfile.mkdtemp(prefix="spread-export-"))
        (redirect / ".vntrader").mkdir(parents=True, exist_ok=True)
    except Exception:          # noqa: BLE001 - 临时目录建不出来就沿用默认目录
        redirect = None

    original = Path.cwd()
    try:
        if redirect is not None:
            os.chdir(redirect)
        import vnpy_sqlite.sqlite_database as sq

        sq.db.close()
        sq.db.init(str(real))
        return sq.SqliteDatabase()
    finally:
        os.chdir(original)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from utils.calendar import parse_trading_day

    day = parse_trading_day(args.date)
    if day is None:
        print(f"[错误] 日期格式应为 YYYY-MM-DD：{args.date}")
        return 2

    try:
        database = open_database()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 打不开数据库：{exc!r}")
        return 1
    print(f"数据库：{real_database_path()}")

    # 放在 open_database 之后导入：它会拉起 vnpy_spreadtrading → vnpy 日志模块，
    # 那时 TRADER_DIR 已经被指到临时目录了。
    from utils.spread_export import export_spread_day

    output = (lambda *_args, **_kwargs: None) if args.quiet else print
    out_dir = Path(args.out) if args.out else None
    try:
        report = export_spread_day(
            day,
            out_dir,
            names=args.spread or None,
            source=args.source,
            database=database,
            output=output,
        )
    except Exception as exc:  # noqa: BLE001 - 命令行工具：给出可读错误即可
        print(f"[错误] 导出失败：{exc!r}")
        return 1

    for line in report.describe().splitlines():
        print(line)
    if not report.total_rows:
        print("提示：该交易日没有任何 tick —— 可能没开程序采集，或日期/价差选错了。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
