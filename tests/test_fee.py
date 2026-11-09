"""T1–T3：费率转换、手续费计算、自适应 ``min_exit_pnl``。

用 ``python -m unittest discover tests`` 运行（标准库 unittest，不引入 pytest）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fees.calculator import (  # noqa: E402
    DEFAULT_FEE,
    KIND_CLOSE,
    KIND_TODAY,
    FeeCalculator,
    FeeSpec,
    product_prefix,
    spec_from_record,
)
from tools.build_fee_table import DEFAULT_XLSX, build, read_workbook  # noqa: E402

# 推荐文档给出的乘数 / 价格区间（方案 4.3 已核实数值）
PRODUCT_CASES: dict[str, dict] = {
    "J": {"size": 100, "tick": 0.5, "price": 1250.0, "expected_min_pnl": 50.0},
    "OI": {"size": 10, "tick": 1.0, "price": 9000.0, "expected_min_pnl": 10.0},
    "A": {"size": 10, "tick": 1.0, "price": 4000.0, "expected_min_pnl": 10.0},
    "B": {"size": 10, "tick": 1.0, "price": 3600.0, "expected_min_pnl": 10.0},
    "RM": {"size": 10, "tick": 1.0, "price": 2400.0, "expected_min_pnl": 10.0},
}


class TestCustomFeeRecord(unittest.TestCase):
    def test_custom_fee_overrides_table_and_falls_back_per_field(self):
        base = FeeSpec(exchange="DCE", unit="元", open=2.0,
                       close_yesterday=2.0, close_today=2.0)
        custom = spec_from_record(
            {"unit": "万分之", "open": 0.5, "close_today": 0.0}, base)
        self.assertEqual(custom.unit, "万分之")
        self.assertEqual(custom.open, 0.5)
        self.assertEqual(custom.close_yesterday, 2.0)
        self.assertEqual(custom.close_today, 0.0)
        self.assertEqual(custom.exchange, "DCE")

        fallback = spec_from_record({"unit": "bad", "open": "bad"}, base)
        self.assertEqual(fallback.unit, "元")
        self.assertEqual(fallback.open, 2.0)


class TestFeeTableConversion(unittest.TestCase):
    """T1：xlsx 89 行全部转换，unit 分类正确。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_XLSX.exists():
            raise unittest.SkipTest(f"缺少手续费表：{DEFAULT_XLSX}")
        import tempfile

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.output = Path(cls.tmpdir.name) / "fee_table.json"
        cls.table = build(DEFAULT_XLSX, cls.output)
        cls.sheets = read_workbook(DEFAULT_XLSX)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()

    def test_product_rows_all_converted(self) -> None:
        """表头 1 行 + 88 个品种；全部落到 products。"""
        rows = self.sheets["期货手续费"]
        self.assertEqual(len(rows), 89)
        self.assertEqual(len(self.table["products"]), 88)

    def test_unit_classification(self) -> None:
        products = self.table["products"]
        self.assertEqual(products["J"]["unit"], "万分之")
        for code in ("OI", "A", "B", "RM"):
            self.assertEqual(products[code]["unit"], "元", code)

    def test_recommended_values(self) -> None:
        products = self.table["products"]
        self.assertEqual(products["OI"]["exchange"], "CZCE")
        self.assertEqual(products["OI"]["open"], 2.0)
        self.assertEqual(products["J"]["exchange"], "DCE")
        self.assertEqual(products["J"]["open"], 1.0)
        self.assertEqual(products["J"]["close_today"], 1.4)
        self.assertEqual(products["B"]["open"], 1.0)
        self.assertEqual(products["RM"]["open"], 1.5)

    def test_contract_sheet_parsed(self) -> None:
        """「特定合约费率备查」进 contracts（合约级覆盖）。"""
        self.assertGreater(len(self.table["contracts"]), 0)
        self.assertIn("au2510", self.table["contracts"])


class TestFeeCalculation(unittest.TestCase):
    """T2：手算 4 组对照，误差为 0。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.calc = FeeCalculator()

    def test_j_rate_open(self) -> None:
        """J 万分之 1，价 1250，乘数 100，1 手：1250×100×1÷10000 = 12.5 元。"""
        spec = self.calc.spec("j2701.DCE", "j2703.DCE")
        self.assertEqual(spec.unit, "万分之")
        self.assertAlmostEqual(spec.edge_fee(KIND_CLOSE, 1250.0, 100, 1), 12.5)

    def test_j_rate_close_today(self) -> None:
        """J 平今万分之 1.4 → 1250×100×1.4÷10000 = 17.5 元。"""
        spec = self.calc.spec("j2701.DCE", "j2703.DCE")
        self.assertAlmostEqual(spec.edge_fee(KIND_TODAY, 1250.0, 100, 1), 17.5)

    def test_oi_fixed_open(self) -> None:
        """OI 元 2：按手固定，2 手 = 4 元，与价格无关。"""
        spec = self.calc.spec("OI505.CZCE", "OI509.CZCE")
        self.assertEqual(spec.unit, "元")
        self.assertAlmostEqual(spec.edge_fee("open", 9000.0, 10, 1), 2.0)
        self.assertAlmostEqual(spec.edge_fee("open", 9000.0, 10, 2), 4.0)

    def test_oi_fixed_close_yesterday(self) -> None:
        """OI 平昨元 2：1 手 2 元。"""
        spec = self.calc.spec("OI505.CZCE", "OI509.CZCE")
        self.assertAlmostEqual(spec.edge_fee(KIND_CLOSE, 9000.0, 10, 1), 2.0)

    def test_round_trip_four_legs(self) -> None:
        """一层四条腿：OI 1 手 = 2+2+2+2 = 8 元；3 手 = 24 元。"""
        spec = self.calc.spec("OI505.CZCE", "OI509.CZCE")
        one = self.calc.round_trip(
            spec, 10, 1, near_open_price=9000.0, far_open_price=9050.0
        )
        self.assertAlmostEqual(one.total, 8.0)
        three = self.calc.round_trip(
            spec, 10, 3, near_open_price=9000.0, far_open_price=9050.0
        )
        self.assertAlmostEqual(three.total, 24.0)

    def test_round_trip_close_today(self) -> None:
        """同日开平 → 平今费率（J：1.0 + 1.0 + 1.4 + 1.4 = 4.8 万分之）。"""
        spec = self.calc.spec("j2701.DCE", "j2703.DCE")
        breakdown = self.calc.round_trip(
            spec, 100, 1,
            near_open_price=1250.0, far_open_price=1200.0,
            close_kind=KIND_TODAY,
        )
        expected = (1250 + 1200 + 1250 + 1200) * 100 / 10000
        expected += (1250 + 1200) * 100 * 0.4 / 10000
        self.assertAlmostEqual(breakdown.total, expected, places=6)

    def test_fee_buffer(self) -> None:
        spec = self.calc.spec("OI505.CZCE", "OI509.CZCE")
        base = self.calc.round_trip(
            spec, 10, 1, near_open_price=9000.0, far_open_price=9050.0
        )
        buffered = self.calc.round_trip(
            spec, 10, 1, near_open_price=9000.0, far_open_price=9050.0, buffer=1.1
        )
        self.assertAlmostEqual(buffered.total, base.total * 1.1)


class TestMinExitPnl(unittest.TestCase):
    """T3：5 品种复现 50/10/10/10/10。"""

    def setUp(self) -> None:
        self.calc = FeeCalculator()

    def test_all_recommended_products(self) -> None:
        for code, case in PRODUCT_CASES.items():
            with self.subTest(product=code):
                spec = self.calc.spec(f"{code}2701", f"{code}2703")
                value = self.calc.min_exit_pnl(
                    spec,
                    case["size"],
                    case["tick"],
                    near_price=case["price"],
                    far_price=case["price"],
                )
                self.assertEqual(value, case["expected_min_pnl"])

    def test_j_uses_price_scaled_fee(self) -> None:
        """J 用价 1250 验证：4 × 12.5 = 50 与一跳 50 打平。"""
        spec = self.calc.spec("j2701", "j2703")
        self.assertEqual(self.calc.min_exit_pnl(spec, 100, 0.5, near_price=1250.0), 50.0)

    def test_missing_leg_price_falls_back_to_tick_value(self) -> None:
        """腿价不可得（回测）时退化为一跳金额。"""
        spec = self.calc.spec("j2701", "j2703")
        self.assertEqual(self.calc.min_exit_pnl(spec, 100, 0.5), 50.0)
        spec_rm = self.calc.spec("RM701", "RM703")
        self.assertEqual(self.calc.min_exit_pnl(spec_rm, 10, 1.0), 10.0)


class TestFeeLookupFallback(unittest.TestCase):
    """三层回落：合约 slug → 单腿合约 → 品种 → 兜底。"""

    def setUp(self) -> None:
        self.calc = FeeCalculator()

    def test_product_level(self) -> None:
        spec = self.calc.spec("OI505.CZCE", "OI509.CZCE")
        self.assertEqual(spec.open, 2.0)

    def test_contract_level_overrides_product(self) -> None:
        table = {
            "products": {"AU": {"exchange": "SHFE", "unit": "元", "open": 10.0,
                                "close_yesterday": 10.0, "close_today": 0.0}},
            "contracts": {"au2510": {"exchange": "SHFE", "unit": "元", "open": 2.0,
                                     "close_yesterday": 2.0, "close_today": 0.0}},
        }
        calc = FeeCalculator(table=table)
        self.assertEqual(calc.spec("au2510.SHFE", "au2602.SHFE").open, 2.0)
        self.assertEqual(calc.spec("au2604.SHFE", "au2606.SHFE").open, 10.0)

    def test_slug_level_first(self) -> None:
        table = {
            "products": {"A": {"exchange": "DCE", "unit": "元", "open": 2.0,
                               "close_yesterday": 2.0, "close_today": 2.0}},
            "contracts": {"A2611-A2701": {"exchange": "DCE", "unit": "元", "open": 3.5,
                                          "close_yesterday": 3.5, "close_today": 3.5}},
        }
        calc = FeeCalculator(table=table)
        self.assertEqual(calc.spec("a2611.DCE", "a2701.DCE").open, 3.5)
        # 未命中 slug 时回落到品种
        self.assertEqual(calc.spec("a2701.DCE", "a2703.DCE").open, 2.0)

    def test_unknown_product_uses_default(self) -> None:
        spec = self.calc.spec("ZZ999.X", "ZZ1001.X")
        self.assertEqual(spec.open, DEFAULT_FEE)
        self.assertEqual(spec.unit, "元")

    def test_close_kind_by_trading_day(self) -> None:
        self.assertEqual(FeeCalculator.close_kind("2026-11-02", "2026-11-02"), KIND_TODAY)
        self.assertEqual(FeeCalculator.close_kind("2026-11-02", "2026-11-03"), KIND_CLOSE)
        self.assertEqual(FeeCalculator.close_kind("", "2026-11-03"), KIND_CLOSE)


class TestHelpers(unittest.TestCase):
    def test_product_prefix(self) -> None:
        self.assertEqual(product_prefix("OI505.CZCE"), "OI")
        self.assertEqual(product_prefix("a2611"), "A")
        self.assertEqual(product_prefix("pg2701.DCE"), "PG")
        self.assertEqual(product_prefix(""), "")

    def test_rate_unit_abs_defense(self) -> None:
        """万分之品种误传负价（价差）时不应得到负费用。"""
        spec = FeeSpec(unit="万分之", open=1.0)
        self.assertAlmostEqual(spec.edge_fee("open", -1250.0, 100, 1), 12.5)


if __name__ == "__main__":
    unittest.main()
