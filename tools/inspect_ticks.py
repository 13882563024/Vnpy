"""查看已采集的 tick 数据：概况 / 某合约明细 / 按天汇总 / 合成价差预览。

数据落在 vnpy 数据库（默认 ``~/.vntrader/database.db``，SQLite）。查询逻辑与 GUI 的
「功能 → 行情数据」面板共用 ``recorder/tick_store.py``，两边看到的数字一定一致。

用法::

    # 1) 概况：每个合约多少条、时间范围
    .venv\\Scripts\\python.exe tools\\inspect_ticks.py

    # 2) 某合约最近 20 条明细
    .venv\\Scripts\\python.exe tools\\inspect_ticks.py --symbol OI701 --limit 20

    # 3) 按天汇总（检查每天采了多少）
    .venv\\Scripts\\python.exe tools\\inspect_ticks.py --by-day

    # 4) 预览某价差合成结果（两腿 tick → 价差 bid/ask/mid）
    .venv\\Scripts\\python.exe tools\\inspect_ticks.py --spread OI_701_703 --limit 15
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from recorder.tick_store import DEFAULT_DB_PATH, TickStore  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查看已采集的 tick 数据")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="数据库文件路径")
    parser.add_argument("--symbol", default="", help="只看某个合约（如 OI701）")
    parser.add_argument("--exchange", default="", help="配合 --symbol 指定交易所（如 CZCE）")
    parser.add_argument("--limit", type=int, default=15, help="明细/预览显示条数")
    parser.add_argument("--by-day", action="store_true", help="按天汇总")
    parser.add_argument("--spread", default="", help="预览某个价差的合成结果（名字见 config/spreads.json）")
    return parser.parse_args(argv)


def show_overview(store: TickStore) -> None:
    symbols = store.list_symbols()
    if not symbols:
        print("dbtickdata 里还没有数据 —— 先用 run_recorder.py 或 run_strategy.py --record 采一段")
        return

    print(f"{'合约':<12} {'条数':>8}  {'最早':<21} {'最晚':<21}")
    print("-" * 68)
    for summary in symbols:
        print(f"{summary.vt_symbol:<12} {summary.count:>8}  {summary.start:<21} {summary.end:<21}")
    print("-" * 68)
    print(f"合计 {len(symbols)} 个合约 / {store.total_ticks()} 条 tick")
    print("（K 线表为空属正常；本项目回测只用 tick）")


def show_by_day(store: TickStore) -> None:
    days = store.list_days()
    if not days:
        print("还没有数据")
        return
    print(f"{'日期':<12} {'合约':<12} {'条数':>8}")
    print("-" * 36)
    for day in days:
        print(f"{day.day:<12} {day.symbol}.{day.exchange:<10} {day.count:>8}")


def show_symbol(store: TickStore, symbol: str, exchange: str, limit: int) -> None:
    rows = store.load_recent(symbol, exchange, limit)
    if not rows:
        print(f"没有 {symbol} 的数据")
        return

    print(f"{symbol} 最近 {len(rows)} 条（时间倒序）：")
    print(f"{'时间':<21} {'最新':>10} {'买一':>10} {'卖一':>10} {'买量':>7} {'卖量':>7} {'成交量':>9}")
    print("-" * 84)
    for moment, last, bid, ask, bid_volume, ask_volume, volume in rows:
        print(f"{str(moment):<21} {last:>10.2f} {bid:>10.2f} {ask:>10.2f} "
              f"{bid_volume:>7.0f} {ask_volume:>7.0f} {volume:>9.0f}")


def show_spread(name: str, limit: int) -> None:
    """复用回测引擎的合成逻辑，确认「两腿 tick → 价差」拼得对不对。"""
    from utils.spread_config import load_spread_configs

    config = load_spread_configs().get(name)
    if config is None:
        print(f"[错误] config/spreads.json 里没有价差 {name}")
        return

    from backtest.engine import build_spread_data, synthesize_spread_ticks

    spread = build_spread_data(config)
    ticks, _frames = synthesize_spread_ticks(
        spread, datetime(2000, 1, 1), datetime.now() + timedelta(days=1),
        output=lambda *_: None,
    )
    if not ticks:
        print(f"没有可合成的 tick —— 先采集 {config.near_symbol} / {config.far_symbol}")
        return

    print(f"价差 {name}：{config.near_symbol} − {config.far_symbol}"
          f"（乘数 {config.size:g} 跳 {config.pricetick:g}）")
    print(f"合成 {len(ticks)} 条价差 tick，时间 {ticks[0].datetime} ~ {ticks[-1].datetime}")
    print()
    print(f"{'时间':<30} {'价差bid':>10} {'价差ask':>10} {'mid':>10} {'可成交手数':>10}")
    print("-" * 78)
    for tick in ticks[-max(1, limit):]:
        mid = (tick.bid_price_1 + tick.ask_price_1) / 2
        depth = min(tick.bid_volume_1, tick.ask_volume_1)
        print(f"{str(tick.datetime):<30} {tick.bid_price_1:>10.2f} {tick.ask_price_1:>10.2f} "
              f"{mid:>10.2f} {depth:>10.0f}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.spread:
        show_spread(args.spread, args.limit)
        return 0

    store = TickStore(args.db)
    if not store.exists:
        print(f"[错误] 数据库不存在：{store.db_path}")
        return 1

    print(f"数据库：{store.describe()}\n")
    if args.symbol:
        show_symbol(store, args.symbol, args.exchange, args.limit)
    elif args.by_day:
        show_by_day(store)
    else:
        show_overview(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
