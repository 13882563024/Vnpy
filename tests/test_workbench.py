"""Offline contract tests. Writers below only build temporary test fixtures."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_core.journal import Journal
from workbench.model import Sources, load_snapshot, scoped_decisions, tier_returns


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)
        config=self.root/'cfg.json'; config.write_text('{"A":{"exchange_spread":"SP N&F","legs":["N.DCE","F.DCE"]}}')
        self.s=Sources(config=config, shadow=self.root/'s.db', facts=self.root/'f.db', credentials=self.root/'credentials')

    def fixture(self):
        j=Journal(self.s.shadow)
        for did,purpose,side,price in [('o','open','buy',11),('x','take_profit','sell',16)]:
            j.append('a','20261119','order_intent',('s',did),dict(account='a',day='20261119',strategy='s',decision_id=did,
                     purpose=purpose,side=side,tier_id='L1',price=80,lots=1,symbol='SP N&F',reason={'config':{'multiplier':10}},config_version='v1'))
            j.append('a','20261119','shadow_input',('s','mat'+did),dict(kind='matched',data=dict(order=did,lots=1,avg=price,residual_near=0,residual_far=0)))
        return j

    def test_missing_never_created_and_secret_never_opened(self):
        self.s.credentials.write_text('SECRET')
        with patch.object(Path,'open',autospec=True,side_effect=AssertionError('unexpected open')):
            from workbench.model import _creds
            self.assertIn('未读取',_creds(self.s.credentials))
        snap=load_snapshot(self.s)
        self.assertFalse(self.s.facts.exists()); self.assertFalse(self.s.shadow.exists()); self.assertEqual(snap.feeds[0].events,[])
        self.assertIn('未读取',snap.credential_status)

    def test_closed_snapshot_no_mutation_including_sidecars(self):
        self.fixture(); before={p.name:p.read_bytes() for p in self.root.iterdir()}
        with patch.object(Journal,'__init__',side_effect=AssertionError('writer invoked')):
            snap=load_snapshot(self.s); rows=scoped_decisions(snap,'A'); profits=tier_returns(rows)
        self.assertEqual([x['avg'] for x in rows],[Decimal(11),Decimal(16)])
        self.assertEqual(profits[0][1:3],(0,Decimal(50)))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
        self.assertEqual(scoped_decisions(load_snapshot(replace(self.s,day='20261120')),'A'),[])
        self.assertEqual(scoped_decisions(load_snapshot(replace(self.s,account='b')),'A'),[])

    def test_conflicting_input_invalidates_profit(self):
        j=self.fixture(); j.append('a','20261119','shadow_input',('s','mato'),{'other':True})
        rows=scoped_decisions(load_snapshot(self.s),'A'); self.assertEqual(rows[0]['state'],'需核对'); self.assertIsNone(tier_returns(rows)[0][2])

    def test_real_order_and_shadow_match_do_not_cross_sources(self):
        self.fixture()
        from trading_core.intents import OrderIntent
        from trading_core.send_binding import reserve_and_bind
        from trading_core.ctp_facts import account_key, append_report
        f=Journal(self.s.facts); account=account_key('b','u'); day='20261119'
        o=OrderIntent(account,day,'s','o','L1','open','SP N&F','buy',1,88,'cfg',{'z':-2})
        reserve_and_bind(f,o,('2','3','4'),'DCE')
        common=dict(ExchangeID='DCE',TradingDay=day,OrderSysID='sys',Direction='0')
        append_report(f,'order',dict(common,InstrumentID='SP N&F',FrontID=2,SessionID=3,OrderRef='4',OrderStatus='0',CombOffsetFlag='0',LimitPrice=88,VolumeTotalOriginal=1,VolumeTraded=1),'b','u',day)
        view=lambda: scoped_decisions(load_snapshot(self.s),'A')
        self.assertEqual(view()[0]['state'],'缺少成交明细')
        for leg,side,px in [('N','0',89),('F','1',90)]:
            append_report(f,'fill',dict(common,InstrumentID=leg,Direction=side,Volume=1,Price=px,TradeID='trade'),'b','u',day)
        rows=view(); self.assertEqual(rows[0]['feed'],'事实流水'); self.assertEqual(Decimal(rows[0]['avg']),Decimal(-1)); self.assertEqual(len(rows[0]['refs']),5)
        self.assertEqual(rows[1]['feed'],'影子流水'); self.assertEqual(Decimal(rows[1]['avg']),Decimal(11))

    def test_wal_rejected_without_side_effect(self):
        self.fixture(); Path(str(self.s.shadow)+'-wal').write_bytes(b'pending')
        self.assertIn('读取失败',load_snapshot(self.s).feeds[1].status)

    def test_bad_config_one_bad_source_does_not_hide_good_source(self):
        self.fixture(); self.s.facts.write_bytes(b'broken'); self.s.config.write_text('[]')
        snap=load_snapshot(self.s); self.assertFalse(snap.spreads); self.assertIn('读取失败',snap.feeds[0].status); self.assertEqual(len(snap.feeds[1].events),4)
