"""真实网关发送入口，模拟登录/柜台目录/核验收集端，不连接 CTP。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest
from vnpy_ctp.gateway.ctp_gateway import THOST_FTDC_PC_Combination, symbol_contract_map

from gateway.ctp_spread_gateway import SpreadCtpTdApi
from trading_core.ctp_facts import account_key
from trading_core.intents import OrderIntent
from trading_core.journal import Journal, Outcome
from trading_core.preflight import Evidence, Preflight


class SendBindingTests(TestCase):
    symbol = 'SP a2701&a2703'

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'facts.sqlite3'
        self.enterContext(patch.dict(symbol_contract_map, clear=True))
        self.intent = OrderIntent(account_key('B', 'A'), '20261119', 'strategy', 'signal-1',
                                  'tier-1', 'open', self.symbol, 'buy', 2, -5., 'v1', {'z': 2.1})
        self.login()

    def login(self):
        if hasattr(self, 'api'):
            self.api.close()  # 模拟旧进程已退出并释放 OS 写锁
        self.api = api = SpreadCtpTdApi(Mock(gateway_name='CTP'))
        self.addCleanup(api.close)
        api.journal = Journal(self.path)
        api.brokerid, api.userid = 'B', 'A'
        with patch.object(api, 'reqSettlementInfoConfirm', return_value=0):
            api.onRspUserLogin(dict(TradingDay='20261119', FrontID=8, SessionID=-2, MaxOrderRef=' 90 '),
                               {'ErrorID': 0}, 1, True)
        api.onRspQryInstrument(dict(ProductClass=THOST_FTDC_PC_Combination, InstrumentID=self.symbol,
                                    ExchangeID='DCE', InstrumentName='combo', VolumeMultiple=0,
                                    PriceTick=1, MinLimitOrderVolume=1, MaxLimitOrderVolume=100), {}, 2, True)

    def request(self, intent=None, *, seal=True, **changes):
        intent = intent or self.intent
        req = OrderRequest(**(dict(symbol=intent.symbol, exchange=Exchange.DCE, direction=Direction.LONG,
                                    offset=Offset.OPEN, price=intent.price, volume=intent.lots,
                                    type=OrderType.LIMIT, reference='CrossPeriod_strategy_open') | changes))
        round_ = self.api.preflight.begin(intent, '8/-2')
        ev = Evidence(round_, True, True, True, True, True, True, True, False, 2, 2, 100., 50.)
        req.extra = {'order_intent': intent, 'preflight_evidence': self.api.preflight.seal(ev) if seal else ev}
        return req

    def rows(self):
        return list(Journal.open_readonly(self.path).replay())

    def test_binding_is_committed_before_wire_and_restored_without_resend(self):
        def observe_wire(data, reqid):
            intent, binding = self.rows()
            self.assertEqual((intent.kind, intent.identity), ('order_intent', ('strategy', 'signal-1')))
            self.assertEqual((binding.kind, binding.account, binding.trading_day, binding.identity),
                             ('order_binding', self.intent.account, self.intent.day, ('8', '-2', data['OrderRef'])))
            self.assertEqual(binding.payload, dict(strategy='strategy', decision_id='signal-1', exchange='DCE'))
            return 0
        with patch.object(self.api, 'reqOrderInsert', side_effect=observe_wire) as send:
            self.assertEqual(self.api.send_order(self.request()), 'CTP.8_-2_91')
            self.assertFalse(self.api.send_order(self.request()))
            send.assert_called_once()
        self.login()
        with patch.object(self.api, 'reqOrderInsert', return_value=0) as send:
            self.assertFalse(self.api.send_order(self.request()))
            self.assertEqual(self.api.send_order(self.request(replace(self.intent, decision_id='next'))),
                             'CTP.8_-2_93')
            send.assert_called_once()
        self.assertEqual([r.outcome for r in self.rows()], [Outcome.NEW]*2 + [Outcome.DUPLICATE]*2 + [Outcome.NEW]*2)

    def test_intent_or_gate_mismatch_never_reserves_or_sends(self):
        with patch.object(self.api, 'reqOrderInsert') as send:
            for change in (dict(account='wrong'), dict(day='20261118'), dict(side='sell'), dict(price=0.5),
                           dict(purpose='risk_exit'), dict(purpose='unknown')):
                with self.subTest(change=change):
                    self.assertFalse(self.api.send_order(self.request(replace(self.intent, **change))))
            for change in (dict(exchange=Exchange.SHFE), dict(symbol='a2701'), dict(volume=2.1),
                           dict(price=-4), dict(price=float('nan')), dict(type=OrderType.FAK),
                           dict(type=OrderType.FOK), dict(direction=Direction.NET)):
                with self.subTest(change=change):
                    self.assertFalse(self.api.send_order(self.request(**change)))
            for extra in ({}, {'order_intent': self.intent}, {'order_intent': 'bogus'},
                          {'order_intent': self.intent, 'preflight_evidence': {'ok': True}}):
                req = self.request()
                req.extra = extra
                self.assertFalse(self.api.send_order(req))
            self.assertFalse(self.api.send_order(self.request(seal=False)))
            self.api.preflight = Preflight(ttl_seconds=15, clock=lambda: self.now)
            self.now = 0
            req = self.request()
            self.now = 16
            self.assertFalse(self.api.send_order(req))
            self.api.cabinet_trading_day = ''
            self.assertFalse(self.api.send_order(self.request()))
            send.assert_not_called()
        self.assertFalse(self.rows())

    def test_nonpaper_gateway_denies_shadow_intent_even_with_sealed_evidence(self):
        with patch.object(self.api,'reqOrderInsert') as send:
            self.assertFalse(self.api.send_order(self.request(replace(self.intent,reason={'shadow_only':True}))))
            send.assert_not_called()
        self.assertEqual(self.rows(),[])

    def test_manual_and_realtime_activity_invalidate_outstanding_authorization(self):
        self.api.offset_flag_width = 2
        with patch.object(self.api, 'reqOrderInsert', return_value=0) as send:
            for event in ('order', 'fill', 'manual', 'disconnect'):
                with self.subTest(event=event):
                    req = self.request()
                    if event in ('order', 'fill'):
                        with patch.object(self.api, '_record', return_value=False):
                            (self.api.onRtnOrder if event == 'order' else self.api.onRtnTrade)({})
                    elif event == 'manual':
                        manual = self.request(symbol='a2701', reference='', offset=Offset.CLOSE)
                        self.assertFalse(self.api.send_order(manual))  # 不将手动单标为策略
                        manual.extra = {}
                        self.assertTrue(self.api.send_order(manual))
                        self.assertEqual(send.call_args.args[0]['CombOffsetFlag'], '1')
                    else:
                        self.api.onFrontDisconnected(1)
                    self.assertFalse(self.api.send_order(req))
            send.assert_called_once()
        self.assertEqual(self.rows(), [])

    def test_verified_combo_close_encodes_close_on_both_legs(self):
        self.api.offset_flag_width = 2
        with patch.object(self.api, 'reqOrderInsert', return_value=0) as send:
            req = self.request(replace(self.intent, purpose='take_profit'), offset=Offset.CLOSE)
            self.assertTrue(self.api.send_order(req))
            self.assertEqual(send.call_args.args[0]['CombOffsetFlag'], '11')

    def test_disk_failure_after_reserve_leaves_evidence_without_permitting_retry(self):
        append = self.api.journal.append
        def fail_binding(account, day, kind, identity, payload):
            if kind == 'order_binding':
                raise OSError('disk full')
            return append(account, day, kind, identity, payload)
        with patch.object(self.api.journal, 'append', side_effect=fail_binding), patch.object(self.api, 'reqOrderInsert') as send:
            self.assertFalse(self.api.send_order(self.request()))
            send.assert_not_called()
        self.assertIn('disk full', self.api.journal_error)
        self.login()
        with patch.object(self.api, 'reqOrderInsert') as send:
            self.assertFalse(self.api.send_order(self.request()))
            send.assert_not_called()
        self.assertEqual([(r.kind, r.outcome) for r in self.rows()], [('order_intent', Outcome.NEW),
                                                                     ('order_intent', Outcome.DUPLICATE)])

    def test_wire_failure_no_retry_and_changed_decision_conflict_preserves_cancel(self):
        with patch.object(self.api, 'reqOrderInsert', return_value=-1) as send:
            self.assertFalse(self.api.send_order(self.request()))
            self.assertFalse(self.api.send_order(self.request()))
            self.assertFalse(self.api.send_order(self.request(replace(self.intent, price=0.))))
            self.assertTrue(self.api.journal_error)
            send.assert_called_once()
        self.assertEqual(self.rows()[-1].outcome, Outcome.CONFLICT)
        with patch.object(self.api, 'reqOrderAction', return_value=0) as cancel:
            self.api.cancel_order(SimpleNamespace(orderid='8_-2_91', symbol=self.symbol, exchange=Exchange.DCE))
            cancel.assert_called_once()

    def test_local_identity_collision_freezes_even_for_distinct_decisions(self):
        with patch.object(self.api, 'reqOrderInsert', return_value=0) as send:
            self.assertTrue(self.api.send_order(self.request()))
            self.api.order_ref = 90  # 注入本地序号故障
            self.assertFalse(self.api.send_order(self.request(replace(self.intent, decision_id='other'))))
            send.assert_called_once()
        self.assertEqual((self.rows()[-1].kind, self.rows()[-1].outcome), ('order_binding', Outcome.CONFLICT))
