"""按策略隔离的逐笔证据（scoped_check）门禁用例。"""
from __future__ import annotations

import unittest
from time import monotonic

from trading_core.ctp_facts import account_key
from trading_core.intents import OrderIntent
from trading_core.preflight import GateDenied, QueryRound
from trading_core.query_round import QuerySnapshot
from trading_core.scoped_check import ScopedLimits, scoped_evidence

BROKER, INVESTOR = '9999', '000001'
DAY = '20261120'
ACCOUNT = account_key(BROKER, INVESTOR)


def _round(token='t1', strategy='CS_2701_2703', decision='d1'):
    return QueryRound(token, ACCOUNT, DAY, '1/2', strategy, decision, 0, monotonic())


def _account_row(cash=200_000.0):
    return {'BrokerID': BROKER, 'AccountID': INVESTOR, 'TradingDay': DAY,
            'CurrencyID': 'CNY', 'Available': cash}


def _pos(symbol, direction, lots, *, long_frozen=0, short_frozen=0, today=0, exchange='DCE'):
    return {'BrokerID': BROKER, 'InvestorID': INVESTOR, 'TradingDay': DAY,
            'InstrumentID': symbol, 'PosiDirection': direction, 'Position': lots,
            'LongFrozen': long_frozen, 'ShortFrozen': short_frozen,
            'TodayPosition': today, 'YdPosition': lots - today,
            'ExchangeID': exchange, 'HedgeFlag': '1'}


def _order(symbol, status='0', front='1', session='2', ref='3', exchange='DCE'):
    return {'BrokerID': BROKER, 'InvestorID': INVESTOR, 'TradingDay': DAY,
            'InstrumentID': symbol, 'OrderStatus': status, 'VolumeTraded': 0,
            'FrontID': int(front), 'SessionID': int(session), 'OrderRef': ref,
            'ExchangeID': exchange}


def _snapshot(positions=(), orders=(), cash=200_000.0, strategy='CS_2701_2703', decision='d1'):
    return QuerySnapshot(_round(strategy=strategy, decision=decision),
                         tuple(orders), (), (_account_row(cash),), tuple(positions))


def _intent(purpose='take_profit', side='sell', lots=1, symbol='SP cs2701&cs2703',
            strategy='CS_2701_2703', decision='d1', price=-6.0):
    return OrderIntent(ACCOUNT, DAY, strategy, decision, 'tier-1', purpose, symbol,
                       side, lots, price, 'cfg-v1', {'text': '预挂止盈'})


def _limits(**kw):
    base = dict(strategy='CS_2701_2703', near='cs2701', far='cs2703', exchange='DCE',
                combo='SP cs2701&cs2703', max_open_lots=3, margin_per_lot=2500.0,
                signal_current=True, quotes_current=True, session_open=True,
                expected_near=5.0, expected_far=-5.0, known_orders=frozenset())
    base.update(kw)
    return ScopedLimits(**base)


LONG_SPREAD = (_pos('cs2701', '2', 5), _pos('cs2703', '3', 5))


class ScopedEvidenceTests(unittest.TestCase):
    def test_take_profit_with_yesterday_position_passes(self):
        """昨日开的多价差（TodayPosition=0）也能挂止盈：这是与 held_check 的关键区别。"""
        ev = scoped_evidence(_intent(), _snapshot(positions=LONG_SPREAD), None, _limits())

        self.assertEqual(ev.free_close_lots, 5)
        self.assertEqual(ev.free_open_lots, 0)
        self.assertFalse(ev.conflict)

    def test_other_spreads_in_same_account_do_not_block(self):
        extra = (_pos('a2701', '2', 3), _pos('a2703', '3', 3), _pos('PK701', '2', 1))
        ev = scoped_evidence(_intent(), _snapshot(positions=LONG_SPREAD + extra), None, _limits())

        self.assertEqual(ev.free_close_lots, 5)

    def test_legs_must_match_local_book(self):
        limits = _limits(expected_near=4.0)
        with self.assertRaisesRegex(GateDenied, '柜台两腿与本地账不符'):
            scoped_evidence(_intent(), _snapshot(positions=LONG_SPREAD), None, limits)

    def test_unknown_active_order_on_legs_is_advisory_only(self):
        """重启后 OMS 不认识历史遗留委托，不能因此让整个价差永久挂不出单。

        可平量已由柜台冻结量扣减，单腿/超量平仓仍会被交易所拒绝；这里只做提示。
        """
        snap = _snapshot(positions=LONG_SPREAD, orders=[_order('cs2701')])
        ev = scoped_evidence(_intent(), snap, None, _limits())

        self.assertEqual(ev.free_close_lots, 5)

    def test_known_active_order_is_tolerated(self):
        snap = _snapshot(positions=LONG_SPREAD, orders=[_order('cs2701')])
        limits = _limits(known_orders=frozenset({('1', '2', '3')}))
        ev = scoped_evidence(_intent(), snap, None, limits)
        self.assertEqual(ev.free_close_lots, 5)

    def test_order_on_other_spread_is_ignored(self):
        snap = _snapshot(positions=LONG_SPREAD, orders=[_order('a2701')])
        ev = scoped_evidence(_intent(), snap, None, _limits())
        self.assertEqual(ev.free_close_lots, 5)

    def test_frozen_lots_reduce_closable_amount(self):
        positions = (_pos('cs2701', '2', 5, short_frozen=4), _pos('cs2703', '3', 5))
        with self.assertRaisesRegex(GateDenied, '可平量不足'):
            scoped_evidence(_intent(lots=2), _snapshot(positions=positions), None, _limits())

    def test_open_requires_capacity_and_margin(self):
        with self.assertRaisesRegex(GateDenied, '可开额度不足'):
            scoped_evidence(_intent(purpose='open', side='buy'), _snapshot(positions=LONG_SPREAD),
                            None, _limits(max_open_lots=3))

        limits = _limits(max_open_lots=10, margin_per_lot=5000.0)
        ev = scoped_evidence(_intent(purpose='open', side='buy'), _snapshot(positions=LONG_SPREAD),
                             None, limits)
        self.assertEqual(ev.free_open_lots, 5)
        self.assertEqual(ev.extra_margin, 5000.0)

        with self.assertRaisesRegex(GateDenied, '可用资金不足'):
            scoped_evidence(_intent(purpose='open', side='buy'),
                            _snapshot(positions=LONG_SPREAD, cash=1000.0), None, limits)

    def test_signal_or_session_must_be_current(self):
        for field in ('signal_current', 'quotes_current', 'session_open'):
            with self.assertRaisesRegex(GateDenied, '信号、行情或交易时段'):
                scoped_evidence(_intent(), _snapshot(positions=LONG_SPREAD), None,
                                _limits(**{field: False}))

    def test_intent_strategy_must_match_limits(self):
        with self.assertRaisesRegex(GateDenied, '意图与价差配置不符'):
            scoped_evidence(_intent(strategy='A_2701_2703'),
                            _snapshot(positions=LONG_SPREAD, strategy='A_2701_2703'),
                            None, _limits())


if __name__ == '__main__':
    unittest.main()
