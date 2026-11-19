"""Per-fill fee evidence. Only the explicit paper recorder writes; projections never do.

A ``paper_fill_fee`` uses simulated executions and an identified input schedule,
not an invoice. A separate ``settled_fill_fee`` (same fill identity, with
strategy, decision_id, tier_id, amount, currency='CNY', source) must come from
attributed settlement evidence. Neither intent budgets nor an account-level
commission total can substitute for either record.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .journal import Outcome


def _num(v):
    try:
        n = Decimal(str(v))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError('无效的费用数值') from exc
    if not n.is_finite() or n < 0 or abs(n.adjusted()) > 99:
        raise ValueError('无效的费用数值')
    return n


def _source(p):
    s = p.get('source') if isinstance(p, dict) else None
    if not isinstance(s, str) or not s.strip():
        raise ValueError('缺少费率或结算凭据来源')
    return s


def _amount(unit, rate, price, multiplier, lots):
    if unit not in ('元', '万分之') or price <= 0 or multiplier <= 0 or lots <= 0 or lots != int(lots):
        raise ValueError('无效费率单位、腿价、乘数或成交量')
    return rate * lots * (price * multiplier / 10000 if unit == '万分之' else 1)


def fee_schedule_from_table(near, far, exchange, *, table_path=None):
    """Resolve both legs to named, exchange-matched records. Never use meta.default."""
    import json
    import re
    from pathlib import Path
    from hashlib import sha256

    path = Path(table_path) if table_path is not None else Path(__file__).resolve().parents[1] / 'fees' / 'fee_table.json'
    raw = path.read_bytes(); table = json.loads(raw)
    contracts = {k.upper(): v for k, v in table['contracts'].items()}
    products = {k.upper(): v for k, v in table['products'].items()}
    legs = {}; matched = []
    for symbol in (near, far):
        match = re.fullmatch(r'([a-zA-Z]+)\d+', symbol)
        if not match or near == far:
            raise ValueError('需要两个独立的腿合约代码')
        candidates = [(contracts, key.upper()) for key in (near+'-'+far, near+'_'+far, symbol)]
        candidates.append((products, match[1].upper()))
        hit = next(((key, store[key]) for store, key in candidates if key in store), None)
        if not hit or hit[1].get('exchange') != exchange:
            raise ValueError(f'{symbol} 无匹配交易所的明确费率记录')
        key, spec = hit
        if spec.get('unit') not in ('元', '万分之'):
            raise ValueError('未知费率单位')
        legs[symbol] = {'unit': spec['unit'], **{k: str(_num(spec.get(k))) for k in ('open','close_today','close_yesterday')}}
        matched.append(f'{symbol}={key}')
    source = table.get('meta', {}).get('source') or path.name
    return {'source': f'{source} · {exchange} · {", ".join(matched)} · sha256:{sha256(raw).hexdigest()}', 'legs': legs}


def record_paper_fees(journal, trace, *, fee_schedule, near, far, multiplier, open_day):
    """Record scheduled fees for cumulative attributed leg fills, return new row count.

    None disables recording. Supply a named schedule with unit/open/close_today/
    close_yesterday or ``legs`` keyed by exact symbol containing those fields.
    No unknown rate, price, or opening day falls back to a default.
    """
    if fee_schedule is None:
        return 0
    source = _source(fee_schedule)
    intent = trace.intent
    history = [r for r in journal.replay() if (r.account, r.trading_day) == (intent.account, intent.trading_day)]
    markers = [r for r in history if r.kind == 'paper_session' and r.outcome != Outcome.DUPLICATE]
    if (len(markers) != 1 or markers[0].outcome != Outcome.NEW or markers[0].payload.get('synthetic') is not True
            or trace.state != 'broker_reported' or near == far):
        raise ValueError('需要纸面账户及唯一归属的成交回报')
    opening = intent.payload['purpose'] in ('open', 'add')
    day = intent.trading_day
    if not opening and (not isinstance(open_day, str) or len(open_day) != 8 or not open_day.isdigit() or open_day > day):
        raise ValueError('需要该层开仓交易日')
    kind = 'open' if opening else 'close_today' if open_day == day else 'close_yesterday'
    prior = {r.identity: r for r in history if r.kind == 'paper_fill_fee' and r.outcome != Outcome.DUPLICATE}
    pending = []
    for fill in trace.fills:
        f = fill.payload; symbol = f['symbol']
        if symbol == intent.payload['symbol']:  # Native combo print is not a third chargeable leg.
            continue
        if symbol not in (near, far) or f['offset'] not in {'open': ('0',), 'close_today': ('1','3'), 'close_yesterday': ('1','4')}[kind]:
            raise ValueError('成交合约或开平标志与计费口径不符')
        spec = fee_schedule if 'legs' not in fee_schedule else fee_schedule['legs'][symbol]
        unit = spec['unit']; rate = _num(spec[kind]); px = _num(f['price']); size = _num(multiplier); qty = _num(f['volume'])
        amount = _amount(unit, rate, px, size, qty)
        data = dict(strategy=intent.identity[0], decision_id=intent.identity[1], tier_id=intent.payload['tier_id'],
                    source=source, currency='CNY', unit=unit, rate=str(rate), fee_kind=kind, price=str(px),
                    multiplier=str(size), volume=int(qty), amount=str(amount))
        previous = prior.get(fill.identity)
        if previous and previous.outcome == Outcome.CONFLICT:
            raise ValueError('同笔模拟手续费记录冲突')
        if not previous or previous.payload != data:
            pending.append((fill.identity, data))
    count = 0
    for identity, data in pending:
        _, outcome = journal.append(intent.account, day, 'paper_fill_fee', identity, data)
        if outcome == Outcome.CONFLICT:
            raise ValueError('同笔模拟手续费记录冲突')
        count += outcome == Outcome.NEW
    return count


@dataclass(frozen=True)
class Fees:
    amount: Decimal | None = None
    sources: tuple[str, ...] = ()
    refs: tuple[int, ...] = ()


def order_fees(trace, events, *, paper, near, far):
    """Sum only the complete set of attributed fees in the requested evidence class."""
    intent = trace.intent; kind = 'paper_fill_fee' if paper else 'settled_fill_fee'
    fills = {f.identity: f for f in trace.fills if f.payload.get('symbol') in (near, far)}
    if not fills or trace.state != 'broker_reported':
        return Fees()
    items = [r for r in events if r.kind == kind and r.identity in fills and
             (r.account, r.trading_day) == (intent.account, intent.trading_day) and r.outcome != Outcome.DUPLICATE]
    refs = tuple(r.sequence for r in items)
    if len(items) != len(fills) or {r.identity for r in items} != fills.keys():
        return Fees(refs=refs)
    total = Decimal(0); sources = set()
    for r in items:
        p = r.payload
        try:
            if (r.outcome != Outcome.NEW or (p['strategy'], p['decision_id']) != intent.identity
                    or p['tier_id'] != intent.payload['tier_id'] or p['currency'] != 'CNY'):
                return Fees(refs=refs)
            amount = _num(p['amount']); sources.add(_source(p))
            if paper:
                f = fills[r.identity].payload; px = _num(f['price']); qty = _num(f['volume'])
                if (_num(p['price']) != px or _num(p['volume']) != qty or
                        _amount(p['unit'], _num(p['rate']), px, _num(p['multiplier']), qty) != amount):
                    return Fees(refs=refs)
            total += amount
        except (TypeError, KeyError, ArithmeticError, ValueError):
            return Fees(refs=refs)
    return Fees(total, tuple(sorted(sources)), refs)
