from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from trading_core.config_adapter import profile
from trading_core.config_draft import preview,export

SOURCE=Path(__file__).resolve().parents[1]/'config/spreads.json'; NAME='A_2701_2703'

class DraftTests(unittest.TestCase):
    def test_export_new_profile_without_changing_original(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'draft.json'; before=SOURCE.read_bytes()
            draft=preview(SOURCE,NAME,{'entry_k':1.1,'max_positions':2})
            export(SOURCE,path,draft)
            self.assertEqual(profile(path,NAME).config.max_lots,2)
            self.assertEqual(profile(path,NAME).config.entry_k,1.1)
            self.assertEqual(SOURCE.read_bytes(),before)
            with self.assertRaises(FileExistsError): export(SOURCE,path,draft)

    def test_no_permission_change_stale_tampered_and_invalid_profiles(self):
        with TemporaryDirectory() as d:
            src=Path(d)/'base'; src.write_bytes(SOURCE.read_bytes());target=src.with_name('new')
            with self.assertRaises(ValueError): preview(src,NAME,{'live_trading':True})
            bad=preview(src,NAME,{'layer_volume':1.5})
            with self.assertRaises(ValueError): export(src,target,bad)
            good=preview(src,NAME,{'layer_volume':2})
            with self.assertRaises(ValueError): export(src,target,replace(good,document='{}'))
            src.write_bytes(src.read_bytes()+b' ')
            with self.assertRaises(ValueError): export(src,target,good)
            self.assertFalse(target.exists())
