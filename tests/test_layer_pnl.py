from decimal import Decimal as D
import unittest
from trading_core.pnl import layer_pnl


def pnl(**override):
    args=dict(direction=1,opened=2,entry=D(-12),multiplier=D(10),exits=[(1,D(-10))],
              open_fee=D(8),paid_exit_fee=D(4),spread_bid=D(-11),spread_ask=D(-9))
    return layer_pnl(**(args|override))


class LayerPnlTests(unittest.TestCase):
    def test_partial_and_completed_layer_fees_allocated_once(self):
        p=pnl(); self.assertEqual((p.remaining,p.realized_gross,p.realized_net,p.unrealized_gross,p.unallocated_open_fee),(1,20,12,10,4))
        p=pnl(exits=[(1,D(-10)),(1,D(-11))],paid_exit_fee=D(8))
        self.assertEqual((p.realized_net,p.remaining,p.unallocated_open_fee),(14,0,0))

    def test_short_marks_at_ask_and_unknown_fee_not_zero(self):
        p=pnl(direction=-1,exits=[(1,D(-14))], paid_exit_fee=None)
        self.assertEqual(p.realized_gross,20); self.assertIsNone(p.realized_net)
        self.assertEqual(p.unrealized_gross,-30)
        self.assertIsNone(pnl(spread_bid=None).unrealized_gross)

    def test_overclose_rejected(self):
        with self.assertRaises(ValueError): pnl(exits=[(3,D(10))])
