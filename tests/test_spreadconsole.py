"""价差管理 GUI 控制器的无柜台门禁与真实 Qt 生命周期测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from time import monotonic
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vnpy.trader.constant import Direction, Exchange, Offset, Status
from vnpy.trader.ui import QtCore, QtWidgets

from gateway.ctp_spread_gateway import PositionQueryError
from spreadconsole.control import SpreadTradingControl
from spreadconsole.ui.widget import ReconciliationDialog, SpreadConsoleWidget
from utils.managed_spreads import ManagedSpreadError, ManagedSpreadService


class MockStrategy:
    def __init__(self):
        self.strategy_name = "CrossPeriodMRStrategy_A_2701_2703"
        self.paused = False
        self.live_trading = False
        self.inited = True
        self.trading = True
        self.layers = []
        self.pause_count = 0
        self.resume_count = 0
        self.cfg_loads = 0
        self.executor = "two_leg"
        self.adopt_result = True
        self.direction = 0
        self.pause_order_reports = {}
        self.pause_trade_reports = {}
        self.reserved_exit_orders = {}
        self.reserved_open_orders = {}

    def adopt_legacy_position(self):
        if not self.adopt_result:
            return False
        self.direction = 1
        self.layers.append(SimpleNamespace(
            adopted=True, direction=1, volume=1, tier_id="tier-1",
            describe=lambda: "接管层"))
        return True

    def reconcile_legacy_position(self):
        """差额接管：测试替身里等同"补一层并返回补入手数"。"""
        if self.layers:
            return 0.0
        return 1.0 if self.adopt_legacy_position() else 0.0

    def _reserved_exit_active(self, layer):
        tiers = {getattr(item, "tier_id", "") for item in self.layers}
        return any((not tier or tier in tiers)
                   for tier in self.reserved_exit_orders.values())

    def pause_strategy(self):
        self.paused = True
        self.pause_count += 1

    def get_pause_state(self):
        return dict(active_algoids=[], orders={}, trades_by_order={},
                    spread_net_pos_now=0, spread_net_pos_at_pause=0,
                    reconciliation_required=self.paused)

    def resume_strategy(self, *, reconciled=False):
        self.resume_count += 1
        self.paused = not reconciled
        return reconciled

    def _load_static_config(self):
        self.cfg_loads += 1

    def _refresh_contract_metrics(self):
        pass

    def _rebuild_filters(self):
        pass

    def on_start(self):
        pass

    def on_init(self):
        pass


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = ManagedSpreadService(Path(self.temp.name) / "spreads.json")
        self.config, _ = self.service.add("DCE.SP a2701&a2703", today=datetime(2026, 11, 2).date())
        self.strategy = MockStrategy()
        self.strategy.algoids = set()
        self.strategy.pause_position_at = 0.0
        self.spread = SimpleNamespace(variable_symbols={"near": "a2701.DCE", "far": "a2703.DCE"})
        self.spread_engine = SimpleNamespace(
            strategy_engine=SimpleNamespace(
                strategies={self.strategy.strategy_name: self.strategy},
                remove_strategy=Mock(return_value=True),
            ),
            get_spread=lambda _: self.spread,
            start_strategy=Mock(),
            add_spread=Mock(),
            remove_spread=Mock(),
            write_log=Mock(),
        )
        self.gateway = SimpleNamespace(md_api=SimpleNamespace(login_status=True),
                                       td_api=SimpleNamespace(login_status=True))
        self.contracts = {symbol: SimpleNamespace(exchange=Exchange.DCE, size=10, pricetick=1)
                          for symbol in (*self.config.legs, self.config.exchange_spread_vt_symbol)}
        self.main = SimpleNamespace(get_gateway=lambda _: self.gateway,
            get_contract=self.contracts.get, get_all_active_orders=lambda: [],
            get_all_positions=lambda: [],
            get_tick=lambda _: SimpleNamespace(bid_price_1=100, ask_price_1=101, datetime=datetime.now()),
            subscribe=Mock(), spread_tick_recorder=None)
        self.control = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        self.control.confirm_reconciliation()

    def tearDown(self):
        self.temp.cleanup()

    def test_layers_match_net_accepts_real_open_layers(self):
        """2026-11-11 事故回归：真实开仓层 + 接管层混在一起，也算与净仓一致。

        旧判断 `_adopted_layers_ok` 要求所有层都是接管来的，导致"核对并纠正"对
        本地真实持仓永远报"需人工逐层核对"，柜台多出的手数一直没人管。
        """
        self.spread.net_pos = 2
        self.strategy.pause_trade_reports = {}
        self.strategy.layers = [
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=False),
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=True),
        ]

        self.assertTrue(self.control._layers_match_net(self.spread, self.strategy))
        self.assertFalse(self.control._adopted_layers_ok(
            self.config.name, self.spread, self.strategy))

    def test_layers_match_net_rejects_shortfall(self):
        self.spread.net_pos = 2
        self.strategy.pause_trade_reports = {}
        self.strategy.layers = [
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=False),
        ]

        self.assertFalse(self.control._layers_match_net(self.spread, self.strategy))

    def test_layers_match_net_rejects_pending_trades(self):
        self.spread.net_pos = 1
        self.strategy.pause_trade_reports = {"CTP.1": 1.0}
        self.strategy.layers = [
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=True),
        ]

        self.assertFalse(self.control._layers_match_net(self.spread, self.strategy))

    # ------------------------------------------------ 隔夜止盈委托不绑定（2026-11-17）
    def _exit_order(self, orderid: str, moment):
        from vnpy.trader.constant import Direction, Offset

        return SimpleNamespace(
            vt_orderid=orderid,
            vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, datetime=moment)

    def test_overnight_exit_orders_are_not_bound(self):
        """CTP 委托当日有效：跨日不重启时 OMS 里的昨日委托已失效，不能占住止盈位。"""
        from datetime import datetime, timedelta

        self.strategy.layers = [SimpleNamespace(
            tier_id="t1", direction=-1, volume=1)]
        self.strategy.reserved_exit_orders = {}
        stale = self._exit_order(
            "CTP.9_old", datetime.now().astimezone() - timedelta(days=7))
        self.main.get_all_active_orders = lambda: [stale]

        bound = self.control._bind_existing_exit_orders(
            self.config.name, self.config, self.strategy, self.spread)

        self.assertEqual(bound, 0)
        self.assertEqual(self.strategy.reserved_exit_orders, {})

    def test_same_day_exit_order_is_bound(self):
        from datetime import datetime

        self.strategy.layers = [SimpleNamespace(
            tier_id="t1", direction=-1, volume=1)]
        self.strategy.reserved_exit_orders = {}
        fresh = self._exit_order("CTP.9_new", datetime.now().astimezone())
        self.main.get_all_active_orders = lambda: [fresh]

        bound = self.control._bind_existing_exit_orders(
            self.config.name, self.config, self.strategy, self.spread)

        self.assertEqual(bound, 1)
        self.assertEqual(self.strategy.reserved_exit_orders, {"CTP.9_new": "t1"})

    def test_local_clock_timestamp_is_not_bound(self):
        """本机时钟写的时间（无时区）不能证明委托仍挂在柜台：不许占位。

        2026-11-17 实测：本机时钟比柜台慢一天，21:01 挂的单被恢复成 11-17 21:23，
        `trading_day_of` 算出来正好等于柜台交易日，于是隔夜失效单被当成有效止盈位，
        策略一张单都挂不出来。
        """
        self.strategy.layers = [SimpleNamespace(
            tier_id="t1", direction=-1, volume=1)]
        self.strategy.reserved_exit_orders = {}
        local = SimpleNamespace(
            vt_orderid="CTP.local", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, volume=1, traded=0,
            datetime=datetime.now())        # 无时区 = 本机时钟
        self.main.get_all_active_orders = lambda: [local]

        bound = self.control._bind_existing_exit_orders(
            self.config.name, self.config, self.strategy, self.spread)

        self.assertEqual(bound, 0)
        self.assertEqual(self.strategy.reserved_exit_orders, {})

    def test_current_trading_day_prefers_cabinet_value(self):
        """柜台 TradingDay 是权威交易日：认节假日，本地推算只跳周末。"""
        self.gateway.td_api.cabinet_trading_day = "2026-12-25"   # 圣诞假期
        self.assertEqual(self.control.cabinet_trading_day(), "2026-12-25")
        self.assertEqual(self.control.current_trading_day(), "2026-12-25")

    def test_current_trading_day_falls_back_to_local_calendar(self):
        from utils.calendar import trading_day_of

        self.gateway.td_api.cabinet_trading_day = ""
        moment = datetime(2026, 11, 16, 10, 0)
        self.assertEqual(self.control.cabinet_trading_day(), "")
        self.assertEqual(self.control.current_trading_day(moment),
                         trading_day_of(moment).isoformat())

    def test_cabinet_trading_day_ignores_malformed_value(self):
        self.gateway.td_api.cabinet_trading_day = "20261225"
        self.assertEqual(self.control.cabinet_trading_day(), "")

    def test_purge_clears_overnight_orders_but_keeps_today(self):
        """2026-11-17 事故：跨日不重启时引擎里还留着昨天的未成交委托。"""
        from datetime import datetime, timedelta

        from vnpy.trader.constant import Direction, Offset, OrderType, Status
        from vnpy.trader.object import OrderData

        def make(orderid: str, moment) -> OrderData:
            return OrderData(
                symbol="SP a2701&a2703", exchange=Exchange.DCE, orderid=orderid,
                direction=Direction.LONG, offset=Offset.CLOSE,
                type=OrderType.LIMIT, price=-14.0, volume=1.0,
                status=Status.NOTTRADED, datetime=moment, gateway_name="CTP")

        old = make("old", datetime.now() - timedelta(days=7))
        new = make("new", datetime.now().astimezone())
        self.main.orders = {old.vt_orderid: old, new.vt_orderid: new}
        self.main.trades = {}
        self.strategy.reserved_exit_orders = {"CTP.old": "t1"}

        result = self.control.purge_stale_caches()

        self.assertEqual(result["engine_orders"], 1)
        self.assertEqual(result["reserved"], 1)
        self.assertNotIn(old.vt_orderid, self.main.orders)
        self.assertIn(new.vt_orderid, self.main.orders, "当日委托不能被误清")
        self.assertEqual(self.strategy.reserved_exit_orders, {})

    def test_purge_reads_oms_engine_caches(self):
        """``orders`` / ``trades`` 在 OmsEngine 上：旧写法读 MainEngine 永远是 None，
        于是跨日清缓存一次都没真正执行过（2026-11-17 “隔夜委托 0 笔”）。"""
        from vnpy.trader.constant import OrderType, Status
        from vnpy.trader.object import OrderData

        def make(orderid: str, moment) -> OrderData:
            return OrderData(
                symbol="SP a2701&a2703", exchange=Exchange.DCE, orderid=orderid,
                direction=Direction.LONG, offset=Offset.CLOSE,
                type=OrderType.LIMIT, price=-14.0, volume=1.0,
                status=Status.NOTTRADED, datetime=moment, gateway_name="CTP")

        old = make("old", datetime.now() - timedelta(days=7))
        new = make("new", datetime.now().astimezone())
        oms = SimpleNamespace(orders={old.vt_orderid: old, new.vt_orderid: new}, trades={})
        self.main.get_engine = lambda name: oms if name == "oms" else None

        result = self.control.purge_stale_caches()

        self.assertEqual(result["engine_orders"], 1)
        self.assertNotIn(old.vt_orderid, oms.orders)
        self.assertIn(new.vt_orderid, oms.orders, "当日委托不能被误清")

    def test_purge_drops_overnight_algo_slots(self):
        """跨日遗留的算法占位会让核对永远报“旧算法仍在途或状态未知”。"""
        current = self.control.current_trading_day()
        old = SimpleNamespace(orders={"CTP.1": SimpleNamespace(
            datetime=datetime.now() - timedelta(days=1))})
        fresh = SimpleNamespace(orders={"CTP.2": SimpleNamespace(datetime=datetime.now())})
        self.spread_engine.algo_engine = SimpleNamespace(
            algos={"old": old, "fresh": fresh})
        self.strategy.paused = True

        self.strategy.algoids = {"old", "fresh"}
        dropped = self.control._archive_stale_algo_slots(self.strategy, current)
        self.assertEqual(dropped, 1)
        self.assertEqual(self.strategy.algoids, {"fresh"}, "当日算法必须保留")

        self.strategy.algoids = {"old"}
        self.assertEqual(self.control._archive_stale_algo_slots(self.strategy, current), 1)
        self.assertEqual(self.strategy.algoids, set())

    def test_cabinet_covers_counts_pairable_legs_not_their_sum(self):
        """可平量按两腿可配对数量取 min：旧写法把两腿相加，等于放大一倍。"""
        self.main.get_all_positions = lambda: [
            SimpleNamespace(vt_symbol=self.config.legs[0],
                            direction=Direction.SHORT, volume=5),
            SimpleNamespace(vt_symbol=self.config.legs[1],
                            direction=Direction.LONG, volume=5),
        ]

        self.assertTrue(self.control._cabinet_covers(self.config.name, -1, 5))
        self.assertFalse(self.control._cabinet_covers(self.config.name, -1, 6))
        self.assertFalse(self.control._cabinet_covers(self.config.name, 1, 1))

    def test_cabinet_covers_subtracts_pending_close_orders(self):
        """柜台 5 手空、已有 1 手止盈在途 → 只能再平 4 手（2026-11-17 21:05 拒单 30）。"""
        self.main.get_all_positions = lambda: [
            SimpleNamespace(vt_symbol=self.config.legs[0],
                            direction=Direction.SHORT, volume=5),
            SimpleNamespace(vt_symbol=self.config.legs[1],
                            direction=Direction.LONG, volume=5),
        ]
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_orderid="CTP.9_46", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, volume=1, traded=0)]

        self.assertFalse(self.control._cabinet_covers(self.config.name, -1, 5))
        self.assertTrue(self.control._cabinet_covers(self.config.name, -1, 4))

    def test_layers_well_formed_rejects_merged_takeover_layer(self):
        """净仓对得上但层被合并（5 手并成 1 层）不算“一致”，必须重建。"""
        self.spread.net_pos = 5
        self.strategy.layer_volume = 1
        self.strategy.layers = [SimpleNamespace(
            direction=1, volume=5, is_open=True, adopted=True,
            describe=lambda: "第1层 多5手")]

        self.assertFalse(self.control._layers_well_formed(self.spread, self.strategy))

        rebuilt = []

        def adopt():
            self.strategy.layers = [
                SimpleNamespace(direction=1, volume=1, is_open=True, adopted=True,
                                describe=lambda: "第N层 多1手")
                for _ in range(5)
            ]
            rebuilt.append(True)
            return True

        self.strategy.adopt_legacy_position = adopt
        self.assertTrue(self.control._normalize_adopted_layers(
            self.config.name, self.spread, self.strategy))
        self.assertEqual(rebuilt, [True])
        self.assertTrue(self.control._layers_well_formed(self.spread, self.strategy))

    def test_layers_well_formed_keeps_real_open_layers(self):
        """策略自己开的 3 层各 1 手就是规范结构，不能被重建。"""
        self.spread.net_pos = 3
        self.strategy.layer_volume = 1
        self.strategy.layers = [
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=False,
                            describe=lambda: "第N层 多1手")
            for _ in range(3)
        ]

        self.assertTrue(self.control._layers_well_formed(self.spread, self.strategy))
        self.assertFalse(self.control._normalize_adopted_layers(
            self.config.name, self.spread, self.strategy))
        self.assertEqual(len(self.strategy.layers), 3)

    def test_binds_only_orders_matching_latest_layer_volume(self):
        """只绑盯最新层的那张：剩余量大于最新层手数的单绑上去会平错量。"""
        self.strategy.layers = [
            SimpleNamespace(tier_id="t1", direction=-1, volume=1),
            SimpleNamespace(tier_id="t2", direction=-1, volume=1),
        ]
        self.strategy.reserved_exit_orders = {}
        big = SimpleNamespace(
            vt_orderid="CTP.big", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, volume=3, traded=0,
            datetime=datetime.now().astimezone())
        small = SimpleNamespace(
            vt_orderid="CTP.small", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, volume=1, traded=0,
            datetime=datetime.now().astimezone())
        self.main.get_all_active_orders = lambda: [big, small]

        bound = self.control._bind_existing_exit_orders(
            self.config.name, self.config, self.strategy, self.spread)

        self.assertEqual(bound, 1)
        self.assertEqual(self.strategy.reserved_exit_orders, {"CTP.small": "t2"})

    def test_prunes_local_orders_absent_from_cabinet_snapshot(self):
        """柜台当日委托快照里没有的本地记录 = 上一交易日失效单：清掉并释放止盈位。"""
        self.strategy.reserved_exit_orders = {"CTP.old": "t1"}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.old", "vt_symbol": self.config.exchange_spread_vt_symbol,
            "direction": "空", "offset": "平", "volume": 3, "traded": 0,
            "price": 13, "status": "未成交",
            "datetime": datetime.now().astimezone().isoformat()}]}
        self.control._recovered_orderids = {"CTP.old", "CTP.keep"}
        self.gateway.td_api.orders_snapshot_ok = True
        self.gateway.td_api.orders_snapshot_at = monotonic()
        self.gateway.td_api.orders_snapshot = {"CTP.keep"}
        oms = SimpleNamespace(orders={}, active_orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None

        dropped = self.control._prune_orders_absent_from_cabinet(
            self.config.name, self.config)

        self.assertEqual(dropped, 1)
        self.assertNotIn("CTP.old", self.control._recovered_orderids)
        self.assertIn("CTP.keep", self.control._recovered_orderids)
        self.assertNotIn("CTP.old", self.strategy.reserved_exit_orders,
                         "失效单必须释放止盈位，否则策略永远不补挂")
        self.assertNotIn("A_2701_2703", self.control._saved_orders)

    def test_keeps_everything_without_cabinet_order_snapshot(self):
        """没有柜台委托快照（查询失败/未查）时一条都不清，避免误删真实在途单。"""
        self.control._recovered_orderids = {"CTP.old"}
        self.gateway.td_api.orders_snapshot_ok = False
        oms = SimpleNamespace(orders={}, active_orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None

        dropped = self.control._prune_orders_absent_from_cabinet(
            self.config.name, self.config)

        self.assertEqual(dropped, 0)
        self.assertIn("CTP.old", self.control._recovered_orderids)

    def test_stale_cabinet_order_snapshot_is_ignored(self):
        """快照过旧（超过 SNAPSHOT_MAX_AGE）不能拿来清本地记录。"""
        self.control._recovered_orderids = {"CTP.old"}
        self.gateway.td_api.orders_snapshot_ok = True
        self.gateway.td_api.orders_snapshot_at = monotonic() - 10_000
        self.gateway.td_api.orders_snapshot = set()
        oms = SimpleNamespace(orders={}, active_orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None

        dropped = self.control._prune_orders_absent_from_cabinet(
            self.config.name, self.config)

        self.assertEqual(dropped, 0)
        self.assertIn("CTP.old", self.control._recovered_orderids)

    def test_unreconciled_account_accepts_self_built_layers(self):
        """策略自己开的层（非接管）只要合计与柜台一致，就不能拦开启交易。

        2026-11-20 A/RM 实测：层账 1 层 1 手 vs 柜台 1 手完全一致，却因为
        ``_adopted_layers_ok`` 要求“每层都是接管来的”、``_can_adopt_legacy`` 要求
        “一层都没有”，两条都不满足而禁止开启交易。
        """
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 1, self.config.legs[1]: -1}
        self.spread.net_pos = 1
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        self.strategy.pause_trade_reports = {}
        self.strategy.layers = [SimpleNamespace(
            direction=1, volume=1, is_open=True, adopted=False, tier_id="t1")]

        self.assertEqual(
            self.control._unreconciled_account(self.config, allow_adopted=True), "")

    def test_unreconciled_account_blocks_mismatched_layers(self):
        """层账合计与柜台净仓不符时仍然必须拦（不能因为宽松而放过真问题）。"""
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 1, self.config.legs[1]: -1}
        self.spread.net_pos = 1
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        self.strategy.pause_trade_reports = {}
        self.strategy.layers = [
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=False,
                            tier_id="t1"),
            SimpleNamespace(direction=1, volume=1, is_open=True, adopted=False,
                            tier_id="t2"),
        ]

        self.assertIn(
            "逐层持仓尚未对账",
            self.control._unreconciled_account(self.config, allow_adopted=True))

    def test_stop_never_calls_engine_stop_or_cancel(self):
        self.control.enabled.add(self.config.name)
        self.control.master_running = True
        self.control.stop_all()
        self.assertTrue(self.strategy.paused)
        self.assertFalse(self.strategy.live_trading)
        self.assertFalse(self.control.master_running)
        self.assertEqual(self.strategy.pause_count, 2)  # 打开窗口、用户停止均调用非撤单暂停
        self.assertFalse(hasattr(self.spread_engine, "stop_strategy"))

    def test_config_summary_shows_all_key_fields(self):
        from utils.spread_config import SpreadConfig

        config = SpreadConfig(
            name="X", legs=["a2701.DCE", "a2703.DCE"], stop_loss=10000,
            margin_per_lot=5000,
            strategy={
                "mean_window_minutes": 5, "sd_window_minutes": 60,
                "entry_k": 1.5, "layer_step": 0.4, "max_positions": 4,
                "layer_volume": 2, "width_ticks": 2, "min_depth": 3,
                "vr_max": 10, "close_only": True, "exit_preplace": True,
                "market_close_timeout": 0, "entry_partial_timeout": 0,
                "max_hold_days": 999,
            },
        )
        text = SpreadConsoleWidget._describe_config(None, config)
        for part in ("N=5 分钟", "M=60 分钟", "X=1.5 sd", "层距=0.4 sd",
                     "层数≤4", "每层2手", "整仓止损=10000 元（2500 元/层）",
                     "盘口≤2 tick", "深度≥3", "量比≤10",
                     "保证金/手=5000 元/手（满层约 40000 元）",
                     "退出：只平仓", "预挂止盈=是",
                     "超时：平仓 不启用 / 开仓部分成交 不启用"):
            self.assertIn(part, text)

    def test_remove_contract_removes_engine_and_config(self):
        self.control.enabled.add(self.config.name)
        removed = self.control.remove(self.config.name)
        self.assertEqual(removed.name, self.config.name)
        self.assertIsNone(self.service.get(self.config.name))
        self.assertNotIn(self.config.name, self.control.enabled)
        self.spread_engine.strategy_engine.remove_strategy.assert_called_once_with(
            self.strategy.strategy_name)
        self.spread_engine.remove_spread.assert_called_once_with(self.config.name)
        self.assertFalse(self.strategy.trading)

    def test_remove_refuses_while_master_running(self):
        self.control.master_running = True
        with self.assertRaisesRegex(ManagedSpreadError, "先停止总交易"):
            self.control.remove(self.config.name)
        self.assertIsNotNone(self.service.get(self.config.name))

    def test_remove_refuses_exposure_or_unfinished_orders(self):
        self.main.get_all_positions = lambda: [
            SimpleNamespace(vt_symbol=self.config.legs[0], volume=1)]
        with self.assertRaisesRegex(ManagedSpreadError, "单腿持仓"):
            self.control.remove(self.config.name)

        self.main.get_all_positions = lambda: []
        self.strategy.get_pause_state = lambda: dict(
            active_algoids=[], orders={"CTP.1": {"status": "未成交"}},
            trades_by_order={}, spread_net_pos_now=0, spread_net_pos_at_pause=0)
        with self.assertRaisesRegex(ManagedSpreadError, "未终结委托"):
            self.control.remove(self.config.name)
        self.assertIsNotNone(self.service.get(self.config.name))

    def test_remove_with_active_order_requires_explicit_force(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol, vt_orderid="CTP.9")]
        with self.assertRaisesRegex(ManagedSpreadError, "在途委托"):
            self.control.remove(self.config.name)
        self.assertIsNotNone(self.service.get(self.config.name))
        removed = self.control.remove(self.config.name, force_exposure=True)
        self.assertEqual(removed.name, self.config.name)
        self.assertIsNone(self.service.get(self.config.name))
        self.spread_engine.write_log.assert_called()  # 留下“带着在途委托删除”的警告

    def test_remove_with_real_cabinet_position_requires_explicit_force(self):
        self._stale_spread_and_snapshot()
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        with self.assertRaisesRegex(ManagedSpreadError, "强制删除"):
            self.control.remove(self.config.name)
        self.assertIsNotNone(self.service.get(self.config.name))
        removed = self.control.remove(self.config.name, force_exposure=True)
        self.assertEqual(removed.name, self.config.name)
        self.assertIsNone(self.service.get(self.config.name))
        # 删除时同步清理持仓文件，避免下次启动把旧仓位读回来。
        self.spread_engine.data_engine.save_pos.assert_called_once()

    def test_remove_without_td_requires_offline_confirmation(self):
        self.gateway.td_api.login_status = False
        with self.assertRaisesRegex(ManagedSpreadError, "交易端未连接"):
            self.control.remove(self.config.name)
        self.control.remove(self.config.name, force_offline=True)
        self.assertIsNone(self.service.get(self.config.name))

    def test_contract_metadata_failure_keeps_trading_disabled(self):
        self.main.get_contract = lambda symbol: self.contracts.get(symbol) if symbol != self.config.exchange_spread_vt_symbol else None
        with self.assertRaisesRegex(ManagedSpreadError, "未提供交易所组合合约"):
            self.control.enable(self.config.name, True)
        self.assertNotIn(self.config.name, self.control.enabled)
        self.assertFalse(self.strategy.live_trading)

    def test_existing_orders_are_notes_not_exposure(self):
        self.main.get_contract = lambda _: None
        with self.assertRaises(ManagedSpreadError):
            self.control.enable(self.config.name, True)
        self.main.get_contract = self.contracts.get
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol, vt_orderid="CTP.1",
            direction=Direction.LONG, offset=Offset.OPEN, traded=0, volume=1,
            price=1, status=Status.NOTTRADED)]
        report = self.control.reconciliation_report()
        self.assertTrue(any("在途委托" in note for note in report["notes"]))
        self.assertFalse(any("在途委托" in item for item in report["exposures"]))
        # 有在途委托但引擎/柜台持仓一致 → 不算敞口，可以开启
        self.control.enable(self.config.name, True)
        self.assertIn(self.config.name, self.control.enabled)

    def test_allowed_contract_survives_restart_and_disable_persists(self):
        self.control.enable(self.config.name, True)
        self.assertTrue(self.service.get(self.config.name).trading_enabled)

        restored = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        self.assertIn(self.config.name, restored.enabled)

        restored.enable(self.config.name, False)
        self.assertNotIn(self.config.name, restored.enabled)
        self.assertFalse(self.service.get(self.config.name).trading_enabled)

    def test_enable_then_explicit_start(self):
        self.control.enable(self.config.name, True)
        self.assertFalse(self.strategy.live_trading)
        started = self.control.start_all()
        self.assertEqual(started, [self.config.name])
        self.assertTrue(self.control.master_running)
        self.assertTrue(self.strategy.live_trading)
        self.assertFalse(self.strategy.paused)
        self.assertEqual(self.strategy.executor, "exchange_spread")

    def test_reconciliation_verifies_every_contract_not_just_selected(self):
        second, _ = self.service.add(
            "DCE.SP a2703&a2705", today=datetime(2026, 11, 2).date())
        for symbol in (*second.legs, second.exchange_spread_vt_symbol):
            self.contracts[symbol] = SimpleNamespace(
                exchange=Exchange.DCE, size=10, pricetick=1)

        report = self.control.reconciliation_report()
        self.assertEqual(len(report["contracts"]), 2)
        self.assertTrue(all(item["verify"] == "已验证" for item in report["contracts"]))
        self.assertEqual(self.service.get(self.config.name).contract_status, "verified")
        self.assertEqual(self.service.get(second.name).contract_status, "verified")

    def test_bad_local_spread_blocks_only_its_own_reconciliation(self):
        self._stale_spread_and_snapshot()
        second, _ = self.service.add("DCE.SP j2701&j2703", today=datetime(2026, 11, 2).date())
        for symbol in (*second.legs, second.exchange_spread_vt_symbol):
            self.contracts[symbol] = SimpleNamespace(exchange=Exchange.DCE, size=10, pricetick=1)
        self.spread_engine.get_spread = lambda name: self.spread if name == self.config.name else None
        self.control.confirm_reconciliation()
        self.assertTrue(self.control.is_reconciled(self.config.name))
        self.assertFalse(self.control.is_reconciled(second.name))

    def test_reconciliation_does_not_fail_on_existing_exposure(self):
        self.control.invalidate_reconciliation()
        self.main.get_all_active_orders = lambda: [
            SimpleNamespace(
                vt_symbol=self.config.legs[0], vt_orderid="CTP.1",
                direction=Direction.LONG, offset=Offset.OPEN,
                traded=0, volume=2, price=4000, status=Status.NOTTRADED,
            )]
        report = self.control.reconciliation_report()
        self.assertTrue(report["ok"])
        self.assertTrue(any("在途委托" in note for note in report["notes"]))
        self.assertFalse(report["exposures"])  # 委托不算敞口
        self.assertIn("CTP.1", report["contracts"][0]["order_details"][0])

        self.control.confirm_reconciliation()
        self.assertTrue(self.control.reconciliation_confirmed)
        # 只有委托、没有持仓不一致 → 允许开启（委托本身不是敞口）
        self.control.validate(self.config.name)

    def test_clean_contract_can_enable_while_other_has_exposure(self):
        self.control.invalidate_reconciliation()
        second, _ = self.service.add(
            "DCE.SP j2701&j2703", today=datetime(2026, 11, 2).date())
        for symbol in (*second.legs, second.exchange_spread_vt_symbol):
            self.contracts[symbol] = SimpleNamespace(
                exchange=Exchange.DCE, size=10, pricetick=1)
        self.main.get_all_active_orders = lambda: [
            SimpleNamespace(
                vt_symbol=second.legs[0], vt_orderid="CTP.9",
                direction=Direction.LONG, offset=Offset.OPEN,
                traded=0, volume=2, price=4000, status=Status.NOTTRADED,
            )]

        self.control.confirm_reconciliation()
        self.control.enable(self.config.name, True)
        self.assertIn(self.config.name, self.control.enabled)

    def test_reconciliation_survives_ui_refresh(self):
        self.gateway.td_api.brokerid = "9999"
        self.gateway.td_api.userid = "tester"
        self.control.confirm_reconciliation()
        self.control.refresh()
        self.assertTrue(self.control.reconciliation_confirmed)
        self.assertIsNotNone(self.control.last_reconciliation_at)

    def _stale_spread_and_snapshot(self):
        """模拟柜台已空仓，但 vn.py 仍保存旧价差仓位。"""
        self.spread.leg_pos = {self.config.legs[0]: 1, self.config.legs[1]: -1}
        self.spread.net_pos = 1
        self.spread.calculate_pos = Mock(side_effect=lambda: setattr(
            self.spread, "net_pos", 1 if self.spread.leg_pos[self.config.legs[0]] else 0))
        self.spread_engine.data_engine = SimpleNamespace(save_pos=Mock())
        self.spread_engine.algo_engine = SimpleNamespace(algos={})
        self.gateway.td_api.brokerid = "broker"
        self.gateway.td_api.userid = "account"
        self.gateway.td_api.position_query_pending = False
        self.gateway.td_api._position_query_failed = False
        self.gateway.td_api.contract_inited = True
        self.gateway.td_api.position_query_generation = 1
        self.gateway.td_api.position_snapshot = {}
        self.gateway.td_api.position_snapshot_sides = {}
        self.gateway.td_api.position_snapshot_account = ("broker", "account")
        self.gateway.td_api.position_snapshot_at = monotonic()
        self.control.confirm_reconciliation()

    def test_reconciliation_fails_when_oms_read_fails(self):
        self.main.get_all_positions = Mock(side_effect=RuntimeError("OMS读取故障"))
        self.control.invalidate_reconciliation()
        with self.assertRaisesRegex(ManagedSpreadError, "持仓缓存读取失败"):
            self.control.confirm_reconciliation()
        self.assertFalse(self.control.reconciliation_confirmed)

    def test_validate_blocks_new_pause_fill_even_with_matching_positions(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.strategy.get_pause_state = lambda: dict(active_algoids=[], orders={},
            trades_by_order={"CTP.7": 1}, spread_net_pos_now=0, spread_net_pos_at_pause=0)
        with self.assertRaisesRegex(ManagedSpreadError, "暂停后有新成交"):
            self.control.enable(self.config.name, True)

    def test_repair_noop_reports_recheck_without_write(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        with self.assertRaisesRegex(ManagedSpreadError, "无须重复纠正"):
            self.control.repair_flat_spread(self.config.name)
        self.spread_engine.data_engine.save_pos.assert_not_called()

    def test_repair_clears_flat_book_and_keeps_reconciliation(self):
        self._stale_spread_and_snapshot()
        self.service.set_trading_enabled(self.config.name, True)
        self.control.enabled.add(self.config.name)
        result = self.control.repair_flat_spread(self.config.name)
        self.assertIn("已清零", result)
        self.assertEqual(self.spread.net_pos, 0)
        self.assertEqual(tuple(self.spread.leg_pos.values()), (0, 0))
        self.spread_engine.data_engine.save_pos.assert_called_once()
        # 柜台事实没变：核对确认与交易许可都保留，不必再核对一次。
        self.assertTrue(self.control.is_reconciled(self.config.name))
        self.assertTrue(self.service.get(self.config.name).trading_enabled)
        self.assertIn(self.config.name, self.control.enabled)
        self.assertFalse(self.control.master_running)
        self.control.start_all()
        self.assertTrue(self.control.master_running)

    def test_repair_archives_only_paused_algorithm_without_orders(self):
        self._stale_spread_and_snapshot()
        algo = SimpleNamespace(paused=True, status=Status.NOTTRADED, orders={},
                               is_order_finished=Mock(return_value=True))
        self.spread_engine.algo_engine.algos["old"] = algo
        self.strategy.algoids.add("old")
        self.strategy.get_pause_state = lambda: dict(active_algoids=["old"], orders={})
        self.control.repair_flat_spread(self.config.name)
        self.assertEqual(algo.status, Status.CANCELLED)
        self.assertNotIn("old", self.strategy.algoids)
        self.assertEqual(self.spread.net_pos, 0)

    def test_repair_rejects_algorithm_with_unfinished_order(self):
        self._stale_spread_and_snapshot()
        algo = SimpleNamespace(paused=True, status=Status.NOTTRADED, orders={},
                               is_order_finished=Mock(return_value=False))
        self.spread_engine.algo_engine.algos["old"] = algo
        self.strategy.get_pause_state = lambda: dict(active_algoids=["old"], orders={})
        with self.assertRaisesRegex(ManagedSpreadError, "旧算法仍在途"):
            self.control.repair_flat_spread(self.config.name)
        self.assertEqual(algo.status, Status.NOTTRADED)
        self.spread_engine.data_engine.save_pos.assert_not_called()

    def test_report_detects_local_missing_real_position(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        report = self.control.reconciliation_report()["contracts"][0]
        self.assertEqual([leg["difference"] for leg in report["leg_differences"]], [1, -1])
        self.assertTrue(any("本地少仓" in text for text in report["problems"]))
        with self.assertRaisesRegex(ManagedSpreadError, "本地与柜台持仓不一致"):
            self.control.enable(self.config.name, True)
        result = self.control.repair_flat_spread(self.config.name)
        self.assertIn("已按柜台仓位同步", result)
        self.assertEqual(self.spread.leg_pos[self.config.legs[0]], 1)
        self.assertEqual(self.spread.leg_pos[self.config.legs[1]], -1)
        self.assertTrue(self.strategy.layers and self.strategy.layers[0].adopted)
        self.spread_engine.data_engine.save_pos.assert_called_once()
        # 同步后本地与柜台一致，核对保留，可直接开启。
        self.control.enable(self.config.name, True)
        self.assertIn(self.config.name, self.control.enabled)

    def test_report_blocks_hedged_cabinet_position_even_when_net_zero(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 1},
        }
        report = self.control.reconciliation_report()["contracts"][0]
        self.assertTrue(any("净仓相同不代表无持仓" in text for text in report["problems"]))
        with self.assertRaisesRegex(ManagedSpreadError, "双向持仓"):
            self.control.enable(self.config.name, True)
        with self.assertRaisesRegex(ManagedSpreadError, "两腿仓位不匹配"):
            self.control.repair_flat_spread(self.config.name)

    def test_enable_requires_complete_confirmed_snapshot_when_gateway_supports_it(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_at = 0
        self.control._saved_reconciliation.clear()  # 模拟本地也没有存过核对
        with self.assertRaisesRegex(ManagedSpreadError, "尚未取得完整持仓快照"):
            self.control.enable(self.config.name, True)
        self.assertNotIn(self.config.name, self.control.enabled)

    def test_snapshot_stays_valid_while_user_reads_the_dialog(self):
        self._stale_spread_and_snapshot()
        # 用户看一会儿核对结果再点确认；5 分钟内仍然有效，不再因几十秒失效。
        stamp = monotonic() - 120
        self.gateway.td_api.position_snapshot_at = stamp
        self.control._reconciliation_snapshot_at = stamp
        self.control.confirm_reconciliation()
        self.assertTrue(self.control.reconciliation_confirmed)

    def test_enable_rejects_expired_snapshot_after_confirmation(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        expired = monotonic() - 601
        self.gateway.td_api.position_snapshot_at = expired
        self.control._reconciliation_snapshot_at = expired
        self.control._saved_reconciliation.clear()
        with self.assertRaisesRegex(ManagedSpreadError, "超过 5 分钟"):
            self.control.enable(self.config.name, True)

    def test_report_detects_local_excess_position(self):
        self._stale_spread_and_snapshot()
        report = self.control.reconciliation_report()["contracts"][0]
        self.assertEqual([leg["difference"] for leg in report["leg_differences"]], [-1, 1])
        self.assertTrue(any("本地多或方向不符仓" in text for text in report["problems"]))

    def test_repair_adopts_matched_real_cabinet_position(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        result = self.control.sync_spread_position(self.config.name)
        self.assertIn("已按柜台仓位同步", result)
        self.assertEqual(self.spread.net_pos, 1)

    def test_repair_reports_missing_cost_when_no_layer_price(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.strategy.adopt_result = False
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        with self.assertRaisesRegex(ManagedSpreadError, "缺少两腿持仓均价"):
            self.control.sync_spread_position(self.config.name)
        self.assertEqual(self.spread.net_pos, 0)  # 回滚，不留半截状态

    def test_missing_local_position_can_restart_after_manual_flatten(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        with self.assertRaisesRegex(ManagedSpreadError, "本地与柜台持仓不一致"):
            self.control.enable(self.config.name, True)
        # 用户在独立柜台处理真实仓位；本应用不发单，重启重建缓存并重新查询。
        self.gateway.td_api.position_snapshot_sides = {}
        self.gateway.td_api.position_query_generation += 1
        self.gateway.td_api.position_snapshot_at = monotonic()
        self.control.invalidate_reconciliation()
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.confirm_reconciliation()
        restarted.enable(self.config.name, True)
        self.assertIn(self.config.name, restarted.enabled)

    def test_saved_snapshot_cannot_authorize_restart(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.control.confirm_reconciliation()
        self.gateway.td_api.position_snapshot_at = 0  # 重启后内存快照没了
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.refresh()
        self.assertFalse(restarted.is_reconciled(self.config.name))
        with self.assertRaises(ManagedSpreadError):
            restarted.enable(self.config.name, True)
        self.assertIsNone(restarted._cabinet_leg_differences(self.config, self.spread))

    def test_restart_remembers_unfinished_orders(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol, vt_orderid="CTP.1",
            direction=Direction.LONG, offset=Offset.OPEN, traded=0, volume=2,
            price=4000, status=Status.NOTTRADED)]
        self.control.confirm_reconciliation()
        self.main.get_all_active_orders = lambda: []  # 重启后引擎不认这笔委托
        self.gateway.td_api.position_snapshot_at = 0
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        report = restarted.reconciliation_report()["contracts"][0]
        self.assertTrue(any("上次会话" in problem for problem in report["problems"]))
        self.assertTrue(any("CTP.1" in detail and "上次会话" in detail
                            for detail in report["pending_details"]))

    def test_force_enable_cannot_bypass_failed_checks(self):
        self._stale_spread_and_snapshot()
        with self.assertRaisesRegex(ManagedSpreadError, "本地与柜台持仓不一致"):
            self.control.enable(self.config.name, True)
        with self.assertRaisesRegex(ManagedSpreadError, "不允许强制"):
            self.control.enable(self.config.name, True, force=True)
        self.assertNotIn(self.config.name, self.control.enabled)

    def test_start_all_requires_fresh_quote_even_when_forced(self):
        self.control.enabled.add(self.config.name)
        stale = datetime.now() - timedelta(seconds=30)
        self.main.get_tick = lambda _: SimpleNamespace(
            bid_price_1=100, ask_price_1=101, datetime=stale)
        with self.assertRaisesRegex(ManagedSpreadError, "报价超过 5 秒"):
            self.control.start_all()
        self.assertFalse(self.control.master_running)
        # 无有效报价不能以人工强制操作绕过门禁。
        with self.assertRaisesRegex(ManagedSpreadError, "不允许强制"):
            self.control.start_all(force=True)
        self.assertFalse(self.control.master_running)

    def test_preopen_start_arms_without_sending_any_order(self):
        """盘前点「开始交易」：策略就绪但不放行实盘，等两腿报价到了再开。"""
        self.control.enabled.add(self.config.name)
        self.main.get_tick = lambda _: None          # 未开盘：没有盘口
        started = self.control.start_all(preopen=True)

        self.assertEqual(started, [])
        self.assertTrue(self.control.master_running)
        self.assertEqual(self.control.armed_names, [self.config.name])
        self.assertFalse(self.strategy.live_trading)   # 关键：没有放行实盘
        self.assertFalse(self.strategy.paused)         # 但已经恢复，不是暂停态
        self.assertEqual(self.strategy.resume_count, 1)
        # 默认行为不变：没有报价时普通启动仍然被拒。
        self.control.stop_all()
        self.control.confirm_reconciliation()
        with self.assertRaisesRegex(ManagedSpreadError, "无有效买一/卖一报价"):
            self.control.start_all()

    def test_preopen_releases_when_quotes_arrive(self):
        self.control.enabled.add(self.config.name)
        self.main.get_tick = lambda _: None
        self.control.start_all(preopen=True)
        self.assertFalse(self.strategy.live_trading)

        self.main.get_tick = lambda _: SimpleNamespace(
            bid_price_1=100, ask_price_1=101, datetime=datetime.now())
        released = self.control.tick_armed()

        self.assertEqual(released, [self.config.name])
        self.assertEqual(self.control.armed, {})
        self.assertTrue(self.strategy.live_trading)
        self.assertTrue(self.control.master_running)

    def test_preopen_times_out_and_stops(self):
        self.control.enabled.add(self.config.name)
        self.main.get_tick = lambda _: None
        self.control.start_all(preopen=True)
        self.control.armed[self.config.name] = monotonic() - 1     # 已过截止时间

        self.assertEqual(self.control.tick_armed(), [])

        self.assertFalse(self.control.master_running)
        self.assertFalse(self.strategy.live_trading)
        self.assertTrue(self.strategy.paused)

    def test_preopen_revoked_when_snapshot_expires(self):
        self.control.enabled.add(self.config.name)
        self.main.get_tick = lambda _: None
        self.control.start_all(preopen=True)
        td = self.gateway.td_api
        td.position_snapshot_sides = {}
        td.position_snapshot_account = ("", "")
        td.position_snapshot_at = monotonic() - 10_000

        self.control.tick_armed()

        self.assertFalse(self.control.master_running)
        self.assertEqual(self.control.armed, {})
        self.assertFalse(self.strategy.live_trading)

    def test_preopen_arm_does_not_survive_stop(self):
        self.control.enabled.add(self.config.name)
        self.main.get_tick = lambda _: None
        self.control.start_all(preopen=True)
        self.assertTrue(self.control.armed)

        self.control.stop_all()

        self.assertEqual(self.control.armed, {})
        self.assertEqual(self.control.armed_names, [])

    def test_force_start_cannot_skip_blocking_checks(self):
        self._stale_spread_and_snapshot()
        self.control.enabled.add(self.config.name)
        with self.assertRaisesRegex(ManagedSpreadError, "本地与柜台持仓不一致"):
            self.control.start_all()
        with self.assertRaisesRegex(ManagedSpreadError, "不允许强制"):
            self.control.start_all(force=True)
        self.assertFalse(self.control.master_running)

    def test_cold_start_does_not_resurrect_terminal_order(self):
        self.control._saved_orders = {self.config.name: [{'orderid':'CTP.1_2_3',
            'vt_symbol':self.config.legs[0], 'direction':'多', 'offset':'开',
            'volume':2, 'traded':0, 'price':4000, 'status':'未成交'}]}
        self.control._orders_restored = False
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={},
                              orders={'CTP.1_2_3':SimpleNamespace(status=Status.CANCELLED)})
        self.main.get_engine = lambda name: oms if name == 'oms' else None
        self.assertEqual(self.control.restore_orders(), 0)
        oms.process_order_event.assert_not_called()
        self.assertEqual(oms.active_orders, {})

    def test_restores_last_session_orders_into_oms_and_spread_map(self):
        self._stale_spread_and_snapshot()
        self.spread_engine.order_spread_map = {}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3",
            "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "开", "volume": 2, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        self.assertEqual(restarted.restore_orders(), 1)
        oms.process_order_event.assert_called_once()
        self.assertIn("CTP.1_2_3", restarted.spread_engine.order_spread_map)
        self.assertIn("CTP.1_2_3", self.strategy.pause_order_reports)
        algoid = "recovered_CTP.1_2_3"
        self.assertIn(algoid, self.spread_engine.algo_engine.algos)
        self.assertIn(algoid, self.strategy.algoids)
        algo = self.spread_engine.algo_engine.algos[algoid]
        self.assertTrue(algo.paused)          # 只跟踪，绝不发单
        self.assertTrue(algo.is_active())
        self.assertEqual(restarted.restore_orders(), 0)  # 幂等，不重复注入

    def test_clear_leftover_orders_removes_only_local_records(self):
        self._stale_spread_and_snapshot()
        self.spread_engine.order_spread_map = {}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3",
            "vt_symbol": self.config.exchange_spread_vt_symbol,
            "direction": "多", "offset": "开", "volume": 2, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(),
                              active_orders={"CTP.1_2_3": object()},
                              orders={"CTP.1_2_3": object()})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        self.assertIn("recovered_CTP.1_2_3", self.spread_engine.algo_engine.algos)
        self.assertEqual(restarted.clear_leftover_orders(), 1)
        self.assertEqual(oms.active_orders, {})
        self.assertEqual(restarted.spread_engine.order_spread_map, {})
        self.assertEqual(restarted._saved_orders, {})
        self.assertNotIn("recovered_CTP.1_2_3", self.spread_engine.algo_engine.algos)
        self.assertNotIn("recovered_CTP.1_2_3", self.strategy.algoids)

    def test_recovered_algo_drops_after_order_finishes(self):
        self._stale_spread_and_snapshot()
        self.spread_engine.order_spread_map = {}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "开", "volume": 2, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.main.get_order = lambda orderid: SimpleNamespace(is_active=lambda: False)
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        restarted._prune_finished_recovered(self.config.name)
        self.assertNotIn("recovered_CTP.1_2_3", self.spread_engine.algo_engine.algos)
        self.assertNotIn("recovered_CTP.1_2_3", self.strategy.algoids)
        self.assertEqual(self.strategy.pause_order_reports, {})

    def test_slot_lines_show_open_and_exit_occupancy(self):
        from spreadconsole.ui.widget import _slot_lines

        layer = SimpleNamespace(tier_id="tier-1", algoid="", direction=-1, volume=1)
        strategy = SimpleNamespace(
            layers=[layer], pending_open=None, pending_close=None,
            exit_algoid="", jobs={},
            reserved_open_orders={"CTP.8": ""},
            reserved_exit_orders={"CTP.9": "tier-1"},
        )

        def getter(orderid):
            if orderid == "CTP.8":
                return SimpleNamespace(direction=Direction.LONG, offset=Offset.OPEN,
                                       traded=0, volume=1, price=4000,
                                       status=Status.NOTTRADED)
            return SimpleNamespace(direction=Direction.SHORT, offset=Offset.CLOSE,
                                   traded=0, volume=1, price=-14,
                                   status=Status.NOTTRADED)

        lines = _slot_lines(strategy, getter)
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("开/加仓槽位：占用"))
        self.assertIn("多/开", text)          # 方向/开平
        self.assertIn("@4000", text)          # 报价
        self.assertIn("平仓槽位 第1层（空1手）：占用", text)
        self.assertIn("空/平", text)
        self.assertIn("@-14", text)

    def test_unowned_order_not_displayed_as_covering_each_layer(self):
        from spreadconsole.ui.widget import _slot_lines
        layers = [SimpleNamespace(tier_id=t, direction=1, volume=1) for t in ('L1','L2')]
        strategy = SimpleNamespace(layers=layers, reserved_exit_orders={'CTP.orphan':''})
        text = '\n'.join(_slot_lines(strategy))
        self.assertEqual(text.count('CTP.orphan'), 1)
        self.assertIn('非顶层，按策略不挂止盈', text)
        self.assertIn('未见止盈记录，请核对柜台', text)
        self.assertIn('未归属任何存续层', text)

    def test_slot_lines_show_pending_action_with_price(self):
        from spreadconsole.ui.widget import _slot_lines

        strategy = SimpleNamespace(
            layers=[], exit_algoid="", jobs={},
            pending_open=SimpleNamespace(direction=1, volume=2, price=-15),
            pending_close=None,
            reserved_open_orders={}, reserved_exit_orders={})
        lines = _slot_lines(strategy)
        self.assertIn("待发开仓 多2手@-15", "\n".join(lines))

    def test_slot_lines_show_idle_when_nothing_pending(self):
        from spreadconsole.ui.widget import _slot_lines

        strategy = SimpleNamespace(
            layers=[], pending_open=None, pending_close=None,
            exit_algoid="", jobs={}, reserved_open_orders={}, reserved_exit_orders={})
        lines = _slot_lines(strategy)
        self.assertEqual(lines[0], "开/加仓槽位：空闲")
        self.assertEqual(lines[1], "平仓槽位：无持仓层")

    def test_contract_page_lists_engine_and_saved_active_orders(self):
        from spreadconsole.ui.widget import _active_order_lines

        order = SimpleNamespace(
            vt_orderid="CTP.1", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.SHORT, offset=Offset.CLOSE, traded=0, volume=1,
            price=-14, status=Status.NOTTRADED)
        main = SimpleNamespace(get_all_active_orders=lambda: [order])
        saved = [
            {"orderid": "CTP.1", "vt_symbol": order.vt_symbol, "status": "未成交"},
            {"orderid": "CTP.2", "vt_symbol": "", "traded": 0, "volume": 1,
             "status": "未成交"},
        ]
        lines = _active_order_lines(main, self.config, saved)
        self.assertEqual(len(lines), 2)  # CTP.1 不重复，CTP.2 标为本地记录
        self.assertIn("CTP.1", lines[0])
        self.assertIn("空/平", lines[0])
        self.assertIn("CTP.2", lines[1])
        self.assertIn("本地记录", lines[1])
        self.assertEqual(_active_order_lines(SimpleNamespace(
            get_all_active_orders=lambda: []), self.config), [])

    def test_terminal_saved_orders_disappear_from_detail_list(self):
        from spreadconsole.ui.widget import _active_order_lines

        def getter(_orderid):
            return SimpleNamespace(is_active=lambda: False)

        main = SimpleNamespace(get_all_active_orders=lambda: [], get_order=getter)
        rows = [{"orderid": "CTP.1", "vt_symbol": self.config.exchange_spread_vt_symbol,
                 "traded": 1, "volume": 1, "status": "全部成交"}]
        self.assertEqual(_active_order_lines(main, self.config, rows), [])

    def test_sync_binds_saved_exit_order_before_engine_restores_it(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        self.main.get_all_active_orders = lambda: []  # 引擎尚未恢复该委托
        self.gateway.td_api.live_seen_orderids = {"CTP.9"}  # 本次登录收到过它的回报
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.9", "vt_symbol": self.config.exchange_spread_vt_symbol,
            "direction": "空", "offset": "平", "volume": 1, "traded": 0,
            "price": -12, "status": "未成交",
            "datetime": datetime.now().astimezone().isoformat()}]}
        self.control.sync_spread_position(self.config.name, force=True)
        self.assertIn("CTP.9", self.strategy.reserved_exit_orders)

    def test_sync_keeps_same_day_exit_order_across_restart(self):
        """2026-11-17 21:04 事故回归：21:01 挂出的止盈单跨进程重启后仍挂在柜台，
        不能被当成历史记录清掉——否则策略为整层重挂止盈，撞上柜台在途单被拒 30。"""
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.9_46", "vt_symbol": self.config.exchange_spread_vt_symbol,
            "direction": "空", "offset": "平", "volume": 1, "traded": 0,
            "price": -12, "status": "未成交",
            "datetime": datetime.now().astimezone().isoformat()}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.gateway.td_api.live_seen_orderids = set()   # 重启后 CTP 不会重推回报
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        result = restarted.sync_spread_position(self.config.name, force=True)

        self.assertIn("CTP.9_46", restarted._recovered_orderids)
        self.assertIn("CTP.9_46", self.strategy.reserved_exit_orders,
                      "当日委托必须占住止盈位，策略不能再挂第二张")
        self.assertNotIn("清理了", result)

    def test_sync_prunes_orders_without_live_reports(self):
        self._stale_spread_and_snapshot()
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "平", "volume": 1, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.gateway.td_api.live_seen_orderids = set()
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        self.assertIn("CTP.1_2_3", restarted._recovered_orderids)
        result = restarted.sync_spread_position(self.config.name, force=True)
        self.assertIn("清理了 1 条", result)     # 核对纠正时自动更新本地委托
        self.assertEqual(restarted._recovered_orderids, set())
        self.assertNotIn("CTP.1_2_3", oms.orders)

    def test_prune_keeps_order_matched_by_exchange_sysid(self):
        """重启后会话号变了，但交易所委托号不变：按 sysid 认定柜台仍在，不能清。"""
        td = self.gateway.td_api
        td.orders_snapshot_ok = True
        td.orders_snapshot_at = monotonic()
        td.orders_snapshot = {"CTP.9_111_5"}            # 会话号对不上（新进程）
        td.orders_snapshot_sysids = {"SYS-1": "CTP.1_2_3"}
        td.sysid_orderid_map = {"SYS-1": "1_2_3"}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "平", "volume": 1, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._recovered_orderids = {"CTP.1_2_3"}

        pruned = self.control._prune_orders_absent_from_cabinet(self.config.name, self.config)

        self.assertEqual(pruned, 0)
        self.assertIn("CTP.1_2_3", self.control._recovered_orderids)

    def test_prune_drops_order_absent_by_both_session_and_sysid(self):
        td = self.gateway.td_api
        td.orders_snapshot_ok = True
        td.orders_snapshot_at = monotonic()
        td.orders_snapshot = {"CTP.9_111_5"}
        td.orders_snapshot_sysids = {"SYS-9": "CTP.9_111_5"}
        td.sysid_orderid_map = {"SYS-9": "9_111_5"}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "平", "volume": 1, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._recovered_orderids = {"CTP.1_2_3"}

        pruned = self.control._prune_orders_absent_from_cabinet(self.config.name, self.config)

        self.assertEqual(pruned, 1)
        self.assertNotIn("CTP.1_2_3", self.control._recovered_orderids)

    def test_sync_prunes_close_order_with_same_direction_as_layer(self):
        """多/平 与当前多头层同向，不可能是平它的单，属于历史残留。"""
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        stale = SimpleNamespace(
            vt_orderid="CTP.9", vt_symbol=self.config.exchange_spread_vt_symbol,
            direction=Direction.LONG, offset=Offset.CLOSE, is_active=lambda: True)
        oms = SimpleNamespace(process_order_event=Mock(),
                              active_orders={"CTP.9": stale},
                              orders={"CTP.9": stale})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.main.get_all_active_orders = lambda: [stale]
        self.gateway.td_api.live_seen_orderids = {"CTP.9"}
        result = self.control.sync_spread_position(self.config.name, force=True)
        self.assertIn("清理了 1 条", result)
        self.assertNotIn("CTP.9", oms.orders)

    def test_sync_keeps_orders_seen_live_this_session(self):
        self._stale_spread_and_snapshot()
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "平", "volume": 1, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.gateway.td_api.live_seen_orderids = {"CTP.1_2_3"}
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        restarted.sync_spread_position(self.config.name, force=True)
        self.assertIn("CTP.1_2_3", restarted._recovered_orderids)  # 实时委托保留

    def test_sync_treats_missing_leg_keys_as_zero(self):
        """leg_pos 是 defaultdict：没有键就是 0，不应误报“两腿仓位不完整”。"""
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        result = self.control.sync_spread_position(self.config.name, force=True)
        self.assertIn("已按柜台仓位同步", result)
        self.assertEqual(self.spread.leg_pos, {self.config.legs[0]: 1.0,
                                               self.config.legs[1]: -1.0})

    def test_sync_all_processes_every_contract_in_one_call(self):
        self._stale_spread_and_snapshot()
        results = self.control.sync_all()
        self.assertEqual(len(results), 1)
        name, kind, _message = results[0]
        self.assertEqual(name, self.config.name)
        self.assertEqual(kind, "ok")
        self.assertEqual(self.spread.net_pos, 0)

    def test_sync_all_reports_skip_for_already_consistent_book(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        results = self.control.sync_all()
        self.assertEqual(results[0][1], "skip")

    def _running_repair(self):
        self._stale_spread_and_snapshot()
        self.control.master_running = True
        self.control.enabled.add(self.config.name)
        self.strategy.paused = False
        self.strategy.live_trading = True
        self.other = MockStrategy()
        self.other.live_trading = True
        self.spread_engine.strategy_engine.strategies['other'] = self.other

    def test_sync_all_repairs_only_target_and_restores_live_permission(self):
        self._running_repair()
        def check_saved():
            self.assertFalse(self.strategy.live_trading)
            self.assertTrue(self.strategy.paused)
            self.assertTrue(self.other.live_trading)
        self.spread_engine.data_engine.save_pos.side_effect = check_saved
        with patch.object(self.control, 'quote_problem', return_value=''):
            results = self.control.sync_all()
        self.assertEqual(results[0][1], 'ok', results)
        self.assertIn('已恢复交易', results[0][2])
        self.assertTrue(self.control.master_running)
        self.assertTrue(self.strategy.live_trading)
        self.assertEqual(self.other.pause_count, 0)

    def test_running_repair_leaves_target_paused_when_restart_guard_fails(self):
        self._running_repair()
        with patch.object(self.control, 'quote_problem', return_value='盘口过旧'):
            result = self.control.sync_spread_position(self.config.name)
        self.assertIn('该合约保持暂停：盘口过旧', result)
        self.assertTrue(self.strategy.paused)
        self.assertFalse(self.strategy.live_trading)
        self.assertTrue(self.other.live_trading)

    def test_running_repair_trims_three_real_layers_to_two(self):
        from execution.base import PositionLayer
        from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy

        self._running_repair()
        near, far = self.config.legs
        self.gateway.td_api.position_snapshot_sides = {
            'a2701': {'long': 2, 'short': 0}, 'a2703': {'long': 0, 'short': 2}}
        # The engine's leg positions are already current, but the layer book is not.
        self.spread.leg_pos = {near: 2, far: -2}
        self.spread.net_pos = 2
        self.spread.calculate_pos.side_effect = lambda: setattr(self.spread, 'net_pos', self.spread.leg_pos[near])
        self.strategy.layers = [PositionLayer(layer_index=i, volume=1, avg_spread=-i)
                                for i in (1, 2, 3)]
        self.strategy.closed_layers = []
        self.strategy.spread = self.spread
        self.strategy._snapshot = None
        self.strategy._refresh_variables = self.strategy._save_layers = self.strategy.write_log = Mock()
        self.strategy.trim_legacy_position = lambda: CrossPeriodMRStrategy.trim_legacy_position(self.strategy)
        with patch.object(self.control, 'quote_problem', return_value=''):
            self.control.confirm_reconciliation()
            result = self.control.sync_spread_position(self.config.name)
        self.assertIn('削减了 1 手', result)
        self.assertEqual([layer.avg_spread for layer in self.strategy.layers], [-1, -2])
        self.assertEqual(self.strategy.closed_layers[0].realized_pnl, 0)
        self.assertEqual(self.spread.leg_pos, {near: 2, far: -2})

    def test_running_repair_defers_while_live_algo_is_inflight(self):
        self._running_repair()
        self.spread_engine.algo_engine.algos['working'] = SimpleNamespace(is_order_finished=lambda: False)
        state = self.strategy.get_pause_state()
        state['active_algoids'] = ['working']
        with patch.object(self.strategy, 'get_pause_state', return_value=state):
            with self.assertRaisesRegex(ManagedSpreadError, '算法仍在途'):
                self.control.sync_spread_position(self.config.name, force=True)
        self.assertTrue(self.strategy.live_trading)
        self.assertFalse(self.strategy.paused)
        self.spread_engine.data_engine.save_pos.assert_not_called()

    def test_running_repair_does_not_pause_already_consistent_target(self):
        self._running_repair()
        for key in self.spread.leg_pos:
            self.spread.leg_pos[key] = 0
        self.spread.net_pos = 0
        pauses = self.strategy.pause_count
        self.assertEqual(self.control.sync_all()[0][1], 'skip')
        self.assertTrue(self.strategy.live_trading)
        self.assertEqual(self.strategy.pause_count, pauses)

    def test_clear_contract_orders_keeps_algorithm_owned_orders(self):
        oms = SimpleNamespace(
            orders={
                "CTP.1": SimpleNamespace(vt_symbol=self.config.exchange_spread_vt_symbol),
                "CTP.2": SimpleNamespace(vt_symbol=self.config.legs[0]),
                "CTP.3": SimpleNamespace(vt_symbol="other.DCE"),
            },
            active_orders={"CTP.1": object(), "CTP.2": object(), "CTP.3": object()},
        )
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.spread_engine.algo_engine = SimpleNamespace(algos={})
        self.spread_engine.algo_engine.algos["real"] = SimpleNamespace(
            orders={"CTP.2": object()})
        cleared = self.control.clear_contract_orders(self.config.name)
        self.assertEqual(cleared, 1)          # 只清没有算法持有的 CTP.1
        self.assertNotIn("CTP.1", oms.orders)
        self.assertIn("CTP.2", oms.orders)    # 真实算法持有 → 保留
        self.assertIn("CTP.3", oms.orders)    # 其它合约 → 不动

    def test_sync_binds_existing_open_order_into_open_slot(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol,
            vt_orderid="CTP.8", direction=Direction.LONG, offset=Offset.OPEN)]
        self.control.sync_spread_position(self.config.name, force=True)
        self.assertIn("CTP.8", self.strategy.reserved_open_orders)

    def test_sync_binds_existing_exit_order_into_layer_slot(self):
        self._stale_spread_and_snapshot()
        self.spread.leg_pos = {self.config.legs[0]: 0, self.config.legs[1]: 0}
        self.spread.net_pos = 0
        self.gateway.td_api.position_snapshot_sides = {
            "a2701": {"long": 1, "short": 0},
            "a2703": {"long": 0, "short": 1},
        }
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol,
            vt_orderid="CTP.9", direction=Direction.SHORT, offset=Offset.CLOSE,
            volume=1, traded=0, datetime=datetime.now().astimezone())]
        self.control.sync_spread_position(self.config.name, force=True)
        self.assertIn("CTP.9", self.strategy.reserved_exit_orders)
        layer = self.strategy.layers[0]
        self.assertEqual(self.strategy.reserved_exit_orders["CTP.9"], layer.tier_id)
        # 该层止盈位已被占用：策略侧不应再补挂
        self.assertTrue(self.strategy._reserved_exit_active(layer))

    def test_forced_sync_works_while_recovered_algo_is_active(self):
        self._stale_spread_and_snapshot()
        self.spread_engine.order_spread_map = {}
        self.control._saved_orders = {"A_2701_2703": [{
            "orderid": "CTP.1_2_3", "vt_symbol": self.config.legs[0],
            "direction": "多", "offset": "开", "volume": 2, "traded": 0,
            "price": 4000, "status": "未成交"}]}
        self.control._persist()
        oms = SimpleNamespace(process_order_event=Mock(), active_orders={}, orders={})
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.gateway.td_api.live_seen_orderids = {"CTP.1_2_3"}  # 实时委托，不会被自动清理
        restarted = SpreadTradingControl(
            self.main, self.spread_engine, self.service,
            state_path=Path(self.temp.name) / "state.json")
        restarted.restore_orders()
        self.strategy.get_pause_state = lambda: dict(
            active_algoids=list(self.strategy.algoids), orders={},
            trades_by_order={}, spread_net_pos_now=0, spread_net_pos_at_pause=0)
        with self.assertRaisesRegex(ManagedSpreadError, "强制纠正"):
            restarted.sync_spread_position(self.config.name)
        result = restarted.sync_spread_position(self.config.name, force=True)
        self.assertIn("已清零", result)
        self.assertEqual(self.spread.net_pos, 0)

    def test_repair_stale_oms_cache_no_longer_blocks_trading_forever(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_positions = lambda: [SimpleNamespace(
            vt_symbol=self.config.legs[0], volume=1, direction=Direction.LONG, price=100)]
        result = self.control.repair_flat_spread(self.config.name)
        self.assertIn("核对状态保留", result)
        self.assertEqual(self.spread.net_pos, 0)
        # 柜台快照为空仓时，以柜台为准，旧 OMS 缓存不再永久拦截。
        self.control.confirm_reconciliation()
        self.control.enable(self.config.name, True)
        self.assertIn(self.config.name, self.control.enabled)

    def test_sync_flat_caches_drops_stale_oms_positions_and_holdings(self):
        self._stale_spread_and_snapshot()
        oms = SimpleNamespace(
            positions={"a2701.DCE.LONG.CTP": SimpleNamespace(vt_symbol=self.config.legs[0]),
                       "other.DCE.LONG.CTP": SimpleNamespace(vt_symbol="other.DCE")},
            offset_converters={"CTP": SimpleNamespace(holdings={self.config.legs[0]: object(),
                                                               "other.DCE": object()})},
        )
        self.main.get_engine = lambda name: oms if name == "oms" else None
        self.control.repair_flat_spread(self.config.name)
        self.assertEqual(list(oms.positions), ["other.DCE.LONG.CTP"])
        self.assertEqual(list(oms.offset_converters["CTP"].holdings), ["other.DCE"])

    def test_repair_with_active_order_warns_then_force_syncs(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_active_orders = lambda: [SimpleNamespace(
            vt_symbol=self.config.exchange_spread_vt_symbol, vt_orderid="CTP.9")]
        with self.assertRaisesRegex(ManagedSpreadError, "在途委托"):
            self.control.repair_flat_spread(self.config.name)
        result = self.control.repair_flat_spread(self.config.name, force=True)
        self.assertIn("已清零", result)
        self.assertEqual(self.spread.net_pos, 0)
        self.spread_engine.data_engine.save_pos.assert_called_once()

    def test_repair_rejects_active_order_or_unknown_algo(self):
        self._stale_spread_and_snapshot()
        self.main.get_all_active_orders = lambda: [SimpleNamespace(vt_symbol=self.config.exchange_spread_vt_symbol)]
        with self.assertRaisesRegex(ManagedSpreadError, "在途委托"):
            self.control.repair_flat_spread(self.config.name)
        self.main.get_all_active_orders = lambda: []
        self.strategy.get_pause_state = lambda: dict(active_algoids=["old"], orders={})
        with self.assertRaisesRegex(ManagedSpreadError, "旧算法仍在途"):
            self.control.repair_flat_spread(self.config.name)

    def test_repair_archives_settled_trades_and_layers_when_cabinet_flat(self):
        self._stale_spread_and_snapshot()
        self.strategy.get_pause_state = lambda: dict(active_algoids=[], orders={},
                                                     trades_by_order={"CTP.1": 1})
        self.strategy.layers = [SimpleNamespace()]
        result = self.control.repair_flat_spread(self.config.name)
        self.assertIn("归档 1 层", result)
        self.assertEqual(self.strategy.layers, [])
        self.assertEqual(self.spread.net_pos, 0)

    def test_repair_save_failure_keeps_historical_layers(self):
        self._stale_spread_and_snapshot()
        historical_layer = SimpleNamespace()
        self.strategy.layers = [historical_layer]
        self.spread_engine.data_engine.save_pos.side_effect = OSError("disk")
        with self.assertRaisesRegex(ManagedSpreadError, "已回滚内存"):
            self.control.repair_flat_spread(self.config.name)
        self.assertEqual(self.strategy.layers, [historical_layer])

    def test_repair_works_after_a_new_position_query(self):
        self._stale_spread_and_snapshot()
        # 重新查询会推进 generation；只要快照完整，纠正不再要求“同一次确认”。
        self.gateway.td_api.position_query_generation += 1
        result = self.control.repair_flat_spread(self.config.name)
        self.assertIn("已清零", result)
        self.assertEqual(self.spread.net_pos, 0)

    def test_repair_rejects_incomplete_or_expired_snapshot(self):
        self._stale_spread_and_snapshot()
        self.gateway.td_api.position_query_pending = True
        with self.assertRaisesRegex(ManagedSpreadError, "持仓查询尚未完整返回"):
            self.control.repair_flat_spread(self.config.name)
        self.gateway.td_api.position_query_pending = False
        self.gateway.td_api.position_snapshot_at = monotonic() - 601
        with self.assertRaisesRegex(ManagedSpreadError, "超过 5 分钟"):
            self.control.repair_flat_spread(self.config.name)

    def test_repair_save_failure_restores_memory_and_blocks_trading(self):
        self._stale_spread_and_snapshot()
        self.spread_engine.data_engine.save_pos.side_effect = OSError("disk")
        with self.assertRaisesRegex(ManagedSpreadError, "已回滚内存"):
            self.control.repair_flat_spread(self.config.name)
        self.assertEqual(self.spread.net_pos, 1)
        self.assertEqual(self.spread.leg_pos[self.config.legs[0]], 1)
        self.assertFalse(self.service.get(self.config.name).trading_enabled)

    def test_reconciliation_warns_when_account_data_missing(self):
        self.control.invalidate_reconciliation()
        self.main.get_all_accounts = lambda: []
        report = self.control.reconciliation_report()
        self.assertTrue(report["ok"])
        self.assertTrue(any("账户信息尚未返回" in item for item in report["warnings"]))
        self.control.confirm_reconciliation()
        self.assertTrue(self.control.reconciliation_confirmed)

    def test_requires_explicit_account_reconciliation(self):
        self.control.invalidate_reconciliation()
        with self.assertRaisesRegex(ManagedSpreadError, "尚未人工核对"):
            self.control.enable(self.config.name, True)

    def test_persisted_spread_position_blocks_empty_account_cache(self):
        self.spread.net_pos = 1
        with self.assertRaisesRegex(ManagedSpreadError, "引擎记录存在"):
            self.control.enable(self.config.name, True)

    def test_enable_during_master_running_rejected(self):
        self.control.master_running = True
        with self.assertRaisesRegex(ManagedSpreadError, "先停止总交易"):
            self.control.enable(self.config.name, True)
        with self.assertRaisesRegex(ManagedSpreadError, "先停止总交易"):
            self.control.add("DCE.SP a2701&a2703")

    def test_paused_terminal_order_can_resume_after_manual_recheck(self):
        self.strategy.get_pause_state = lambda: dict(active_algoids=[],
            orders={"CTP.7": {"status": "已撤销"}}, trades_by_order={},
            spread_net_pos_now=0, spread_net_pos_at_pause=0)
        self.control.enabled.add(self.config.name)
        self.control.stop_all()
        # 模拟换了账号/清空本地记录的情形：没有任何可沿用的核对结果。
        self.control._saved_reconciliation.clear()
        with self.assertRaisesRegex(ManagedSpreadError, "尚未人工核对"):
            self.control.start_all()
        self.control.confirm_reconciliation()
        self.assertEqual(self.control.start_all(), [self.config.name])

    def test_failed_start_callback_rolls_back_live_state(self):
        self.control.enabled.add(self.config.name)
        self.strategy.trading = False
        self.strategy.on_start = Mock(side_effect=RuntimeError("启动回调异常"))
        with self.assertRaisesRegex(RuntimeError, "启动回调异常"):
            self.control.start_all()
        self.assertFalse(self.control.master_running)
        self.assertTrue(self.strategy.paused)
        self.assertFalse(self.strategy.live_trading)

    def test_failed_init_callback_rolls_back_live_state(self):
        self.control.enabled.add(self.config.name)
        self.strategy.inited = False
        self.strategy.on_init = Mock(side_effect=RuntimeError("初始化异常"))
        with self.assertRaisesRegex(RuntimeError, "初始化异常"):
            self.control.start_all()
        self.assertFalse(self.control.master_running)
        self.assertFalse(self.strategy.live_trading)

    def test_pause_reports_block_restart(self):
        self.control.enabled.add(self.config.name)
        self.strategy.get_pause_state = lambda: dict(active_algoids=[],
            orders={"CTP.7": {"status": "未成交"}}, trades_by_order={},
            spread_net_pos_now=0, spread_net_pos_at_pause=0)
        with self.assertRaisesRegex(ManagedSpreadError, "旧算法或委托仍在途"):
            self.control.start_all()
        self.assertTrue(self.strategy.paused)

    def test_refresh_does_not_repeat_subscriptions(self):
        self.control.refresh()
        self.control.refresh()
        self.assertEqual(self.main.subscribe.call_count, 2)
        self.gateway.md_api.login_status = False
        self.control.refresh()
        self.gateway.md_api.login_status = True
        self.control.refresh()
        self.assertEqual(self.main.subscribe.call_count, 4)


@unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen", "需显式 offscreen Qt")
class WidgetTests(unittest.TestCase):
    def _make_widget(self, master_running: bool):
        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        spread_engine = SimpleNamespace(strategy_engine=SimpleNamespace(strategies={}),
                                        get_spread=lambda _: None)
        engine = SimpleNamespace(get_engine=lambda _: spread_engine,
            get_gateway=lambda _: None, spread_tick_recorder=None)
        factory_patch = patch("spreadconsole.ui.widget.SpreadTradingControl")
        factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        control = factory.return_value
        control.service.list.return_value = {}
        control.master_running = master_running
        control.md_ready.return_value = True
        control.td_ready.return_value = True
        widget = SpreadConsoleWidget(engine, None)
        self.addCleanup(widget.close)
        return widget, control

    def test_selected_repair_starts_fresh_query_while_trading(self):
        widget, control = self._make_widget(master_running=True)
        def begin_query():
            widget._reconciliation_pending = True
        with patch.object(widget, '_name', return_value='X'), \
                patch.object(widget, '_confirm_reconciliation', side_effect=begin_query) as query, \
                patch.object(QtWidgets.QMessageBox, 'question', return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            widget._repair_selected_position()
        query.assert_called_once_with()
        self.assertEqual(widget._repair_target, 'X')
        control.sync_spread_position.assert_not_called()

    def test_master_start_auto_reconciles_without_asking(self):
        """点「开始交易」时没有有效核对：先自动核对并纠正，不弹核对确认窗口。"""
        widget, control = self._make_widget(master_running=False)
        control.enabled = {"A_2701_2703"}
        control.is_reconciled.return_value = False
        with patch.object(widget, "_confirm_reconciliation") as reconcile, \
                patch.object(widget, "_start_trading") as start:
            widget._toggle_master()

        reconcile.assert_called_once_with(batch=True)
        start.assert_not_called()
        self.assertTrue(widget._pending_start)
        self.assertTrue(widget._auto_mode)

    def test_batch_sync_success_starts_trading_without_second_click(self):
        widget, control = self._make_widget(master_running=False)
        widget._pending_start = True
        widget._auto_mode = True
        control.sync_all.return_value = [("A_2701_2703", "ok", "")]
        with patch.object(widget, "_start_trading") as start:
            widget._run_batch_sync([])

        start.assert_called_once()
        self.assertFalse(widget._pending_start)
        self.assertFalse(widget._auto_mode)

    def test_batch_sync_failure_does_not_autostart(self):
        widget, control = self._make_widget(master_running=False)
        control.enabled = {"A_2701_2703"}
        widget._pending_start = True
        widget._auto_mode = True
        control.sync_all.return_value = [("A_2701_2703", "fail", "柜台两腿仓位不匹配")]
        with patch.object(widget, "_start_trading") as start, \
                patch.object(QtWidgets.QMessageBox, "warning"):
            widget._run_batch_sync([])

        start.assert_not_called()
        self.assertFalse(widget._pending_start)

    def test_batch_sync_ignores_failure_of_contracts_not_enabled(self):
        """PK 这类没勾选交易的合约纠正失败，不该拦住其它合约启动。"""
        widget, control = self._make_widget(master_running=False)
        control.enabled = {"A_2701_2703"}
        widget._pending_start = True
        widget._auto_mode = True
        control.sync_all.return_value = [("PK_701_703", "fail", "柜台两腿仓位不匹配")]
        with patch.object(widget, "_start_trading") as start:
            widget._run_batch_sync([])

        start.assert_called_once()
        self.assertFalse(widget._pending_start)

    def test_close_window_keeps_strategy_running(self):
        """2026-11-11 反馈：叉掉价差窗口后策略要留在后台继续跑。"""
        widget, control = self._make_widget(master_running=True)

        widget.close()

        control.stop_all.assert_not_called()
        self.assertTrue(control.master_running)

    def test_reopen_shows_real_running_state(self):
        """重开窗口时按钮必须反映真实状态，不能回落成「开始交易」。"""
        widget, control = self._make_widget(master_running=True)

        widget.close()
        widget.master.setText("开始交易")     # 模拟界面初始文案
        widget.show()                         # 触发 showEvent → 立即刷新

        self.assertEqual(widget.master.text(), "停止交易")

    def test_widget_constructs_without_cabinet(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        spread_engine = SimpleNamespace(strategy_engine=SimpleNamespace(strategies={}),
                                        get_spread=lambda _: None)
        engine = SimpleNamespace(get_engine=lambda _: spread_engine,
            get_gateway=lambda _: None, spread_tick_recorder=None)
        with patch("spreadconsole.ui.widget.SpreadTradingControl") as factory:
            control = factory.return_value
            control.service.list.return_value = {}
            control.master_running = False
            control.md_ready.return_value = False
            control.td_ready.return_value = False
            widget = SpreadConsoleWidget(engine, None)
            self.assertEqual(widget.contracts.count(), 0)
            self.assertEqual(widget.master.text(), "开始交易")
            widget.close()

    def test_position_query_retries_flow_control_and_never_treats_it_as_flat(self):
        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        spread_engine = SimpleNamespace(strategy_engine=SimpleNamespace(strategies={}),
                                        get_spread=lambda _: None)
        engine = SimpleNamespace(get_engine=lambda _: spread_engine,
            get_gateway=lambda _: None, spread_tick_recorder=None)
        td = SimpleNamespace(position_snapshot_at=1.0)
        gateway = SimpleNamespace(
            td_api=td, query_account=Mock(),
            query_position=Mock(side_effect=[PositionQueryError(-2), PositionQueryError(-2), None]),
        )
        with patch("spreadconsole.ui.widget.SpreadTradingControl") as factory:
            control = factory.return_value
            control.service.list.return_value = {}
            control.master_running = False
            control.md_ready.return_value = True
            control.td_ready.return_value = True
            control._gateway.return_value = gateway
            widget = SpreadConsoleWidget(engine, None)
            scheduled: list = []
            with patch.object(QtCore.QTimer, "singleShot",
                              side_effect=lambda _ms, callback: scheduled.append(callback)), \
                    patch.object(QtWidgets.QMessageBox, "warning") as warning:
                widget._confirm_reconciliation()
                self.assertTrue(widget._reconciliation_pending)
                # 旧快照由查询本身作废；点了核对但请求未发出时不应丢掉上次结果。
                self.assertEqual(td.position_snapshot_at, 1.0)
                gateway.query_position.assert_not_called()      # 资金与持仓错开发送
                while gateway.query_position.call_count < 3:
                    scheduled.pop(0)()
                self.assertEqual(gateway.query_position.call_count, 3)
                self.assertTrue(widget._reconciliation_pending)
                warning.assert_not_called()
            widget.close()

    def test_reconciliation_dialog_scrolls_instead_of_growing(self):
        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        text = "\n".join(f"第 {index} 行：合约 A_2701_2703 状态说明" for index in range(200))
        dialog = ReconciliationDialog(text)
        self.assertIsInstance(dialog.view, QtWidgets.QPlainTextEdit)
        self.assertTrue(dialog.view.isReadOnly())
        self.assertEqual(dialog.plain_text(), text)  # 内容完整，不是截断显示
        self.assertEqual(dialog.view.lineWrapMode(),
                         QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.assertGreaterEqual(dialog.minimumHeight(), 300)
        self.assertEqual(dialog.confirm_button.text(), "确认已核对")
        self.assertEqual(dialog.cancel_button.text(), "取消")
        self.assertIsNone(dialog.clear_button)
        dialog.close()

    def test_reconciliation_dialog_offers_clear_when_leftover_orders_exist(self):
        QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cleared = Mock(return_value=True)
        dialog = ReconciliationDialog("上次会话委托 1 笔", on_clear=cleared)
        self.assertIsNotNone(dialog.clear_button)
        self.assertEqual(dialog.clear_button.text(), "清除遗留委托")
        dialog._clear_leftovers()
        cleared.assert_called_once()
        dialog.close()


