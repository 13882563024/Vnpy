"""真实回调适配入口 + 临时 SQLite；所有出站 CTP 调用均替身拦截。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from vnpy.trader.constant import Exchange
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map

from gateway.ctp_spread_gateway import SpreadCtpTdApi
from trading_core.journal import Outcome


class GatewayJournalTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'facts.db'
        contracts = patch.dict(symbol_contract_map, dict.fromkeys(
            ('spread', 'near', 'far'), SimpleNamespace(exchange=Exchange.DCE)))
        contracts.start()
        self.addCleanup(contracts.stop)
        self.order = dict(InstrumentID='spread', ExchangeID='DCE', FrontID=1, SessionID=2,
                          OrderRef='3', OrderSysID=' SYS ', OrderStatus='3', OrderSubmitStatus='3',
                          Direction='0', CombOffsetFlag='00', LimitPrice=-14, VolumeTotalOriginal=2,
                          VolumeTraded=0, InsertDate='20261118', InsertTime='21:00:00',
                          OrderPriceType='2', TimeCondition='3', VolumeCondition='1', StatusMsg='')
        self.fill = dict(InstrumentID='near', ExchangeID='DCE', OrderSysID='SYS', TradeID=' 01 ',
                         Direction='0', OffsetFlag='0', TradeDate='20261118', TradeTime='21:00:01',
                         Price=4200, Volume=1)

    def start(self, account='A', path=None):
        api = SpreadCtpTdApi(Mock(gateway_name='CTP'), journal_path=path or self.path)
        def offline(*args):
            self.assertTrue(api.journal is not None or api.journal_error)
            api.brokerid, api.userid = 'B', account
        with patch.object(CtpTdApi, 'connect', side_effect=offline):
            api.connect('', account, '', 'B', '', '', False)
        api.contract_inited = True
        api.cabinet_trading_day = '2026-11-19'
        return api

    def query_both(self, api):
        with patch.object(api, 'reqQryOrder', return_value=0):
            api.query_orders()
        api.onRspQryOrder(self.order, {}, api._orders_query_reqid, True)
        with patch.object(api, 'reqQryTrade', return_value=0):
            api.query_trades()
        api.onRspQryTrade(self.fill, {}, api._trades_query_reqid+1, True)  # 旧包不能入账
        api.onRspQryTrade(self.fill, {}, api._trades_query_reqid, True)

    def test_restart_duplicate_rebuilds_cold_oms_once(self):
        api = self.start()
        api.onRtnOrder(self.order)
        api.onRtnTrade(self.fill)
        cold = self.start()
        self.query_both(cold)
        cold.onRtnOrder(self.order)
        cold.onRtnTrade(self.fill)
        cold.gateway.on_order.assert_called_once()
        cold.gateway.on_trade.assert_called_once()
        self.assertEqual([r.outcome for r in cold.journal.replay()],
                         [Outcome.NEW]*2 + [Outcome.DUPLICATE]*4)

    def test_cold_oms_retains_each_leg_with_same_exchange_trade_id(self):
        from vnpy.event import Event, EventEngine
        from vnpy.trader.engine import OmsEngine
        from vnpy.trader.event import EVENT_TRADE

        far = dict(self.fill, InstrumentID='far', Direction='1')
        api = self.start()
        self.query_both(api)
        api.onRtnTrade(far)
        cold = self.start()
        oms = OmsEngine(Mock(), EventEngine())
        cold.gateway.on_trade.side_effect = lambda t: oms.process_trade_event(Event(EVENT_TRADE, t))
        self.query_both(cold)
        cold.onRtnTrade(far)
        cold.onRtnTrade(dict(far, TradeID='01'))
        trades = oms.get_all_trades()
        self.assertEqual({t.symbol for t in trades}, {'near', 'far'})
        self.assertEqual({t.extra['ctp_trade_id'] for t in trades}, {'01'})
        self.assertEqual(len({t.vt_tradeid for t in trades}), 2)
        self.assertEqual(cold.gateway.on_trade.call_count, 2)

    def test_identity_partitions_and_order_transitions(self):
        api = self.start()
        api.onRtnOrder(self.order)
        api.onRtnOrder(dict(self.order, OrderStatus='1', VolumeTraded=1))
        api.onRtnTrade(self.fill)
        api.onRtnTrade(dict(self.fill, InstrumentID='far'))
        api.onRtnTrade(dict(self.fill, TradingDay='20261120', FrontID=1, SessionID=2, OrderRef='3'))
        other = self.start('C')
        other.onRtnOrder(self.order)
        other.onRtnTrade(self.fill)
        rows = list(other.journal.replay())
        self.assertEqual([r.outcome for r in rows], [Outcome.NEW]*7)
        self.assertEqual([r.trading_day for r in rows[:5]], ['20261119']*4 + ['20261120'])
        self.assertNotEqual(rows[0].account, rows[-1].account)

    def assert_only_cancel(self, api):
        with patch.object(api, 'reqOrderInsert') as insert, patch.object(api, 'reqOrderAction') as cancel:
            self.assertEqual(api.send_order(Mock()), '')
            insert.assert_not_called()
            api.cancel_order(SimpleNamespace(orderid='1_2_3', symbol='spread', exchange=Exchange.DCE))
            cancel.assert_called_once()

    def test_conflict_is_durable_and_never_forwarded(self):
        api = self.start()
        self.query_both(api)
        api.onRtnTrade(dict(self.fill, Price=4201))
        api.onRtnOrder(dict(self.order, LimitPrice=0))
        api.gateway.on_trade.assert_called_once()
        api.gateway.on_order.assert_called_once()
        self.assertEqual([r.outcome for r in api.journal.replay()][-2:], [Outcome.CONFLICT]*2)
        self.assert_only_cancel(api)
        self.assert_only_cancel(self.start())
        self.assertFalse(self.start('other').journal_error)

    def test_record_failure_latched_but_cancel_can_still_reach_api(self):
        api = self.start()
        with patch.object(api.journal, 'append', side_effect=OSError('test disk failure')):
            self.query_both(api)
        self.assertFalse(api.orders_snapshot_ok)
        api.gateway.on_order.assert_not_called()
        api.gateway.on_trade.assert_not_called()
        self.assertIn('test disk failure', api.journal_error)
        self.assert_only_cancel(api)
        self.assert_only_cancel(self.start(path=self.path/'not-a-dir'))

    def test_missing_day_or_account_mismatch_is_not_guessed(self):
        for row, day in [(self.order, ''), (dict(self.order, InvestorID='WRONG'), '2026-11-19')]:
            api = self.start()
            api.cabinet_trading_day = day
            api.onRtnOrder(row)
            api.gateway.on_order.assert_not_called()
            self.assertTrue(api.journal_error)
            self.assertEqual(list(api.journal.replay()), [])

    def test_unparseable_order_kept_as_evidence_not_successful_snapshot(self):
        api = self.start()
        with patch.object(api, 'reqQryOrder', return_value=0):
            api.query_orders()
        api.onRspQryOrder(dict(self.order, OrderStatus='unknown'), {}, api._orders_query_reqid, True)
        self.assertFalse(api.orders_snapshot_ok)
        self.assertTrue(api.reconciliation_event_error)
        self.assertEqual([r.kind for r in api.journal.replay()], ['order'])

    def test_order_query_replaces_only_expired_request(self):
        api = self.start()
        with patch.object(api, 'reqQryOrder', return_value=0):
            api.query_orders()
            old = api._orders_query_reqid
            with self.assertRaisesRegex(RuntimeError, '尚未结束'):
                api.query_orders()
            api._orders_query_started_at -= api.POSITION_QUERY_TIMEOUT + 1
            api.query_orders()
        api.onRspQryOrder(self.order, {}, old, True)
        self.assertFalse(api.orders_snapshot_ok)
        self.assertTrue(api.orders_query_pending)
        api.onRspQryOrder({}, {}, api._orders_query_reqid, True)
        self.assertEqual(api.orders_snapshot, set())
        self.assertEqual(list(api.journal.replay()), [])

    def test_contract_buffer_replay_is_not_a_second_receipt(self):
        api = self.start()
        api.contract_inited = False
        api.onRtnOrder(self.order)
        api.onRtnTrade(self.fill)
        api.gateway.on_trade.assert_not_called()
        api.onRspQryInstrument({'ProductClass': 'untracked'}, {}, 1, True)
        api.gateway.on_trade.assert_called_once()
        api.gateway.on_order.assert_called_once()
        self.assertEqual([r.outcome for r in api.journal.replay()], [Outcome.NEW]*2)

    def test_unattributable_fill_recorded_before_projection(self):
        api = self.start()
        api.onRtnTrade(self.fill)
        api.gateway.on_trade.assert_not_called()
        self.assertEqual([r.kind for r in api.journal.replay()], ['fill'])
        self.query_both(api)
        api.gateway.on_trade.assert_called_once()
