"""发送前的 CTP 本地身份绑定；不是柜台受理、成交或逐笔核验。"""
from __future__ import annotations

from decimal import Decimal
from math import isfinite

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest

from .intents import OrderIntent
from .journal import Journal, Outcome


BINDING_KIND = 'order_binding'


def validate_intent(req: OrderRequest, intent: OrderIntent, account: str, day: str,
                    pricetick: float) -> None:
    """按实际将要发送的请求校验，禁止用 int(volume) 截断冒充匹配。"""
    if not isinstance(intent, OrderIntent):
        raise ValueError('策略报单缺少 OrderIntent')
    if (intent.account, intent.day) != (account, day):
        raise ValueError('意图与当前登录账户或柜台交易日不符')
    if req.symbol != intent.symbol or req.exchange == Exchange.LOCAL:
        raise ValueError('意图合约或交易所无效')
    if req.direction not in (Direction.LONG, Direction.SHORT) or intent.side != (
            'buy' if req.direction == Direction.LONG else 'sell'):
        raise ValueError('意图与报单方向不符')
    if (type(req.volume) not in (int, float) or not isfinite(req.volume)
            or req.volume != intent.lots or type(intent.lots) is not int or intent.lots <= 0):
        raise ValueError('意图与报单手数不符')
    if (req.type != OrderType.LIMIT
            or type(req.price) not in (int, float) or not isfinite(req.price)
            or type(intent.price) not in (int, float) or req.price != intent.price):
        raise ValueError('意图与报单限价不符')
    if (type(pricetick) not in (int, float) or not isfinite(pricetick) or pricetick <= 0
            or Decimal(str(req.price)) % Decimal(str(pricetick)) != 0):
        raise ValueError('缺少有效最小变动价位或限价未对齐')
    if not ((intent.purpose in ('open', 'add') and req.offset == Offset.OPEN)
            or (intent.purpose in ('take_profit', 'risk_exit') and req.offset in (
                Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY))):
        raise ValueError('意图用途与报单开平不符')


def reserve_and_bind(journal: Journal, intent: OrderIntent, identity: tuple[str, str, str],
                     exchange: str) -> Outcome:
    """两次独立提交：中途崩溃只留意图，重入拒发。不能将 DUPLICATE 当作重试许可。"""
    result = intent.reserve(journal)
    if result != Outcome.NEW:
        return result
    return journal.append(intent.account, intent.day, BINDING_KIND, identity,
                          {'strategy': intent.strategy, 'decision_id': intent.decision_id,
                           'exchange': exchange})[1]
