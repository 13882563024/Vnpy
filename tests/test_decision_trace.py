from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_core.trace import trace_decision
from trading_core.journal import Journal


class TraceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.j = Journal(Path(tmp.name)/'facts.db')
        self.decision = ('spread','signal-22')
        self.local = ('1','2','3')
        self.sys = {'exchange':'DCE','order_sys_id':'123'}
        self.put('order_intent', self.decision, {'tier_id':'L2'})

    def put(self, kind, key, payload):
        self.j.append('account','20261119',kind,key,payload)

    def get(self):
        return trace_decision(self.j.replay(),'account','20261119',*self.decision)

    def bind(self):
        self.put('order_binding',self.local,{'strategy':'spread','decision_id':'signal-22','exchange':'DCE'})

    def test_crash_window_and_out_of_order_fills(self):
        self.assertEqual(self.get().state,'reserved_only')
        self.bind()
        self.assertEqual(self.get().state,'send_result_unknown')
        for symbol in ('near','far'):
            for _ in range(2):
                self.put('fill',('DCE','trade',symbol),{'order_sys_id':'123'})
        self.put('fill',('OTHER','trade','near'),{'order_sys_id':'123'})
        self.assertFalse(self.get().fills)
        self.put('order',self.local+('all','0','1'),self.sys)
        t = self.get()
        self.assertEqual(t.state,'broker_reported')
        self.assertEqual([r.identity[-1] for r in t.fills],['near','far'])

    def test_ambiguous_order_identity_is_not_guessed(self):
        self.bind()
        self.put('order',self.local+('x',),self.sys)
        self.put('order',('4','5','6','x'),self.sys)
        self.put('fill',('DCE','trade','near'),{'order_sys_id':'123'})
        self.assertEqual(self.get().state,'needs_review')
        self.assertFalse(self.get().fills)

    def test_bank_conflict_scoped_to_this_decision(self):
        self.bind()
        self.put('order_intent',self.decision,{'tier_id':'other'})
        self.assertEqual(self.get().state,'needs_review')
        with self.assertRaises(KeyError):
            trace_decision(self.j.replay(),'another-account','20261119',*self.decision)
