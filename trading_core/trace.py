"""只读关联决策 → 发送预留 → 柜台委托 → 成交；不推测仓位，也不产生许可。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .journal import Outcome, Record


@dataclass(frozen=True)
class DecisionTrace:
    intent: Record
    bindings: tuple[Record, ...]
    orders: tuple[Record, ...]
    fills: tuple[Record, ...]
    issues: tuple[str, ...]

    @property
    def state(self) -> str:
        if self.issues:
            return 'needs_review'
        if not self.bindings:
            return 'reserved_only'
        if not self.orders:
            return 'send_result_unknown'
        return 'broker_reported'  # 不等同于已成交、已归账或已终结


def trace_decision(events: Iterable[Record], account: str, day: str,
                   strategy: str, decision_id: str) -> DecisionTrace:
    scope = [r for r in events if (r.account, r.trading_day) == (account, day)]
    facts = [r for r in scope if r.outcome == Outcome.NEW]
    key = strategy, decision_id
    intent = next((r for r in facts if r.kind == 'order_intent' and r.identity == key), None)
    if intent is None:
        raise KeyError(key)
    bindings = tuple(r for r in facts if r.kind == 'order_binding' and
                     (r.payload.get('strategy'), r.payload.get('decision_id')) == key)
    local = {b.identity for b in bindings}
    orders = tuple(r for r in facts if r.kind in ('order','order_reject') and r.identity[:3] in local)
    sys_owners: dict[tuple[str, str], set] = {}
    for r in facts:
        if r.kind == 'order' and r.payload.get('order_sys_id') and r.payload.get('exchange'):
            syskey = (r.payload['exchange'], r.payload['order_sys_id'])
            sys_owners.setdefault(syskey, set()).add(r.identity[:3])
    issues = []
    if len(bindings) > 1:
        issues.append('同一意图绑定多个报单引用')
    eligible = set()
    for syskey, owners in sys_owners.items():
        if local & owners:
            if len(owners) == 1:
                eligible.add(syskey)
            else:
                issues.append('交易所委托号归属不唯一：' + repr(syskey))
    fills = tuple(r for r in facts if r.kind == 'fill' and
                  (r.identity[0], r.payload.get('order_sys_id')) in eligible)
    related = {(r.kind, r.identity) for r in (intent, *bindings, *orders, *fills)}
    for r in scope:
        if r.outcome == Outcome.CONFLICT and (r.kind, r.identity) in related:
            issues.append(f'关联流水冲突 #{r.sequence}')
    return DecisionTrace(intent, bindings, orders, fills, tuple(issues))
