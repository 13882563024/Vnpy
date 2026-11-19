"""层账落盘与恢复：重启后成本价必须来自本地记录，而不是柜台均价。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from execution.base import PositionLayer
from strategies.cross_period_mean_reversion import CrossPeriodMRStrategy
from utils import layer_store


class LayerStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spread_layers.json"
            with patch.object(layer_store, "_path", return_value=path):
                layer_store.save("X", [{"tier_id": "t1", "volume": 2}])
                self.assertEqual(layer_store.load("X"), [{"tier_id": "t1", "volume": 2}])
                self.assertEqual(layer_store.load("Y"), [])
                layer_store.clear("X")
                self.assertEqual(layer_store.load("X"), [])

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spread_layers.json"
            path.write_text("{not json", encoding="utf-8")
            with patch.object(layer_store, "_path", return_value=path):
                self.assertEqual(layer_store.load("X"), [])

    def test_payload_keeps_cost_and_leg_prices(self):
        layer = PositionLayer(direction=1, volume=2, avg_spread=34.5,
                              near_price=5041.0, far_price=5007.0,
                              open_datetime=datetime(2026, 11, 20, 9, 30))
        payload = CrossPeriodMRStrategy._layer_payload(None, layer)

        self.assertEqual(payload["avg_spread"], 34.5)
        self.assertEqual(payload["near_price"], 5041.0)
        self.assertEqual(payload["far_price"], 5007.0)
        self.assertEqual(payload["open_datetime"], "2026-11-20T09:30:00")
        # 还原后字段类型仍可被 dataclass 接受
        payload["open_datetime"] = datetime.fromisoformat(payload["open_datetime"])
        restored = PositionLayer(**payload)
        self.assertEqual((restored.avg_spread, restored.near_price, restored.far_price),
                         (34.5, 5041.0, 5007.0))


if __name__ == "__main__":
    unittest.main()
