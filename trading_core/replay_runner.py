"""文件驱动的确定性策略运行入口。只能回放；没有发送或撤单能力。"""
from __future__ import annotations
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
from .config_adapter import profile
from .journal import Journal
from .layers import Working, Unknown, Terminal
from .paired_fills import Matched
from .shadow_strategy import ShadowStrategy, EntryChecks, LegQuote, FeeEstimate


def _amount(value):
    if value is None: return None
    price=Decimal(str(value))
    if not price.is_finite(): raise ValueError('成交价不可用')
    return price


def run_events(*, config_path, spread, account, output, events):
    """output 必须是一个未存在的文件；绝不能往已有事实库或影子库写回。"""
    p=profile(config_path,spread)
    path=Path(output); path.touch(exist_ok=False)
    strategy=ShadowStrategy(Journal(path),p.config,account=account,strategy=spread)
    aliases={}; count=0; actions=[]
    for number,event in enumerate(events,1):
        try:
            kind, day=event['type'],event['day']; key=event['id']
            if not isinstance(key,str) or not key.strip() or key in aliases: raise ValueError('输入事件别名重复或无效')
            aliases[key]=None
            if kind=='quote':
                checks=EntryChecks(**event['checks'])
                if p.close_only: checks=replace(checks,can_open=False)
                result=strategy.on_quote(day,LegQuote(**event['near']),LegQuote(**event['far']),now=event['now'],checks=checks)
            elif kind=='protect':
                if not p.exit_preplace: raise ValueError('本合约配置未启用预挂止盈')
                result=strategy.protect(day,key,FeeEstimate(**event['fees']) if event.get('fees') else None)
            else:
                order=aliases.get(event['decision_event'])
                if not order: raise ValueError('不能把回报绑定到未产生意图的事件')
                if kind=='matched':
                    actual=Matched(event['lots'],_amount(event['average_spread']),_amount(event.get('near_average')),
                                   _amount(event.get('far_average')),event['residual_near'],event['residual_far'])
                    result=strategy.on_matched(day,key,order,actual)
                elif kind in ('working','unknown','terminal'):
                    status=(Terminal(order,event['filled']) if kind=='terminal' else
                            Working(order) if kind=='working' else Unknown(order))
                    result=strategy.on_status(day,key,status)
                else: raise ValueError('未知回放事件')
            if result.intent:
                aliases[key]=result.intent.decision_id
                actions.append(dict(event=key,intent=asdict(result.intent)))
            actions.extend(dict(event=key,cancel=asdict(action)) for action in result.cancels)
            count+=1
        except (TypeError,ValueError,KeyError,ArithmeticError) as exc:
            raise ValueError(f'第 {number} 个事件无效；停止回放，诊断流水保留：{exc}') from exc
    return dict(mode='offline_replay',events=count,config_version=p.config.version,actions=actions,
                layers=[dict(**asdict(l),actual_spread=str(strategy.costs[l.tier_id])) for l in strategy.book.layers])


def json_lines(path):
    with Path(path).open(encoding='utf-8') as file:
        for n,line in enumerate(file,1):
            if not line.strip(): continue
            try: yield json.loads(line)
            except ValueError as exc: raise ValueError(f'JSONL 第 {n} 行错误') from exc
