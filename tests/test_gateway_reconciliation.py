"""柜台持仓快照只接受完整、同次请求的回报；无真实交易连接。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vnpy.trader.constant import Exchange
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map
from vnpy_ctp.api import THOST_FTDC_PD_Long, THOST_FTDC_PD_Short

from gateway.ctp_spread_gateway import PositionQueryError, SpreadCtpTdApi, SpreadCtpGateway


class PeriodicQueryTests(unittest.TestCase):
    def test_periodic_query_refreshes_positions_in_auto_mode(self):
        from vnpy.event import EventEngine
        events = EventEngine()
        gateway = SpreadCtpGateway(events, "CTP")
        gateway.init_query()
        self.assertEqual(
            gateway.query_functions, [gateway.query_account, gateway._auto_query_position])
        gateway.td_api.position_snapshot_at = 123.0
        with patch.object(gateway.td_api, "query_position") as query:
            gateway._auto_query_position()
            query.assert_called_once_with(mode="auto")
        # 后台刷新不能作废人工核对快照
        self.assertEqual(gateway.td_api.position_snapshot_at, 123.0)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        gateway = SimpleNamespace(gateway_name="CTP", write_log=Mock(), write_error=Mock(),
                                  on_position=Mock())
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

        orderid = "CTP.1_2_3"
        self.api._known_fields[orderid] = (Direction.LONG, Offset.CLOSE)
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
        self.api.gateway.on_order.assert_called_once()  # 补发全部成交，界面不再显示“未成交”
        self.assertEqual(order.status, Status.ALLTRADED)

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
