import unittest
from datetime import datetime
from time import monotonic
from types import SimpleNamespace as N

from workbench.overview_data import overview_view


class TD(N):
    @property
    def position_query_pending(self):
        raise AssertionError('This gateway property mutates query state')


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {'X': dict(legs=['a.DCE', 'b.DCE'], exchange='DCE', exchange_spread='SP a&b',
                              strategy=dict(max_positions=3, live_trading=True))}
        now = monotonic()
        self.td = TD(brokerid='broker', userid='u', cabinet_trading_day='2026-11-19', login_status=True,
                     contract_inited=True, position_snapshot_account=('broker', 'u'), position_snapshot_at=now,
                     position_snapshot_sides={}, _position_query_pending=False, orders_snapshot_ok=True,
                     orders_snapshot_at=now, orders_snapshot=set(), _order_cache={})
        self.main = N(gateways={'CTP': N(td_api=self.td)}, get_all_accounts=lambda: [
            N(accountid='u', gateway_name='PAPER', balance=999), N(accountid='u', gateway_name='CTP', balance=123, available=0)])
        self.s = N(layers=[], direction=1, spread_name='X', reserved_exit_orders={}, jobs={})
        self.engine = N(strategy_engine=N(strategies={'test': self.s}))

    def row(self):
        return overview_view(self.cfg, self.main, self.engine)['rows'][0]

    def test_live_trading_true_is_not_reported_as_unknown(self):
        """实盘开着却显示"实盘权限未知"是漏了分支；应为"实盘可开仓"。"""
        self.s.live_trading = True
        self.s.close_only = False
        self.s.paused = False

        self.assertEqual(self.row()['opening'], '实盘可开仓')

    def test_config_not_evidence(self):
        v = overview_view(self.cfg)
        self.assertEqual(v['balance'], '未连接柜台')
        self.assertIn('只读', v['permission'])
        self.assertIsNone(v['rows'][0]['held'])
        self.assertIn('近 a.DCE', v['rows'][0]['cabinet'])
        self.assertIn('未知', v['rows'][0]['orders'])

    def test_permission_reflects_live_strategies(self):
        """顶部权限行要反映真实开关，不能永远写"发单授权未知"。"""
        self.s.live_trading = True
        self.s.paused = False
        self.s.close_only = False

        self.assertIn('实盘发单 1', overview_view(self.cfg, self.main, self.engine)['permission'])

        self.s.paused = True
        text = overview_view(self.cfg, self.main, self.engine)['permission']
        self.assertIn('暂停 1', text)
        self.assertNotIn('实盘发单 1', text)

    def test_valid_empty_and_account_scope(self):
        v = overview_view(self.cfg, self.main, self.engine)
        self.assertEqual((v['balance'], v['available'], v['margin']), ('123.00', '0.00', '未知'))
        self.assertIs(self.row()['held'], False)
        self.assertIn('无活动委托', self.row()['orders'])
        self.assertEqual(self.row()['protection'], '无仓')
        self.main.gateways['PAPER'] = N(td_api=self.td)
        self.assertEqual(overview_view(self.cfg, self.main)['balance'], '未知')  # 已登录但资金未返回

    def test_view_snapshot_survives_reconciliation_invalidation(self):
        """委托/成交回报会作废核对快照，但后台查询的视图快照应让总表保持"已知"。"""
        self.td.position_snapshot_at = 0.0          # 来了一条回报，核对快照作废
        self.td.position_view_account = ('broker', 'u')
        self.td.position_view_at = monotonic()
        self.td.position_view_sides = {'a': dict(long=2, short=0, unknown=0),
                                       'b': dict(long=0, short=2, unknown=0)}
        self.s.direction = 1
        self.s.layers = [N(direction=1, volume=1.0, tier_id='t1', layer_index=1, is_open=True),
                         N(direction=1, volume=1.0, tier_id='t2', layer_index=2, is_open=True)]

        row = self.row()

        self.assertIs(row['held'], True)
        self.assertIn('a 多2/空0', row['cabinet'])

        self.td.position_view_at = monotonic() - 60      # 视图也过期 → 退回未知
        self.assertIsNone(self.row()['held'])

    def test_stale_pending_wrong_account_conflict(self):
        for field, value in [('position_snapshot_at', monotonic()-310), ('_position_query_pending', True),
                             ('position_snapshot_account', ('b', 'other')), ('reconciliation_event_error', 'bad'),
                             ('login_status', False), ('_login_scope', ('b', 'other', 'yesterday'))]:
            with self.subTest(field=field):
                original = vars(self.td).copy()
                setattr(self.td, field, value)
                self.assertIsNone(self.row()['held'])
                self.td.__dict__.clear()
                self.td.__dict__.update(original)
        self.td.orders_snapshot.add('CTP.unparsed')
        self.assertIn('未知', self.row()['orders'])
        self.td.orders_snapshot.clear()
        self.td.orders_snapshot_at = monotonic() + 10
        self.assertIn('未知', self.row()['orders'])

    def test_layer_leg_column_shows_entry_prices(self):
        """合约腿列改为顶层持仓的成交价：价差均价 + 当时两腿成交价。"""
        self.s.layers = [N(layer_index=1, tier_id='t1', volume=2, direction=1, is_open=True,
                           avg_spread=34.5, near_price=5041.0, far_price=5007.0)]

        row = self.row()

        self.assertIn('第1层', row['legs'])
        self.assertIn('多2手', row['legs'])
        self.assertIn('价差34.5', row['legs'])
        self.assertIn('a 5041', row['legs'])
        self.assertIn('b 5007', row['legs'])

    def test_pnl_columns(self):
        self.s.total_realized = 1234.5
        self.s.daily_realized = -12.25

        row = self.row()

        self.assertEqual(row['total'], '+1,234.50')
        self.assertEqual(row['today'], '-12.25')

    def test_latest_log_column(self):
        self.s.latest_log = lambda: '22:38:01 开仓被过滤：盘口宽 3.0tick > 2.0'
        self.assertIn('开仓被过滤', self.row()['log'])
        self.assertIn('盘口宽', self.row()['log'])

    def test_latest_log_missing_is_unknown(self):
        self.assertEqual(self.row()['log'], '未知')

    def test_spread_price_columns(self):
        """实时价差 / 预挂止盈价差 / 预挂加仓价差三列要有值。"""
        self.engine.get_spread = lambda _: N(bid_price=31.0, ask_price=33.0)
        self.s.layers = [N(layer_index=1, tier_id='lo', volume=1, direction=1, is_open=True, target_spread=30.0),
                         N(layer_index=2, tier_id='hi', volume=1, direction=1, is_open=True, target_spread=32.0)]
        self.s.pending_open = N(kind='open', price=30.5)

        row = self.row()

        self.assertEqual(row['live'], '32')          # 中间价
        self.assertEqual(row['take_profit'], '32')   # 顶层目标
        self.assertEqual(row['add'], '30.5')         # 待执行开仓价

    def test_add_spread_falls_back_to_next_layer_trigger(self):
        """没有挂单时，用策略自己的入场规则回推下一层的触发价。"""
        self.s.stat = N(mean=lambda: 40.0, sd=lambda: 1.0)
        self.s.entry_k = 0.3
        self.s.layer_step = 1.0
        self.s.max_positions = 3
        self.s.direction = -1
        self.s.layers = [N(layer_index=1, tier_id='t1', volume=1, direction=-1, is_open=True)]

        # 下一层 layer_index=2 → 阈值 = 0.3 + 1.0×1 = 1.3 → 空价差触发价 = 40 + 1.3
        self.assertEqual(self.row()['add'], '41.3')

        self.s.layers = [N(layer_index=index, tier_id=f't{index}', volume=1, direction=-1,
                           is_open=True) for index in (1, 2, 3)]
        self.assertEqual(self.row()['add'], '已满层')

    def test_spread_price_columns_fall_back_to_unknown(self):
        row = self.row()                             # 没有 spread / 待执行开仓
        self.assertEqual(row['live'], '未知')
        self.assertEqual(row['add'], '未知')

    def test_active_order_detail_is_readable(self):
        """活动委托不能只给单号：合约、方向开平、手数、价格、状态、时间、用途都要有。"""
        order = N(vt_orderid='CTP.9_1_2', gateway_name='CTP', vt_symbol='SP a&b.DCE',
                  status=N(name='NOTTRADED', value='未成交'),
                  offset=N(name='CLOSE', value='平'), direction=N(name='LONG', value='多'),
                  volume=1, traded=0, price=32, reference='CrossPeriod_X_take_profit',
                  datetime=datetime(2026, 11, 19, 22, 9, 34))
        self.td._order_cache = {'9_1_2': order}
        self.td.orders_snapshot = {'CTP.9_1_2'}

        text = self.row()['orders']

        for piece in ('SP a&b.DCE', '多平', '1手', '@32', '未成交', '22:09:34',
                      '止盈', 'CTP.9_1_2'):
            self.assertIn(piece, text, text)

    def test_layer_protection_needs_bound_active_order(self):
        top = N(layer_index=2, tier_id='hi', volume=1, direction=1, algoid='ax', target_spread=10)
        self.s.layers = [top, N(layer_index=1, tier_id='lo', volume=1, direction=1)]
        self.td.position_snapshot_sides = {'a': dict(long=2, short=0, unknown=0), 'b': dict(long=0, short=2, unknown=0)}
        self.assertIs(self.row()['held'], True)
        self.assertEqual(self.row()['protection'], '顶层止盈未决')
        self.s.jobs = {'ax': N(kind='take_profit', tier_id='hi')}
        self.engine.algo_engine = N(order_algo_map={'CTP.1': N(algoid='ax')})
        o = N(vt_orderid='CTP.1', gateway_name='CTP', vt_symbol='SP a&b.DCE', status=N(name='NOTTRADED', value='未成交'),
              offset=N(name='CLOSE'), direction=N(name='SHORT'), volume=1, traded=0, price=10)
        self.td._order_cache = {'1': o}
        self.td.orders_snapshot = {'CTP.1'}
        row = self.row()
        self.assertEqual(row['protection'], '顶层止盈已挂')
        self.assertIn('低层按策略', row['detail'])
        self.assertIn('实挂 1 手', row['detail'])
        self.s.reserved_exit_orders['CTP.other'] = ''
        self.assertEqual(self.row()['protection'], '平仓未决')
        self.s.reserved_exit_orders.clear()
        self.cfg['Y'] = dict(self.cfg['X'], legs=['a.DCE', 'c.DCE'])
        self.assertIsNone(self.row()['held'])
        self.assertNotIn('已挂', self.row()['protection'])
        o.status = N(name='CANCELLED')
        self.assertIn('无活动委托', self.row()['orders'])

    def test_net_zero_is_not_flat(self):
        self.td.position_snapshot_sides = {'a': dict(long=1, short=1, unknown=0)}
        self.assertIsNone(self.row()['held'])
        self.assertNotEqual(self.row()['protection'], '无仓')
        self.td.position_snapshot_sides['a']['unknown'] = float('inf')
        self.assertIn('未知', self.row()['cabinet'])
