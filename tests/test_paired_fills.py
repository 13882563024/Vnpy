from decimal import Decimal
import unittest
from trading_core.journal import Outcome, Record
from trading_core.paired_fills import matched_fills


def row(seq, symbol, q, price, side):
    return Record(seq,'account','20261119','fill',('DCE',str(seq),symbol),
                  {'symbol':symbol,'volume':q,'price':price,'direction':side,'order_sys_id':'order'},Outcome.NEW)

def calc(*rows,buy=True):
    return matched_fills(rows,exchange='DCE',sys_id='order',near='N',far='F',combo='C',buy=buy,max_lots=2)

class PairedTests(unittest.TestCase):
    def test_fifo_excludes_unmatched_lot(self):
        n=row(1,'N',1,91,'0'); extra=row(2,'N',1,87,'0'); f=row(3,'F',1,93,'1')
        before=calc(n, extra); self.assertIsNone(before.average_spread)
        result=calc(n,extra,f,n)
        self.assertEqual((result.lots,result.average_spread,result.residual_near),(1,Decimal(-2),1))
        self.assertEqual(calc(n,extra,f,row(4,'F',1,93,'1')).average_spread,Decimal(-4))

    def test_real_zero_combo_no_invented_leg_prices(self):
        r=calc(row(1,'C',1,0,'1'),buy=False)
        self.assertEqual(r.average_spread,0)
        self.assertIsNone(r.near_average)

    def test_mixed_mode_wrong_side_overfill_isolated(self):
        for rows in [(row(1,'N',1,100,'0'), row(2,'C',1,0,'0')),
                     (row(1,'F',1,100,'0'),), (row(1,'N',3,100,'0'),)]:
            with self.assertRaises(ValueError): calc(*rows)
