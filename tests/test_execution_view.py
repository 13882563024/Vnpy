import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from trading_core.journal import Journal
from trading_core.ctp_facts import account_key, append_report
from trading_core.intents import OrderIntent
from trading_core.send_binding import reserve_and_bind
from trading_core.trace import trace_decision
from trading_core.decision_view import describe_execution


class ExecutionViewTest(unittest.TestCase):
    def test_real_fact_shapes_late_derivatives_and_replayed_duplicate(self):
        with TemporaryDirectory() as tmp:
            j=Journal(Path(tmp)/'facts.db'); acc=account_key('b','u'); day='20261119'
            intent=OrderIntent(acc,day,'spread','open1','L1','open','SP N&F','buy',2,-2,'cfg',{'z':-2})
            reserve_and_bind(j,intent,('2','3','4'),'DCE')
            def view():
                trace=trace_decision(j.replay(),acc,day,'spread','open1')
                return describe_execution(trace,near='N',far='F')
            self.assertIsNone(view()['paired_lots'])
            common={'ExchangeID':'DCE','TradingDay':day,'OrderSysID':'sys','Direction':'0'}
            append_report(j,'order',dict(common,InstrumentID='SP N&F',FrontID=2,SessionID=3,OrderRef='4',OrderStatus='1',CombOffsetFlag='0',LimitPrice=-2,VolumeTotalOriginal=2,VolumeTraded=1),'b','u',day)
            self.assertEqual(view()['state'],'awaiting_fill_details')
            for leg,side,price in [('N','0',88),('F','1',90),('N','0',88)]:
                append_report(j,'fill',dict(common,InstrumentID=leg,Direction=side,Volume=1,Price=price,TradeID='same'),'b','u',day)
            result=view()
            self.assertEqual(result['paired_lots'],1)
            self.assertEqual(float(result['avg_spread']),-2)
            self.assertEqual(result['unpaired_near'],0)
            self.assertEqual(result['state'],'broker_reported')
