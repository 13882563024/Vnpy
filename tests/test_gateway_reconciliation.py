"""柜台持仓快照只接受完整、同次请求的回报；无真实交易连接。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vnpy.trader.constant import Exchange, Status
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map
from vnpy_ctp.api import THOST_FTDC_PD_Long, THOST_FTDC_PD_Short

from gateway.ctp_spread_gateway import PositionQueryError, SpreadCtpTdApi, SpreadCtpGateway


class PeriodicQueryTests(unittest.TestCase):
    def setUp(self):
        from vnpy.event import EventEngine
        self.gateway = SpreadCtpGateway(EventEngine(), "CTP")
        self.api = self.gateway.td_api
        self.api.login_status = self.api.contract_inited = True
        self.api.cabinet_trading_day = '2026-11-19'
        self.now = 100.0
        clock = patch('gateway.ctp_spread_gateway.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.account = Mock(return_value=0)
        self.position = Mock(return_value=0)
        for name, mock in [('reqQryTradingAccount', self.account), ('reqQryInvestorPosition', self.position)]:
            stub = patch.object(self.api, name, mock)
            stub.start()
            self.addCleanup(stub.stop)
        contracts = patch.dict(symbol_contract_map, {'a2701': object()})
        contracts.start()
        self.addCleanup(contracts.stop)

    def tick(self, seconds=0):
        self.now += seconds
        self.gateway.process_timer_event(None)

    def complete_account(self):
        self.api.onRspQryTradingAccount({}, {}, self.api._account_query_reqid, True)

    def complete_position(self):
        self.api.onRspQryInvestorPosition({}, {}, self.api._position_query_reqid, True)

    def test_account_and_position_have_different_periods(self):
        self.api.position_snapshot_at = 123.0
        self.tick()
        self.account.assert_called_once()
        self.position.assert_not_called()
        self.complete_account()
        self.tick()
        self.position.assert_not_called()  # shared request spacing
        self.tick(1)
        self.position.assert_called_once()
        self.assertEqual(self.api._position_query_mode, 'auto')
        self.complete_position()
        self.assertEqual(self.api.position_snapshot_at, 123.0)
        self.tick(9)
        self.position.assert_called_once()
        self.tick(1)
        self.assertEqual(self.position.call_count, 2)
        self.complete_position()
        self.tick(3)
        self.account.assert_called_once()
        self.tick(1)
        self.assertEqual(self.account.call_count, 2)

    def test_trade_refresh_coalesces_and_retries_a_stale_response(self):
        self.api._last_account_poll = self.api._last_position_poll = self.now
        for tradeid in ('T1', 'T2'):
            self._trade(tradeid)
        self.tick(1)
        self.position.assert_not_called()
        self.tick(1)
        self.position.assert_called_once()
        # Another fill in flight invalidates that response; the refresh is not lost.
        self._trade('T3')
        self.complete_position()
        self.assertTrue(self.api._position_refresh_needed)
        self.tick(2)
        self.assertEqual(self.position.call_count, 2)
        self.complete_position()
        self.assertFalse(self.api._position_refresh_needed)
        self._trade('T3')  # replay of an existing execution does not request another refresh
        self.tick(2)
        self.assertEqual(self.position.call_count, 2)

    def _trade(self, tradeid):
        data = dict(InstrumentID='a2701', FrontID=1, SessionID=2, OrderRef='3',
                    Direction='0', OffsetFlag='0', TradeDate='20261119', TradeTime='10:00:00',
                    TradeID=tradeid, Price=4200, Volume=1)
        with patch.object(self.api, 'ensure_contract', return_value=SimpleNamespace(exchange=Exchange.DCE)):
            self.api.onRtnTrade(data)

    def test_preflight_takes_priority_over_background_refresh(self):
        with patch.object(self.api, 'query_collector', SimpleNamespace(active=True)), \
                patch.object(self.api, 'advance_order_check') as advance:
            self.tick()
            advance.assert_called_once()
        self.account.assert_not_called()
        self.position.assert_not_called()

    def test_does_not_poll_disconnected_gateway(self):
        self.api.login_status = False
        self.tick()
        self.account.assert_not_called()
        self.position.assert_not_called()


class SpreadOrderReportTests(unittest.TestCase):
    def setUp(self):
        self.gateway = Mock(gateway_name="CTP")
        self.api = SpreadCtpTdApi(self.gateway)
        self.api.contract_inited = True
        self.api.frontid, self.api.sessionid = 1, 2
        self.order = dict(InstrumentID="SP a2701&a2703", FrontID=1, SessionID=2,
                          OrderRef="3", OrderSysID="SYS", OrderStatus="3", OrderSubmitStatus="3",
                          Direction="0", CombOffsetFlag="00", LimitPrice=-14, VolumeTotalOriginal=2,
                          VolumeTraded=0, InsertDate="20261105", InsertTime="10:00:00",
                          OrderPriceType="2", TimeCondition="3", VolumeCondition="1", StatusMsg="")
        self.near = dict(InstrumentID="a2701", OrderSysID="SYS", TradeID="T1", Direction="0",
                         OffsetFlag="0", TradeDate="20261105", TradeTime="10:00:01", Price=4200, Volume=1)
        self.far = dict(self.near, InstrumentID="a2703", Direction="1", Price=4214)
        contract = SimpleNamespace(exchange=Exchange.DCE)
        contracts = patch.dict(symbol_contract_map, dict.fromkeys(
            (self.order["InstrumentID"], "a2701", "a2703"), contract))
        contracts.start()
        self.addCleanup(contracts.stop)

    def _query(self, row):
        with patch.object(self.api, "reqQryOrder", return_value=0):
            self.api.query_orders()
        self.api.onRspQryOrder(row, {}, self.api._orders_query_reqid, True)

    def test_two_legs_of_one_group_do_not_fill_two_group_order(self):
        self.api.onRtnOrder(self.order)
        for row in (self.near, self.near, self.far, self.far):
            self.api.onRtnTrade(row)
        self.assertEqual([call.args[0].symbol for call in self.gateway.on_trade.call_args_list],
                         ["a2701", "a2703"])
        self.gateway.on_order.assert_called_once()
        order = self.gateway.on_order.call_args.args[0]
        self.assertEqual((order.status, order.traded), (Status.NOTTRADED, 0))
        partial = dict(self.order, OrderStatus="1", VolumeTraded=1)
        self._query(partial)
        self.api.onRtnOrder(self.order)   # 旧未成不能覆盖部成
        self.api.onRtnOrder(partial)      # 查询与推送重复
        self.assertEqual(self.gateway.on_order.call_count, 2)
        order = self.gateway.on_order.call_args.args[0]
        self.assertEqual((order.status, order.traded), (Status.PARTTRADED, 1))

    def test_conflicting_duplicate_trade_latches_reconciliation(self):
        self.api.onRtnOrder(self.order)
        self.api.onRtnTrade(self.near)
        self.api.onRtnTrade(dict(self.near, Volume=2))
        self.gateway.on_trade.assert_called_once()
        self.assertIn("明细冲突", self.api.reconciliation_event_error)

    def test_cancel_then_late_query_and_leg_trade(self):
        cancel = dict(self.order, OrderStatus="5", VolumeTraded=1)
        self.api.onRtnOrder(cancel)
        self._query(self.order)
        self.api.onRtnOrder(cancel)
        self.api.onRtnTrade(self.far)
        self.api.onRtnTrade(self.far)
        self.gateway.on_order.assert_called_once()
        self.assertEqual(self.gateway.on_order.call_args.args[0].status, Status.CANCELLED)
        self.gateway.on_trade.assert_called_once()
        self.assertEqual(self.api.orders_snapshot, {"CTP.1_2_3"})

    def test_trade_before_order_then_authoritative_query(self):
        self.api.onRtnTrade(dict(self.near, FrontID=1, SessionID=2, OrderRef="3"))
        self.gateway.on_order.assert_not_called()
        self._query(dict(self.order, OrderStatus="0", VolumeTraded=2))
        self.api.onRtnOrder(self.order)
        self.api.onRtnTrade(self.near)
        self.api.onRtnTrade(self.far)
        self.gateway.on_order.assert_called_once()
        self.assertEqual(self.gateway.on_order.call_args.args[0].status, Status.ALLTRADED)
        self.assertEqual(self.gateway.on_trade.call_count, 2)

    def test_query_only_final_for_spread_missing_from_contract_directory(self):
        del symbol_contract_map[self.order["InstrumentID"]]
        self._query(dict(self.order, ExchangeID="DCE", OrderStatus="0", VolumeTraded=2))
        self.gateway.on_order.assert_called_once()
        order = self.gateway.on_order.call_args.args[0]
        self.assertEqual((order.exchange, order.status), (Exchange.DCE, Status.ALLTRADED))

    def test_leg_cannot_inherit_parent_direction(self):
        self.api.onRtnOrder(self.order)
        self.api.onRtnTrade(dict(self.far, Direction=""))
        self.gateway.on_trade.assert_not_called()
        self.assertTrue(self.api.reconciliation_event_error)

    def test_rejected_insert_not_revived_by_query(self):
        self.api.onRspOrderInsert(self.order, {"ErrorID": 1}, 1, True)
        self._query(self.order)
        self.gateway.on_order.assert_called_once()
        self.assertEqual(self.gateway.on_order.call_args.args[0].status, Status.REJECTED)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        gateway = SimpleNamespace(gateway_name="CTP", write_log=Mock(), write_error=Mock(),
                                  on_position=Mock(), on_account=Mock())
        self.api = SpreadCtpTdApi(gateway)
        self.api.brokerid = "broker"
        self.api.userid = "account"
        self.api.contract_inited = True
        self._contracts = patch.dict(symbol_contract_map, {"a2701": object(), "a2703": object()})
        self._contracts.start()
        self.addCleanup(self._contracts.stop)

    def _begin(self):
        with patch.object(self.api, "reqQryInvestorPosition", return_value=0):
            self.api.query_position()
        return self.api._position_query_reqid

    def _respond(self, data, reqid, last=True, error=None):
        with patch.object(CtpTdApi, "onRspQryInvestorPosition", lambda *_: None):
            self.api.onRspQryInvestorPosition(data, error or {}, reqid, last)

    def test_account_query_maps_margin_and_profit(self):
        """总表要的占用保证金/已实现/未实现，父类没映射，必须在这里补。"""
        self.api._account_query_reqid = 1
        self.api.onRspQryTradingAccount({
            "AccountID": "account", "Balance": 300000.0, "Available": 240000.0,
            "FrozenMargin": 1.0, "FrozenCash": 2.0, "FrozenCommission": 3.0,
            "CurrMargin": 60000.0, "CloseProfit": 1200.0, "PositionProfit": -300.0,
        }, {}, 1, True)

        account = self.api.gateway.on_account.call_args.args[0]
        self.assertEqual(account.curr_margin, 60000.0)
        self.assertEqual(account.close_profit, 1200.0)
        self.assertEqual(account.position_profit, -300.0)
        self.assertEqual(account.frozen, 6.0)
        self.assertEqual(account.available, 240000.0)
        self.assertIs(account.ctp_cache_token, self.api.account_cache_token)

    def test_contract_dir_late_reports_are_replayed_not_frozen(self):
        """登录时先到的委托回报只缓存；目录就绪后重放，并且不再永久冻结核对。

        实测：登录后 CTP 先推当日委托/成交，十几秒后才回合约目录。旧实现把
        ``reconciliation_event_error`` 置成"合约目录尚未完成时收到委托回报"后
        再也没有清过 —— 只要当天有单，之后每次核对都报"柜台回报待核实"。
        """
        self.api.contract_inited = False
        self.api.onRtnOrder({"InstrumentID": "a2701"})
        self.assertEqual(self.api.reconciliation_event_error, "合约目录尚未完成时收到委托回报")
        self.assertEqual(len(self.api.order_data), 1)

        self.api.contract_inited = True
        with patch.object(self.api, "onRtnOrder") as replay:
            self.api._replay_deferred_reports()

        replay.assert_called_once()
        self.assertEqual(self.api.reconciliation_event_error, "")
        self.assertEqual(list(self.api.order_data), [])

    def test_orders_snapshot_keeps_exchange_sysid(self):
        """当日委托快照要同时给出 OrderSysID → 本地单号：它是跨重启的稳定键。"""
        api = self.api
        api.orders_query_pending = True
        api._orders_query_reqid = 7
        order = SimpleNamespace(vt_orderid="CTP.1_2_3", is_active=lambda: True)
        api._orders_query_activity_generation = 17
        with patch.object(api, "_order_from_query", return_value=order), \
                patch.object(api, "_publish_order"):
            api.onRspQryOrder({"InstrumentID": "a2701", "FrontID": 1, "SessionID": 2,
                               "OrderRef": "3", "OrderSysID": " 12345 "}, {}, 7, True)

        self.assertTrue(api.orders_snapshot_ok)
        self.assertEqual(api.orders_snapshot_sysids, {"12345": "CTP.1_2_3"})
        self.assertIn("CTP.1_2_3", api.orders_snapshot)
        self.assertEqual(api.orders_snapshot_active, {"CTP.1_2_3": order})
        self.assertEqual(api.orders_snapshot_activity_generation, 17)

    def test_auto_query_updates_oms_without_publishing_snapshot(self):
        """后台刷新持仓要喂给 OMS，但不能改写人工核对快照。"""
        contract = SimpleNamespace(exchange=Exchange.DCE, size=10, pricetick=1)
        with patch.dict(symbol_contract_map, {"a2701": contract, "a2703": contract}):
            self.api.position_snapshot_at = 123.0
            self.api.position_snapshot = {"keep": 1.0}
            with patch.object(self.api, "reqQryInvestorPosition", return_value=0):
                self.api.query_position(mode="auto")
            reqid = self.api._position_query_reqid
            self.api.onRspQryInvestorPosition(
                {"InstrumentID": "a2701", "Position": 2, "TodayPosition": 2,
                 "PositionProfit": 0.0, "PositionCost": 40000.0,
                 "ShortFrozen": 0, "LongFrozen": 0,
                 "PosiDirection": THOST_FTDC_PD_Long}, {}, reqid, True)
        self.assertEqual(self.api.position_snapshot_at, 123.0)
        self.assertEqual(self.api.position_snapshot, {"keep": 1.0})
        self.api.gateway.on_position.assert_called()
        # 后台查询也要发布"视图快照"，否则总表在每次回报后永远显示未知。
        self.assertGreater(self.api.position_view_at, 0)
        self.assertIn("a2701", self.api.position_view_sides)

    def test_empty_last_reply_is_a_complete_empty_snapshot(self):
        reqid = self._begin()
        self._respond({}, reqid)
        self.assertFalse(self.api.position_query_pending)
        self.assertEqual(self.api.position_snapshot, {})
        self.assertGreater(self.api.position_snapshot_at, 0)
        self.assertEqual(self.api.position_query_generation, 1)

    def test_collects_rows_until_last_and_ignores_other_request(self):
        reqid = self._begin()
        self._respond({"InstrumentID": "a2701", "Position": 1,
                       "PosiDirection": THOST_FTDC_PD_Long}, reqid, False)
        self.assertEqual(self.api.position_snapshot_at, 0)
        self._respond({}, reqid + 1)
        self.assertTrue(self.api.position_query_pending)
        self._respond({"InstrumentID": "a2703", "Position": 2,
                       "PosiDirection": THOST_FTDC_PD_Short}, reqid)
        self.assertEqual(self.api.position_snapshot, {"a2701": 1, "a2703": 2})
        self.assertEqual(self.api.position_snapshot_sides["a2701"]["long"], 1)
        self.assertEqual(self.api.position_snapshot_sides["a2703"]["short"], 2)

    def test_error_or_order_during_query_invalidates_snapshot(self):
        reqid = self._begin()
        self._respond({}, reqid, error={"ErrorID": 1})
        self.assertEqual(self.api.position_snapshot_at, 0)
        self.assertTrue(self.api._position_query_failed)
        reqid = self._begin()
        self.api.contract_inited = False
        self.api.onRtnOrder({"InstrumentID": "a2701"})
        self._respond({}, reqid)
        self.assertEqual(self.api.position_snapshot_at, 0)
        self.assertEqual(self.api.position_query_generation, 0)

    def test_unknown_contract_does_not_void_the_whole_snapshot(self):
        reqid = self._begin()
        self._respond({"InstrumentID": "UNKNOWN", "Position": 1,
                       "PosiDirection": THOST_FTDC_PD_Long}, reqid, False)
        self._respond({"InstrumentID": "a2703", "Position": 2,
                       "PosiDirection": THOST_FTDC_PD_Short}, reqid)
        # 无关合约照常记入快照，但不重放给 OMS（合约未收录）。
        self.assertEqual(self.api.position_snapshot_at > 0, True)
        self.assertEqual(self.api.position_snapshot_sides["UNKNOWN"]["long"], 1)
        self.assertEqual(self.api.position_snapshot_sides["a2703"]["short"], 2)
        self.api.gateway.on_position.assert_not_called()

    def test_unknown_side_is_marked_unknown_for_that_symbol_only(self):
        reqid = self._begin()
        self._respond({"InstrumentID": "a2701", "Position": 1,
                       "PosiDirection": "UNKNOWN"}, reqid, False)
        self._respond({"InstrumentID": "a2703", "Position": 2,
                       "PosiDirection": THOST_FTDC_PD_Short}, reqid)
        self.assertGreater(self.api.position_snapshot_at, 0)
        self.assertEqual(self.api.position_snapshot_sides["a2701"]["unknown"], 1)
        self.assertEqual(self.api.position_snapshot_sides["a2703"]["short"], 2)

    def test_no_contract_directory_or_disconnection_blocks_snapshot(self):
        reqid = self._begin()
        self.api.contract_inited = False
        self._respond({}, reqid)
        self.assertEqual(self.api.position_snapshot_at, 0)
        self.api.contract_inited = True
        reqid = self._begin()
        with patch.object(CtpTdApi, "onFrontDisconnected", lambda *_: None):
            self.api.onFrontDisconnected(1)
        self._respond({}, reqid)
        self.assertEqual(self.api.position_snapshot_at, 0)

    def test_parent_empty_contract_directory_refuses_without_request(self):
        with patch.dict(symbol_contract_map, {}, clear=True):
            with patch.object(self.api, "reqQryInvestorPosition") as request:
                with self.assertRaisesRegex(RuntimeError, "合约目录"):
                    self.api.query_position()
            request.assert_not_called()
        self.assertFalse(self.api.position_query_pending)
        self.assertTrue(self.api._position_query_failed)
        self.assertEqual(self.api.position_snapshot_at, 0)

    def test_real_base_query_sends_request_and_accepts_empty_last(self):
        with patch.object(self.api, "reqQryInvestorPosition", return_value=0) as request:
            self.api.query_position()
        request.assert_called_once_with({"BrokerID": "broker", "InvestorID": "account"},
                                        self.api._position_query_reqid)
        self.api.onRspQryInvestorPosition({}, {}, self.api._position_query_reqid, True)
        self.assertFalse(self.api.position_query_pending)
        self.assertEqual(self.api.position_snapshot, {})
        self.assertGreater(self.api.position_snapshot_at, 0)

    def test_timeout_releases_pending_and_late_rows_never_reach_new_snapshot_or_oms(self):
        old_reqid = self._begin()
        self._respond({"InstrumentID": "a2701", "Position": 8,
                       "PosiDirection": THOST_FTDC_PD_Long}, old_reqid, False)
        with patch("gateway.ctp_spread_gateway.monotonic", return_value=self.api._position_query_started_at + 11):
            self.assertFalse(self.api.position_query_pending)
        self.assertTrue(self.api._position_query_failed)
        self.assertEqual(self.api.position_snapshot_at, 0)
        self._respond({"InstrumentID": "a2701", "Position": 8,
                       "PosiDirection": THOST_FTDC_PD_Long}, old_reqid)
        self.assertEqual(self.api.positions, {})
        new_reqid = self._begin()
        self.api.gateway.on_position.reset_mock()
        self._respond({"InstrumentID": "a2701", "Position": 5,
                       "PosiDirection": THOST_FTDC_PD_Short}, old_reqid)
        self.assertTrue(self.api.position_query_pending)
        self.assertEqual(self.api._position_query_rows, {})
        self.api.gateway.on_position.assert_not_called()
        self._respond({"InstrumentID": "a2703", "Position": 2,
                       "PosiDirection": THOST_FTDC_PD_Short}, new_reqid)
        self.assertEqual(self.api.position_snapshot, {"a2703": 2})
        self.assertEqual(self.api.position_query_generation, 1)
        self.assertEqual(self.api.positions, {})
        self.api.gateway.on_position.assert_not_called()
        self._respond({"InstrumentID": "a2701", "Position": 9,
                       "PosiDirection": THOST_FTDC_PD_Long}, old_reqid)
        self.assertEqual(self.api.position_snapshot, {"a2703": 2})
        self.api.gateway.on_position.assert_not_called()

    def test_unknown_order_direction_latches_manual_reconciliation(self):
        self.api.onRtnOrder({
            "InstrumentID": "a2701", "FrontID": 1, "SessionID": 2,
            "OrderRef": "3", "OrderStatus": "UNKNOWN",
        })
        # 上述状态未知早于方向校验：不能静默当无委托。
        self.assertTrue(self.api.reconciliation_event_error)

    def test_unknown_trade_direction_does_not_publish_as_long(self):
        self.api.sysid_orderid_map["SYS"] = "1_2_3"
        with patch.object(self.api.gateway, "on_trade", Mock(), create=True) as callback:
            self.api.onRtnTrade({"InstrumentID": "a2701", "OrderSysID": "SYS",
                                 "Direction": "UNKNOWN", "OffsetFlag": "0", "TradeID": "T1"})
            callback.assert_not_called()
        self.assertIn("方向或开平标志未知", self.api.reconciliation_event_error)

    def test_missing_flags_on_fill_use_cached_order_fields(self):
        from vnpy.trader.constant import Direction, Offset, Status
        from vnpy.trader.object import OrderData

        orderid = "1_2_3"
        order = OrderData(gateway_name="CTP", symbol="a2701", exchange=Exchange.DCE,
                          orderid="1_2_3", direction=Direction.LONG, offset=Offset.CLOSE,
                          price=-14, volume=1, traded=0, status=Status.NOTTRADED)
        self.api._order_cache[orderid] = order
        self.api.gateway.on_trade = Mock()
        self.api.gateway.on_order = Mock()
        self.api.sysid_orderid_map["SYS"] = orderid
        # 终态成交回报缺方向/开平标志，必须用缓存回填而不是丢弃
        with patch.dict(symbol_contract_map,
                        {"a2701": SimpleNamespace(exchange=Exchange.DCE)}):
            self.api.onRtnTrade({"InstrumentID": "a2701", "OrderSysID": "SYS",
                                 "TradeID": "T1", "TradeDate": "20261105",
                                 "TradeTime": "10:00:00", "Price": -14, "Volume": 1})
        self.api.gateway.on_trade.assert_called_once()
        trade = self.api.gateway.on_trade.call_args.args[0]
        self.assertEqual((trade.vt_orderid, trade.direction, trade.offset),
                         (order.vt_orderid, Direction.LONG, Offset.CLOSE))
        self.api.gateway.on_order.assert_not_called()
        self.assertEqual((order.status, order.traded), (Status.NOTTRADED, 0))

    def test_order_query_publishes_authoritative_final_status(self):
        from vnpy.trader.constant import Status

        self.api.gateway.on_order = Mock()
        self.api.contract_inited = True
        contract = SimpleNamespace(exchange=Exchange.DCE)
        with patch.dict(symbol_contract_map, {"SP a2701&a2703": contract}):
            with patch.object(self.api, "reqQryOrder", return_value=0) as request:
                self.api.query_orders()
            request.assert_called_once()
            reqid = self.api._orders_query_reqid
            self.api.onRspQryOrder({
                "InstrumentID": "SP a2701&a2703", "OrderStatus": "0",
                "Direction": "0", "CombOffsetFlag": "1",
                "FrontID": 9, "SessionID": 1, "OrderRef": 2,
                "LimitPrice": -14, "VolumeTotalOriginal": 1, "VolumeTraded": 1,
                "InsertDate": "20261105", "InsertTime": "10:00:00",
                "OrderPriceType": "2", "TimeCondition": "1", "VolumeCondition": "1",
            }, {}, reqid, True)
        self.api.gateway.on_order.assert_called_once()
        order = self.api.gateway.on_order.call_args.args[0]
        self.assertEqual(order.status, Status.ALLTRADED)  # 以柜台查询结果为准
        self.assertEqual(order.traded, 1)
        self.assertIn(order.vt_orderid, self.api.live_seen_orderids)

    def test_order_query_freezes_cabinet_snapshot_on_last_report(self):
        """查询完整结束才定格快照；本地据此判定“柜台已经没有的委托”是否失效。"""
        self.api.gateway.on_order = Mock()
        self.api.contract_inited = True
        contract = SimpleNamespace(exchange=Exchange.DCE)
        with patch.dict(symbol_contract_map, {"SP a2701&a2703": contract}):
            with patch.object(self.api, "reqQryOrder", return_value=0):
                self.api.query_orders()
            self.assertFalse(self.api.orders_snapshot_ok, "查询中不能有可用快照")
            reqid = self.api._orders_query_reqid
            self.api.onRspQryOrder({
                "InstrumentID": "SP a2701&a2703", "OrderStatus": "0",
                "Direction": "0", "CombOffsetFlag": "1",
                "FrontID": 9, "SessionID": 1, "OrderRef": 2,
                "LimitPrice": -14, "VolumeTotalOriginal": 1, "VolumeTraded": 0,
                "InsertDate": "20261105", "InsertTime": "10:00:00",
                "OrderPriceType": "2", "TimeCondition": "1", "VolumeCondition": "1",
            }, {}, reqid, False)
            self.assertFalse(self.api.orders_snapshot_ok, "未收到末条回报前不算完整")

            self.api.onRspQryOrder({}, {}, reqid, True)
        self.assertTrue(self.api.orders_snapshot_ok)
        self.assertEqual(self.api.orders_snapshot, {"CTP.9_1_2"})
        self.assertGreater(self.api.orders_snapshot_at, 0.0)

    def test_failed_order_query_invalidates_snapshot(self):
        """查询报错时快照必须作废：宁可不清本地记录，也不能按半份数据误删。"""
        self.api.contract_inited = True
        with patch.dict(symbol_contract_map,
                        {"SP a2701&a2703": SimpleNamespace(exchange=Exchange.DCE)}):
            with patch.object(self.api, "reqQryOrder", return_value=0):
                self.api.query_orders()
            reqid = self.api._orders_query_reqid
            self.api.onRspQryOrder({}, {"ErrorID": 7}, reqid, True)
        self.assertFalse(self.api.orders_snapshot_ok)
        self.assertEqual(self.api.orders_snapshot, set())

    def test_parent_accumulator_cleared_before_new_query(self):
        self.api.positions["stale"] = object()
        def query(_request, _reqid):
            self.assertEqual(self.api.positions, {})
            return 0
        with patch.object(self.api, "reqQryInvestorPosition", side_effect=query):
            self.api.query_position()
        self.assertTrue(self.api.position_query_pending)

    def test_gateway_rejection_releases_query_for_retry(self):
        with patch.object(self.api, "reqQryInvestorPosition", return_value=-2):
            with self.assertRaisesRegex(RuntimeError, "拒绝持仓查询"):
                self.api.query_position()
        self.assertFalse(self.api.position_query_pending)
        self.assertEqual(self.api.position_snapshot_at, 0)
        self._begin()
        self.assertTrue(self.api.position_query_pending)

    def test_flow_control_error_is_retryable_but_not_an_empty_position(self):
        for code in (-1, -2, -3):
            with patch.object(self.api, "reqQryInvestorPosition", return_value=code):
                with self.assertRaises(PositionQueryError) as caught:
                    self.api.query_position()
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.retryable, code)
            self.assertEqual(self.api.position_snapshot_at, 0)
            self.assertFalse(self.api.position_query_pending)
        with patch.object(self.api, "reqQryInvestorPosition", return_value=-4):
            with self.assertRaises(PositionQueryError) as caught:
                self.api.query_position()
        self.assertFalse(caught.exception.retryable)
        self.assertIn("-4", str(caught.exception))

    def test_only_complete_rows_publish_to_real_parent_oms(self):
        contract = SimpleNamespace(exchange=Exchange.DCE, size=10)
        with patch.dict(symbol_contract_map, {"a2701": contract, "a2703": contract}):
            reqid = self._begin()
            row = {"InstrumentID": "a2701", "PosiDirection": THOST_FTDC_PD_Long,
                   "Position": 2, "TodayPosition": 2, "YdPosition": 0,
                   "PositionProfit": 0, "PositionCost": 20,
                   "ShortFrozen": 0, "LongFrozen": 0}
            self.api.onRspQryInvestorPosition(row, {}, reqid, False)
            self.api.gateway.on_position.assert_not_called()
            self.assertEqual(self.api.positions, {})
            self.api.onRspQryInvestorPosition({}, {}, reqid, True)
            self.api.gateway.on_position.assert_called_once()
            self.assertEqual(self.api.gateway.on_position.call_args.args[0].volume, 2)
            self.assertEqual(self.api.positions, {})

    def test_empty_last_clears_stale_parent_position_without_oms_event(self):
        reqid = self._begin()
        self.api.positions["stale"] = object()
        self.api.onRspQryInvestorPosition({}, {}, reqid, True)
        self.assertEqual(self.api.positions, {})
        self.api.gateway.on_position.assert_not_called()
        self.assertGreater(self.api.position_snapshot_at, 0)

    def test_unrecognized_row_never_publishes_to_parent_oms(self):
        reqid = self._begin()
        self.api.onRspQryInvestorPosition({"InstrumentID": "UNKNOWN", "Position": 1,
            "PosiDirection": THOST_FTDC_PD_Long}, {}, reqid, True)
        self.api.gateway.on_position.assert_not_called()
        self.assertEqual(self.api.positions, {})
        # 快照仍然可用，但该合约不会被当作已确认的 OMS 持仓。
        self.assertGreater(self.api.position_snapshot_at, 0)
        self.assertEqual(self.api.position_snapshot, {})

    def test_incomplete_or_error_reply_never_publishes_to_parent_oms(self):
        contract = SimpleNamespace(exchange=Exchange.DCE, size=10)
        with patch.dict(symbol_contract_map, {"a2701": contract}):
            reqid = self._begin()
            self.api.onRspQryInvestorPosition({"InstrumentID": "a2701", "Position": 2,
                "PosiDirection": THOST_FTDC_PD_Long}, {}, reqid, False)
            self.api.onRspQryInvestorPosition({}, {"ErrorID": 1}, reqid, True)
            self.api.gateway.on_position.assert_not_called()
            self.assertEqual(self.api.positions, {})
            self.assertEqual(self.api.position_snapshot_at, 0)


if __name__ == "__main__":
    unittest.main()
