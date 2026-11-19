"""持久化核对完整批次中的业务字段，供跨日复核；不从账户摘要补造行。"""
from dataclasses import asdict
from types import MappingProxyType
from .preflight import QueryRound
from .query_round import QuerySnapshot
from .journal import Outcome

# 只留对账所需字段；不落账户回报里的银行资料、证件、用户备注等无关信息。
COMMON=('BrokerID','TradingDay','ExchangeID','InvestorID','InstrumentID')
FIELDS={
 'orders':COMMON+('OrderSysID','OrderRef','FrontID','SessionID','Direction','CombOffsetFlag','OrderStatus','OrderSubmitStatus','VolumeTotalOriginal','VolumeTraded','LimitPrice','OrderPriceType','TimeCondition','VolumeCondition'),
 'trades':COMMON+('TradeID','OrderSysID','OrderRef','Direction','OffsetFlag','Price','Volume','TradeDate','TradeTime'),
 'accounts':('BrokerID','TradingDay','AccountID','CurrencyID','Available','Balance','CurrMargin','FrozenMargin'),
 'positions':COMMON+('PosiDirection','HedgeFlag','Position','TodayPosition','YdPosition','LongFrozen','ShortFrozen','PositionDate'),
}


def encode_batch(snapshot):
    return dict(schema=1,round=asdict(snapshot.round), rows={kind:[{k:r[k] for k in fields if k in r}
                   for r in getattr(snapshot,kind)] for kind,fields in FIELDS.items()})


def load_batch(journal, *, account, day, token):
    result=None
    for r in journal.replay():
        if (r.account,r.trading_day,r.kind,r.identity)!=(account,day,'account_snapshot',(token,)):
            continue
        if r.outcome==Outcome.CONFLICT: raise ValueError('柜台核对批次存在冲突')
        if r.outcome!=Outcome.NEW: continue
        batch=r.payload.get('batch',{})
        if batch.get('schema')!=1: raise ValueError('该历史快照仅有摘要，不能恢复完整批次')
        scope=QueryRound(**batch['round'])
        if (scope.account,scope.day,scope.token)!=(account,day,token): raise ValueError('批次身份不一致')
        rows=batch['rows']
        result=QuerySnapshot(scope,**{kind:tuple(MappingProxyType(dict(row)) for row in rows[kind]) for kind in FIELDS})
    if result is None: raise KeyError('无此账户、日期和轮次的完整批次')
    return result
