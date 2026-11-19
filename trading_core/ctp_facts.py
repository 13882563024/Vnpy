"""CTP 回报的稳定业务字段（不依赖 vn.py、行情目录或 OMS 缓存）。"""
from datetime import datetime
import json
from math import isfinite

from .journal import Journal, Outcome


def _text(value) -> str:
    return '' if value is None else str(value).strip()


def _required(value, field: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f'回报缺少 {field}')
    return text


def account_key(broker: str, investor: str) -> str:
    return json.dumps([_required(broker, 'BrokerID'), _required(investor, 'InvestorID')],
                      ensure_ascii=False, separators=(',', ':'))


def _number(data: dict, field: str) -> float:
    value = float(data[field])
    if not isfinite(value):
        raise ValueError(f'{field} 不是有限数值')
    return value


def trading_day(data: dict, login_day: str) -> str:
    # 夜盘 TradeDate / InsertDate 是自然日，不得代替 TradingDay。
    day = _required(_text(data.get('TradingDay')) or login_day, 'TradingDay').replace('-', '')
    if len(day) != 8 or not day.isascii() or not day.isdigit():
        raise ValueError('无效 TradingDay')
    datetime.strptime(day, '%Y%m%d')
    return day


def append_report(journal: Journal, kind: str, data: dict, broker: str, investor: str,
                  login_day: str) -> Outcome:
    account = account_key(broker, investor)
    for field, expected in [('BrokerID', broker), ('InvestorID', investor)]:
        if _text(data.get(field)) not in ('', _text(expected)):
            raise ValueError(f'回报 {field} 与当前连接不符')
    day = trading_day(data, login_day)
    symbol = _required(data.get('InstrumentID'), 'InstrumentID')
    payload = {'symbol': symbol, 'direction': _text(data.get('Direction'))}
    exchange = _text(data.get('ExchangeID'))
    if kind == 'fill':
        # 不以价格、时间、接收来源、内存解析出的本地委托号来区分成交。
        key = (_required(exchange, 'ExchangeID'), _required(data.get('TradeID'), 'TradeID'), symbol)
        payload.update(order_sys_id=_text(data.get('OrderSysID')), order_ref=_text(data.get('OrderRef')),
                       offset=_text(data.get('OffsetFlag')), price=_number(data, 'Price'),
                       volume=_number(data, 'Volume'), date=_text(data.get('TradeDate')),
                       time=_text(data.get('TradeTime')))
    elif kind in ('order', 'order_reject'):
        key = tuple(_required(data.get(f), f) for f in ('FrontID', 'SessionID', 'OrderRef'))
        payload.update(exchange=exchange, order_sys_id=_text(data.get('OrderSysID')),
                       offset=_text(data.get('CombOffsetFlag')), price=_number(data, 'LimitPrice'),
                       volume=_number(data, 'VolumeTotalOriginal'))
        if kind == 'order':
            traded = _number(data, 'VolumeTraded')
            # 柜台状态和累计成交量标识一个快照，而不是以委托号抹掉后续变化。
            key += (_required(data.get('OrderStatus'), 'OrderStatus'),
                    _text(data.get('OrderSubmitStatus')) or 'unspecified', str(traded))
            payload.update(traded=traded, order_price_type=_text(data.get('OrderPriceType')),
                           time_condition=_text(data.get('TimeCondition')),
                           volume_condition=_text(data.get('VolumeCondition')))
        else:
            payload['error_id'] = _text(data.get('ErrorID'))
    else:
        raise ValueError(f'未知 CTP 事实类型 {kind}')
    return journal.append(account, day, kind, key, payload)[1]
