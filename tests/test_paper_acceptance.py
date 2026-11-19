from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from trading_core.journal import Journal
from trading_core.paper_run import run_paper
from trading_core.replay_runner import json_lines

ROOT=Path(__file__).resolve().parents[1]


class PaperAcceptance(TestCase):
    def test_three_layers_roundtrip_runs_through_real_preflights_and_bindings(self):
        with TemporaryDirectory() as d:
            out=Path(d)/'new';r=run_paper(ROOT/'config/spreads.json','A_2701_2703',
                                         json_lines(ROOT/'examples/paper_bean.jsonl'),out)
            self.assertEqual((r['orders'],r['trade_rows'],r['active_orders'],r['cancelled']),(8,12,0,2))
            self.assertEqual((r['broker_leg_positions'],r['remaining']),({},[]))
            rows=list(Journal.open_readonly(out/'facts.sqlite').replay())
            self.assertEqual(rows[0].kind,'paper_session')
            self.assertEqual(len([x for x in rows if x.kind=='account_snapshot']),8)
            self.assertEqual(len([x for x in rows if x.kind=='order_binding']),8)
            opens=[x for x in rows if x.kind=='order_intent' and x.payload['purpose'] in ('open','add')]
            self.assertEqual([x.payload['tier_id'] for x in opens],['L1','L2','L3'])
            with self.assertRaises(FileExistsError): run_paper(ROOT/'config/spreads.json','A_2701_2703',[],out)

    def test_proven_flat_can_reenter_in_opposite_direction(self):
        from copy import deepcopy
        rows=list(json_lines(ROOT/'examples/paper_bean.jsonl')); base=rows[-1]['now']
        for i in range(7):
            q=deepcopy(rows[30]); q.update(id=f'reverse{i}',now=base+i+1)
            for side,px in [('near',130),('far',100)]:
                q[side].update(event_id=q['id']+side, timestamp=q['now'],bid=px,ask=px+1)
            rows.append(q)
        with TemporaryDirectory() as d:
            out=Path(d)/'new'; result=run_paper(ROOT/'config/spreads.json','A_2701_2703',rows,out)
            self.assertEqual((result['orders'],result['active_orders']),(9,1))
            last=[x for x in Journal.open_readonly(out/'facts.sqlite').replay() if x.kind=='order_intent'][-1]
            self.assertEqual((last.payload['side'],last.payload['tier_id']),('sell','L4'))

    def test_missing_followup_quotes_denies_stale_first_entry(self):
        rows=list(json_lines(ROOT/'examples/paper_bean.jsonl'))[:34]
        for e in rows[-3:]: e['type']='timer'
        with TemporaryDirectory() as d:
            out=Path(d)/'new'
            with self.assertRaisesRegex(ValueError,'未获准'):
                run_paper(ROOT/'config/spreads.json','A_2701_2703',rows,out)
            self.assertFalse(any(r.kind=='order_binding' for r in Journal.open_readonly(out/'facts.sqlite').replay()))
