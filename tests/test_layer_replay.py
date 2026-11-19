from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from trading_core.layer_replay import ShadowLayers
from trading_core.layers import Enter, Fill, Terminal, Protect
from trading_core.journal import Journal


class ShadowTests(unittest.TestCase):
    def setUp(self):
        d=TemporaryDirectory(); self.addCleanup(d.cleanup)
        self.path=Path(d.name)/'shadow.db'

    def restart(self):
        return ShadowLayers(Journal(self.path),account='b/a',strategy='sp',max_layers=3,max_lots=3)

    def test_recover_overnight_layers_without_duplicate_action(self):
        s=self.restart()
        s.observe('20261118','decision',Enter('open1',1))
        s.observe('20261118','trade1',Fill('open1','k1',1))
        s.observe('20261118','done1',Terminal('open1',1))
        s=self.restart()
        self.assertEqual(s.book.layers[0].remaining,1)
        actions=s.observe('20261119','decision',Protect('tp1'))
        self.assertEqual(actions[0].tier_id,'L1')
        self.assertFalse(self.restart().observe('20261119','decision',Protect('tp1')))

    def test_conflict_freezes_projection_after_restart(self):
        s=self.restart(); s.observe('20261119','decision',Enter('open1',1))
        with self.assertRaises(ValueError): s.observe('20261119','decision',Enter('open1',-1))
        with self.assertRaises(ValueError): self.restart().observe('20261119','other',Enter('open2',1))
