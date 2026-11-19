"""只读查看柜台事实流水，不连接网关、不导入交易引擎。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from trading_core.journal import Journal, Outcome


def summarize(path: Path, *, account: str | None = None, day: str | None = None) -> dict:
    counts = Counter()
    kinds = Counter()
    conflicts = []
    last_sequence = 0
    for event in Journal.open_readonly(path).replay():
        if account is not None and event.account != account:
            continue
        if day is not None and event.trading_day != day:
            continue
        counts[event.outcome.value] += 1
        if event.outcome == Outcome.NEW:
            kinds[event.kind] += 1
        elif event.outcome == Outcome.CONFLICT:
            conflicts.append(event.sequence)
        last_sequence = event.sequence
    return {'last_sequence': last_sequence, 'outcomes': dict(counts),
            'canonical_events': dict(kinds), 'conflict_sequences': conflicts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='已有事实库路径')
    parser.add_argument('--day', help='柜台交易日 YYYYMMDD')
    parser.add_argument('--account', help='完整账本账户键')
    parser.add_argument('--decision', nargs=2, metavar=('STRATEGY', 'DECISION_ID'), help='追踪指定策略决策')
    parser.add_argument('--legs', nargs=2, metavar=('NEAR', 'FAR'), help='按给定近远腿代码展示已匹配成交及未配对余量')
    args = parser.parse_args()
    if args.legs and not args.decision:
        parser.error('--legs 需要 --decision')
    if args.decision:
        from dataclasses import asdict
        from trading_core.trace import trace_decision
        if not args.account or not args.day:
            parser.error('--decision 需要 --day 和 --account')
        try:
            trace = trace_decision(Journal.open_readonly(args.path).replay(), args.account,
                                   args.day, *args.decision)
        except KeyError:
            parser.error('在指定账户与交易日未找到该决策')
        report = {'state': trace.state, **asdict(trace)}
        if args.legs:
            from trading_core.decision_view import describe_execution
            report['execution'] = describe_execution(trace, near=args.legs[0], far=args.legs[1])
    else:
        report = summarize(args.path, account=args.account, day=args.day)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
