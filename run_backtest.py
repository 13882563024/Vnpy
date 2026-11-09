"""跨期均值回归策略 · 回测入口。

数据前置（硬要求）：**两腿各自的 tick 数据必须已经在 vnpy 数据库里**
（默认 ``~/.vntrader/database.db``，由 ``vnpy_sqlite`` 提供）。本脚本会在内存里
合成价差，不需要预先落库"合成 tick"。

示例::

    # 先看有哪些价差可以回测
    .venv\\Scripts\\python.exe run_backtest.py --list

    # 回测 2026-09-01 ~ 2026-10-31 的豆一跨期，门槛用自适应
    .venv\\Scripts\\python.exe run_backtest.py --spread A_2611_2701 `
        --start 2026-09-01 --end 2026-10-31

    # 固定门槛（便于与推荐文档的 50/10/10/10/10 对拍）
    .venv\\Scripts\\python.exe run_backtest.py --spread RM_701_703 `
        --start 2026-09-01 --end 2026-10-31 --setting min_exit_pnl_override=10

    # 打开资金曲线图
    .venv\\Scripts\\python.exe run_backtest.py --spread A_2611_2701 `
        --start 2026-09-01 --end 2026-10-31 --show

回测结果的乐观偏差（方案 8.2/8.3）：撮合是"触价即成交"，不建模 maker 排队；
两腿按同一条合成 tick 同时成交，实盘存在先后与单腿裸露。**按打折理解**。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import (                                          # noqa: E402
    CrossPeriodBacktestingEngine,
    build_spread_data,
)
from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy  # noqa: E402
from utils.spread_config import SpreadConfig, load_spread_configs         # noqa: E402
from vnpy.trader.constant import Interval                                  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="跨期均值回归策略回测")
    parser.add_argument("--spread", default="", help="价差名（见 config/spreads.json）")
    parser.add_argument("--list", action="store_true", help="列出可回测的价差")
    parser.add_argument("--start", default="", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--slippage", type=float, default=0.0, help="价差滑点（点）")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--rate", type=float, default=0.0,
                        help="（保留）引擎速率；手续费已按 FeeCalculator 逐腿精确计算")
    parser.add_argument("--setting", action="append", default=[],
                        help="覆盖策略参数 key=value（可多次）")
    parser.add_argument("--show", action="store_true", help="回测后打开资金曲线图")
    parser.add_argument("--log-limit", type=int, default=40, help="打印多少条策略日志")
    return parser.parse_args(argv)


def coerce(text: str):
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    for caster in (int, float):
        try:
            return caster(text)
        except ValueError:
            continue
    return text


def parse_overrides(items: list[str]) -> dict:
    overrides: dict = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"参数覆盖格式应为 key=value：{item}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = coerce(value.strip())
    return overrides


def print_spreads(configs: dict[str, SpreadConfig]) -> None:
    if not configs:
        print("config/spreads.json 为空或无法解析")
        return
    print("可回测的价差：")
    for name, config in configs.items():
        print(f"  {name:<16} {config.near_symbol} / {config.far_symbol} "
              f"乘数{config.size:g} 跳{config.pricetick:g} "
              f"执行器={config.executor} 时段={config.trading_sessions or '不限'}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configs = load_spread_configs()

    if args.list or not args.spread:
        print_spreads(configs)
        if not args.spread:
            print("\n请用 --spread 指定要回测的价差")
        return 0

    config = configs.get(args.spread)
    if config is None:
        print(f"[错误] config/spreads.json 中没有价差 {args.spread}；用 --list 查看")
        return 1

    try:
        start = datetime.fromisoformat(args.start) if args.start else datetime(2026, 1, 1)
        end = datetime.fromisoformat(args.end) if args.end else datetime.now()
    except ValueError as exc:
        print(f"[错误] 日期格式应为 YYYY-MM-DD：{exc}")
        return 1

    spread = build_spread_data(config)

    engine = CrossPeriodBacktestingEngine()
    engine.set_parameters(
        spread=spread,
        interval=Interval.TICK,
        start=start,
        rate=args.rate,
        slippage=args.slippage,
        size=config.size,
        pricetick=config.pricetick,
        capital=int(args.capital),
        end=end,
    )

    setting = parse_overrides(args.setting)
    setting.setdefault("live_trading", True)      # 回测需要真的产生委托
    engine.add_strategy(CrossPeriodMRStrategy, setting)

    engine.load_data()
    if not engine.history_data:
        print(
            "\n[错误] 没有合成出任何价差 tick。请先确认两腿 tick 已入库：\n"
            f"  {config.near_symbol} / {config.far_symbol}\n"
            "数据落在 ~/.vntrader/database.db（vnpy_sqlite）。方案 8.1 已说明这是硬前置。"
        )
        return 1

    engine.run_backtesting()
    engine.calculate_result()
    statistics = engine.calculate_statistics(output=True)

    logs = engine.logs
    if logs:
        limit = max(0, int(args.log_limit))
        print(f"\n策略日志（共 {len(logs)} 条，显示前 {limit} 条）：")
        for line in logs[:limit]:
            print(f"  {line}")
    if not logs:
        print("\n策略日志为空：可能整段区间没有信号或全部被过滤器拦截")

    if statistics and statistics.get("total_net_pnl") is not None:
        print(
            f"\n净值 {statistics['total_net_pnl']:.0f} 元 / "
            f"手续费 {statistics['total_commission']:.0f} 元 / "
            f"成交 {statistics['total_trade_count']} 笔"
        )

    if args.show:
        engine.show_chart()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
