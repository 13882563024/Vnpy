"""Isolated disk and synthetic cabinet observations; never connect to a gateway."""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, time
from types import SimpleNamespace as N
from unittest.mock import patch

from workbench import overview_cache as cache
from workbench.overview_data import overview_view


class OverviewHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        path = patch.object(cache, '_file_path', side_effect=self.directory.joinpath)
        path.start()
        self.addCleanup(path.stop)
        self.config = {'X': dict(legs=['a.DCE', 'b.DCE'], exchange='DCE', exchange_spread='SP a&b',
                                 price_formula='near - far')}
        self.write_connection()
        self.scope = cache.configured_scope()
        now = monotonic()
        self.day = '2026-11-24'
        self.td = N(login_status=True, cabinet_trading_day=self.day, brokerid='broker', userid='user',
                    overview_front=('tcp://cabinet:123', True), account_cache_token=object(),
                    position_view_account=('broker', 'user'), position_view_at=now,
                    position_view_sides={'a': dict(long=2, short=0, unknown=0),
                                         'b': dict(long=0, short=2, unknown=0)},
                    orders_snapshot=set(), orders_snapshot_ok=True, orders_snapshot_at=now,
                    orders_snapshot_active={})
        self.account = N(accountid='user', gateway_name='CTP', balance=123.45, available=0,
                         curr_margin=5.5, position_profit=-31,
                         ctp_cache_token=self.td.account_cache_token, ctp_observed_at=now)
        self.gateway = N(td_api=self.td, md_api=N(login_status=True))
        self.main = N(gateways={'CTP': self.gateway}, get_all_accounts=lambda: [self.account])
        self.quote = N(bid_price=-1, ask_price=1, bid_volume=1, ask_volume=1,
                       price_formula='near - far', variable_symbols={'near': 'a.DCE', 'far': 'b.DCE'},
                       datetime=datetime.now(timezone.utc),
                       legs={leg: N(tick=N(datetime=datetime.now(timezone.utc), gateway_name='CTP'))
                             for leg in self.config['X']['legs']})
        self.quote.datetime = datetime.now(timezone.utc)
        self.spread = N(get_spread=lambda _: self.quote)

    def write_connection(self, **overrides):
        setting = {'柜台环境': '实盘', '交易服务器': 'cabinet:123', '经纪商代码': 'broker',
                   '用户名': 'user', '密码': 'DO-NOT-PERSIST', '授权编码': 'AUTH-DO-NOT-PERSIST'}
        self.directory.joinpath('connect_ctp.json').write_text(
            json.dumps({**setting, **overrides}, ensure_ascii=False), encoding='utf-8')

    def online(self):
        return overview_view(self.config, self.main, self.spread, cache=cache)

    def after_restart(self, gateway='CTP'):
        engine = N(gateways={gateway: N(td_api=N(login_status=False))})
        return overview_view(self.config, engine, cache=cache)

    def test_offline_cold_start_does_not_create_samples(self):
        view = self.after_restart()
        self.assertEqual(view['source'], '无可用本地缓存')
        self.assertEqual(view['balance'], '未连接柜台')
        self.assertEqual(view['rows'][0]['live'], '未知')
        self.assertFalse(cache._path(self.scope).exists())

    def test_actual_observations_round_trip_and_do_not_claim_current_state(self):
        view = self.online()
        self.assertEqual((view['balance'], view['available'], view['rows'][0]['live']), ('123.45', '0.00', '0'))
        history = self.after_restart()
        self.assertEqual(history['account'], 'broker / user（实盘历史）')
        self.assertEqual(history['day'], self.day)
        self.assertIn('历史，非实时', history['source'])
        self.assertIn('上次资金快照', history['updated'])
        self.assertNotEqual(history['connection'], '交易端已登录')
        self.assertEqual(history['balance'], '123.45（历史）')
        self.assertEqual(history['available'], '0.00（历史）')
        self.assertEqual(history['realized'], '未连接柜台')
        row = history['rows'][0]
        self.assertIsNone(row['held'])
        self.assertEqual((row['opening'], row['protection'], row['add']), ('离线', '柜台未核实', '未知'))
        self.assertEqual(row['live'], '0（历史）')
        self.assertIn('a 多2/空0', row['detail'])
        self.assertIn('历史委托', row['orders'])
        self.assertIn('非当前', row['orders'])
        cached = cache._path(self.scope).read_text(encoding='utf-8')
        self.assertNotIn('DO-NOT-PERSIST', cached)
        self.assertNotIn('柜台环境', cached)

    def test_switch_account_front_environment_or_spread_definition(self):
        self.online()
        for setting in ({'用户名': 'other'}, {'经纪商代码': 'other'}, {'交易服务器': 'other:123'}, {'柜台环境': '测试'}):
            with self.subTest(setting=setting):
                self.write_connection(**setting)
                self.assertEqual(self.after_restart()['balance'], '未连接柜台')
                self.assertEqual(self.after_restart()['rows'][0]['live'], '未知')
        self.write_connection()
        self.assertEqual(self.after_restart('PAPER')['balance'], '未连接柜台')
        self.config['X']['price_formula'] = 'far - near'
        row = self.after_restart()['rows'][0]
        self.assertEqual(row['live'], '未知')
        self.assertNotIn('历史腿仓', row['detail'])

    def test_oms_from_previous_session_and_bad_quotes_are_not_published(self):
        self.td.account_cache_token = object()  # reconnected, old OMS data still exists
        self.quote.legs['a.DCE'].tick.gateway_name = 'PAPER'
        self.td.position_view_at -= 60
        self.td.orders_snapshot_at -= 310
        self.assertEqual(self.online()['balance'], '未知')
        self.assertFalse(cache._path(self.scope).exists())

    def test_frozen_order_query_is_not_substituted_by_live_cache(self):
        authoritative = N(vt_orderid='CTP.query', vt_symbol='SP a&b.DCE', gateway_name='CTP',
                          status=N(name='NOTTRADED', value='未成交'), price=17)
        self.td.orders_snapshot = {authoritative.vt_orderid}
        self.td.orders_snapshot_active = {authoritative.vt_orderid: authoritative}
        self.td._order_cache = {'query': N(**{**vars(authoritative), 'status': N(name='CANCELLED')})}
        self.online()
        row = self.after_restart()['rows'][0]
        self.assertIn('CTP.query', row['orders'])
        self.assertIn('@17', row['orders'])
        self.assertIn('非当前', row['orders'])

    def test_repeated_poll_and_old_day_do_not_rewrite_a_snapshot(self):
        self.online()
        recorded = cache.load(self.scope)
        path = cache._path(self.scope)
        mtime = path.stat().st_mtime_ns
        self.online()
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        cache.save(self.scope, '2026-11-23', {'at': time(), 'value': {'balance': 1}}, {})
        self.assertEqual(cache.load(self.scope), recorded)
        cache.save(self.scope, '2026-11-25', {'at': time(), 'value': {'balance': 1}}, {})
        self.assertEqual(cache.load(self.scope)['rows'], {})

    def test_atomic_failure_preserves_last_snapshot_and_cleans_up(self):
        self.online()
        previous = cache.load(self.scope)
        with patch.object(Path, 'replace', side_effect=PermissionError('busy')):
            cache.save(self.scope, self.day, {'at': time() + 1, 'value': {'balance': 87}}, {})
        self.assertEqual(cache.load(self.scope), previous)
        self.assertEqual(list(cache._path(self.scope).parent.glob('*.tmp')), [])
        cache._path(self.scope).write_text('not json', encoding='utf-8')
        self.assertEqual(self.after_restart()['balance'], '未连接柜台')
