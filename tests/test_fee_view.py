"""Read-only strategy projection and evidence boundaries, independent of CTP/Qt."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_core.fee_view import fee_schedule_from_table, record_paper_fees
from trading_core.journal import Journal
from trading_core.trace import trace_decision
from workbench.model import Sources, load_snapshot, scoped_decisions, strategy_returns


class FeeEvidenceTest(unittest.TestCase):
    day = '20261119'
    schedule = {'source':'交易所表 A', 'legs': {
        'N': {'unit':'万分之', 'open':1, 'close_today':2, 'close_yesterday':3},
        'F': {'unit':'元', 'open':2, 'close_today':0, 'close_yesterday':4}}}

    def setUp(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup); root = Path(tmp.name)
        self.j = Journal(root/'facts.db'); config = root/'config.json'
        config.write_text('{"A":{"exchange_spread":"N-F","legs":["N.X","F.X"]}}')
        self.sources = Sources(config=config, facts=self.j.path)

    def paper(self):
        self.j.append('a',self.day,'paper_session',('PAPER',),{'synthetic':True})

    def trade(self, decision, count, npx, fpx, *, opening=False):
        d = decision; q = count; purpose = 'open' if opening else 'take_profit'; side = 'buy' if opening else 'sell'
        reason = {'config':{'multiplier':100}, 'estimated_fee':990, 'fees':{'source':'预算，不是凭据'}}
        self.j.append('a',self.day,'order_intent',('s',d),dict(account='a',day=self.day,strategy='s',decision_id=d,
            purpose=purpose, side=side, tier_id='L1',symbol='N-F',price=1,lots=q,reason=reason))
        self.j.append('a',self.day,'order_binding',('1','1',d),dict(strategy='s',decision_id=d))
        self.j.append('a',self.day,'order',('1','1',d,'0'),dict(exchange='X',order_sys_id=d,traded=q))
        for symbol, price, buy in [('N',npx,opening), ('F',fpx,not opening)]:
            self.j.append('a',self.day,'fill',('X',d,symbol),dict(symbol=symbol,price=price,volume=q,
                direction='0' if buy else '1',offset='0' if opening else '1',order_sys_id=d))
        return trace_decision(self.j.replay(),'a',self.day,'s',d)

    def record(self, trace, schedule):
        return record_paper_fees(self.j,trace,fee_schedule=schedule,near='N',far='F',multiplier=100,open_day=self.day)

    def view(self):
        return strategy_returns(scoped_decisions(load_snapshot(self.sources), 'A'))[0]

    def test_partial_close_uses_pro_rata_and_per_leg_prices_not_budget(self):
        self.paper(); o = self.trade('o',2,100,90,opening=True); x = self.trade('x',1,105,90)
        self.assertEqual(self.record(o,self.schedule),2); self.assertEqual(self.record(o,self.schedule),0)
        self.assertEqual(self.record(x,self.schedule),2)
        before = self.j.path.read_bytes(); v = self.view()
        self.assertTrue(v['paper']); self.assertEqual(v['sources'],['交易所表 A'])
        self.assertEqual((v['gross'],v['fee'],v['net']), (500, Decimal('5.10'), Decimal('494.90')))
        self.assertEqual(v['tiers'][0]['remaining'],1); self.assertEqual(v['budgets'][0][2],990)
        self.assertEqual(before,self.j.path.read_bytes())
        with self.assertRaises(ValueError): self.record(x,{**self.schedule,'source':'替换后'})
        self.assertIsNone(self.view()['net'])

    def test_missing_fee_never_uses_forecast(self):
        self.paper(); o=self.trade('o',1,100,90,opening=True); self.trade('x',1,105,90)
        self.assertEqual(self.record(o,None),0); self.record(o,self.schedule)
        self.assertIsNone(self.view()['fee']); self.assertIsNone(self.view()['net'])

    def test_live_requires_each_settlement_fill_and_scope(self):
        o=self.trade('o',1,100,90,opening=True); x=self.trade('x',1,105,90)
        with self.assertRaises(ValueError): self.record(o,self.schedule)
        for t in (o,x):
            for f in t.fills:
                p=dict(strategy='s',decision_id=t.intent.identity[1],tier_id='L1',amount='1.23',currency='CNY',source='结算单')
                self.j.append('other',self.day,'settled_fill_fee',f.identity,p)
                self.assertIsNone(self.view()['net'])
                self.j.append('a',self.day,'settled_fill_fee',f.identity,p)
        self.assertFalse(self.view()['paper']); self.assertEqual(self.view()['net'],Decimal('495.08'))

    def test_three_tier_fixture_real_table_projection(self):
        from trading_core.paper_run import run_paper
        from trading_core.replay_runner import json_lines
        root=Path(__file__).resolve().parents[1]; out=self.j.path.parent/'run'; cfg=root/'config/spreads.json'
        run_paper(cfg,'A_2701_2703',json_lines(root/'examples/paper_bean.jsonl'),out,
                  fee_schedule=fee_schedule_from_table('a2701','a2703','DCE'),publish=False)
        snapshot=load_snapshot(Sources(config=cfg,facts=out/'facts.sqlite',shadow=out/'strategy.sqlite'))
        sims=[v for v in strategy_returns(scoped_decisions(snapshot,'A_2701_2703')) if v['paper']]
        self.assertEqual(len(sims),1); v=sims[0]
        self.assertEqual((v['gross'],v['fee'],v['net'],len(v['tiers'])),(60,24,36,3))
        self.assertEqual(len([r for r in snapshot.feeds[0].events if r.kind=='paper_fill_fee']),12)

    def test_table_resolution_and_no_default(self):
        schedule=fee_schedule_from_table('a2611','a2701','DCE'); self.assertIn('sha256:',schedule['source'])
        self.assertEqual(Decimal(schedule['legs']['a2611']['open']),2)
        for near,far,exchange in [('a2611','a2701','SHFE'),('zz2611','zz2701','DCE')]:
            with self.assertRaises(ValueError): fee_schedule_from_table(near,far,exchange)
