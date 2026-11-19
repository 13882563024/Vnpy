from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from trading_core.config_adapter import profile
from trading_core.replay_runner import run_events
from trading_core.journal import Journal


ROOT=Path(__file__).resolve().parents[1]; CONFIG=ROOT/'config/spreads.json'; SPREAD='A_2701_2703'


def sample_events():
    day='20261119'; c=profile(CONFIG,SPREAD).config
    for i in range(31):
        t=11*i; mid=(-1)**i if i<30 else -8
        def leg(s,px):
            return {'symbol':s,'event_id':f'{s}-{i}','timestamp':t,'bid':px,'ask':px+1,'bid_size':4,'ask_size':4,'volume':5}
        yield dict(type='quote',day=day,id=f'q{i}',near=leg(c.near,100+mid),far=leg(c.far,100),now=t,
                   checks=dict(in_session=True,can_open=i==30,listed=True,funds_ok=True,quota_ok=True,days_to_delivery=12))
    yield dict(type='matched',day=day,id='f',decision_event='q30', lots=1,average_spread=-9,near_average=91,
               far_average=100,residual_near=0,residual_far=0)
    yield dict(type='terminal',day=day,id='t',decision_event='q30',filled=1)
    yield dict(type='protect',day=day,id='p',fees=dict(tier_id='L1',open_day=day,close_day=day,
               open_near=2,open_far=2,close_near=2,close_far=2,source='synthetic-test'))


class ReplayRunnerTests(unittest.TestCase):
    def test_real_profile_data_signal_to_replayable_workbench_ledger(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'shadow.sqlite'; args=dict(config_path=CONFIG,spread=SPREAD,account='paper-demo',output=path)
            r=run_events(**args,events=sample_events())
            self.assertEqual([a['intent']['purpose'] for a in r['actions']],['open','take_profit'])
            self.assertEqual(r['actions'][-1]['intent']['price'],-7)
            self.assertEqual(r['layers'][0]['actual_spread'],'-9')
            self.assertEqual(len([e for e in Journal.open_readonly(path).replay() if e.kind=='order_intent']),2)
            with self.assertRaises(FileExistsError): run_events(**args,events=())

    def test_feedback_cannot_attach_to_missing_decision(self):
        with TemporaryDirectory() as d, self.assertRaisesRegex(ValueError,'未产生意图'):
            run_events(config_path=CONFIG,spread=SPREAD,account='paper',output=Path(d)/'run.db',
                       events=[dict(day='20261119',id='x',type='terminal',decision_event='absent',filled=1)])
