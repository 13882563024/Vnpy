from dataclasses import replace
import unittest
from trading_core.intents import OrderIntent
from trading_core.preflight import Evidence, GateDenied, Preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.t = 10.
        self.gate = Preflight(ttl_seconds=5, clock=lambda:self.t)
        self.i = OrderIntent('account','20261119','strategy','id','tier','add','SP A&B','buy',1,0.,'cfg',{'z':-2})

    def fresh(self, intent=None, **overrides):
        r = self.gate.begin(intent or self.i,'session')
        return self.gate.seal(replace(Evidence(r,True,True,True,True,True,True,True,False,1,0,100,50),**overrides))

    def test_reconstructed_or_modified_evidence_is_not_a_permit(self):
        authentic = self.fresh()
        with self.assertRaises(GateDenied): self.gate.consume(self.i,'session',replace(authentic))

    def test_same_evidence_not_reused_or_shared(self):
        e1, e2 = self.fresh(), self.fresh()
        self.gate.consume(self.i,'session',e1)
        for e in (e1,e2):
            with self.assertRaises(GateDenied): self.gate.consume(self.i,'session',e)

    def test_state_change_timeout_changed_intent_or_account(self):
        e = self.fresh()
        self.gate.invalidate()
        with self.assertRaises(GateDenied): self.gate.consume(self.i,'session',e)
        e = self.fresh(); self.t += 6
        with self.assertRaises(GateDenied): self.gate.consume(self.i,'session',e)
        for changes in ({'lots':2},{'price':1},{'tier_id':'other'},{'account':'other'}):
            with self.assertRaises(GateDenied): self.gate.consume(replace(self.i,**changes),'session',self.fresh())

    def test_missing_evidence_or_insufficient_capacity_blocks(self):
        for changes in ({'fills_complete':False},{'orders_complete':False},{'conflict':True},
                        {'quotes_current':False},{'available_cash':None},{'extra_margin':101},
                        {'free_open_lots':0},{'extra_margin':float('nan')}):
            with self.assertRaises(GateDenied): self.gate.consume(self.i,'session',self.fresh(**changes))

    def test_closing_cannot_borrow_open_capacity(self):
        i = replace(self.i,purpose='take_profit')
        with self.assertRaises(GateDenied): self.gate.consume(i,'session',self.fresh(i))
        self.gate.consume(i,'session',self.fresh(i,free_close_lots=1,extra_margin=None))
