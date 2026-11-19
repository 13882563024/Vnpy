"""柜台完整核对结果的只读展示投影。账户未知项不默认成零。"""
from datetime import datetime, timezone
from math import isfinite
from sys import float_info

from .journal import Outcome
from .snapshot_archive import encode_batch


def _money(row, name):
    value = row.get(name)
    return value if type(value) in (float, int) and isfinite(value) and abs(value) < float_info.max else None


def record_snapshot(journal, snapshot):
    # 多币种不可加总。保留各币种及缺失项，顶层数值仅供唯一人民币账户使用。
    accounts = [dict(currency=r.get('CurrencyID'), balance=_money(r,'Balance'),
                     available=_money(r,'Available'), margin=_money(r,'CurrMargin'),
                     frozen_margin=_money(r,'FrozenMargin')) for r in snapshot.accounts]
    cny = accounts[0] if len(accounts)==1 and accounts[0]['currency']=='CNY' else {}
    positions = [dict(symbol=row['InstrumentID'], direction=row['PosiDirection'],lots=row['Position'],
                      exchange=row.get('ExchangeID'), date_type=row.get('PositionDate'),
                      hedge_flag=row.get('HedgeFlag'), long_frozen=row.get('LongFrozen'),
                      short_frozen=row.get('ShortFrozen')) for row in snapshot.positions]
    scope = snapshot.round
    payload = dict(available=cny.get('available'), balance=cny.get('balance'), currency=cny.get('currency'),
                   accounts=accounts, batch=encode_batch(snapshot), order_count=len(snapshot.orders),trade_count=len(snapshot.trades),
                   positions=positions, session=scope.session,strategy=scope.strategy,decision=scope.decision,
                   # UI 需能判断这是哪一次历史查询；此字段不是柜台成交时间/交易许可。
                   observed_at=datetime.now(timezone.utc).isoformat())
    seq,outcome = journal.append(scope.account,scope.day,'account_snapshot',(scope.token,),payload)
    if outcome != Outcome.NEW:
        raise ValueError('本轮快照重复或冲突')
    return seq
