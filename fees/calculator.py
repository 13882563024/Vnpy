"""手续费计算（元 / 万分之、平今 / 平昨、自适应 ``min_exit_pnl``）。

口径与 ``docs/plan/PLAN-spreadtrading-跨期均值回归策略实现.md`` 第 4 节一致：

- **计费单位**：``元`` 按手固定（元/手/边）；``万分之`` 按成交额比例
  （``单边费用 = 价格 × 合约乘数 × 手数 × 费率 ÷ 10000``，价格取 ``abs`` 防御）。
- **一层四条腿**：``fee_oc = 开(near) + 开(far) + 平(near) + 平(far)``，再乘手数。
- **平今 / 平昨**按**交易日**判定（不是自然日）：夜盘属于下一交易日，用自然日会判错。
- **费率匹配三层回落**：合约 slug（``OI505-OI509``）→ 单腿合约 → 品种代码（``OI``）
  → 兜底 ``2 元/手/边``（与参考实现未命中时的默认一致）。

``fees/fee_table.json`` 由 ``tools/build_fee_table.py`` 从交易所标准费率表生成；
拿到期货公司实际加收后，替换/覆盖该 json 即可，代码无需改动。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

UNIT_FIXED = "元"
UNIT_RATE = "万分之"
UNIT_RATE_DIVISOR = 10000.0
DEFAULT_FEE = 2.0

# 边类型：开仓 / 平昨 / 平今
KIND_OPEN = "open"
KIND_CLOSE = "close"
KIND_TODAY = "today"
EDGE_KINDS = (KIND_OPEN, KIND_CLOSE, KIND_TODAY)

DEFAULT_TABLE_PATH = Path(__file__).resolve().parent / "fee_table.json"


@dataclass(frozen=True)
class FeeSpec:
    """单腿单边的费率规格。"""

    exchange: str = ""
    unit: str = UNIT_FIXED
    open: float = DEFAULT_FEE
    close_yesterday: float = DEFAULT_FEE
    close_today: float = DEFAULT_FEE

    def rate_of(self, kind: str) -> float:
        """取指定边类型的费率（open / close=平昨 / today=平今）。"""
        if kind not in EDGE_KINDS:
            raise ValueError(f"未知边类型：{kind}（应为 {EDGE_KINDS}）")
        if kind == KIND_OPEN:
            return float(self.open)
        if kind == KIND_TODAY:
            return float(self.close_today)
        return float(self.close_yesterday)

    def edge_fee(self, kind: str, price: float, size: float, volume: float = 1.0) -> float:
        """单腿单边 ``volume`` 手的费用（元）。"""
        rate = self.rate_of(kind)
        if self.unit == UNIT_RATE:
            return abs(float(price)) * float(size) * float(volume) * rate / UNIT_RATE_DIVISOR
        return rate * float(volume)


@dataclass(frozen=True)
class FeeBreakdown:
    """一层「开 + 平」四条腿的费用明细（元），便于日志/核对。"""

    open_near: float = 0.0
    open_far: float = 0.0
    close_near: float = 0.0
    close_far: float = 0.0
    buffer: float = 1.0

    @property
    def raw(self) -> float:
        return self.open_near + self.open_far + self.close_near + self.close_far

    @property
    def total(self) -> float:
        return self.raw * self.buffer

    def __str__(self) -> str:
        return (
            f"费={self.total:.2f}元(开{self.open_near:.2f}+{self.open_far:.2f}"
            f"/平{self.close_near:.2f}+{self.close_far:.2f}×{self.buffer:g})"
        )


def product_prefix(symbol: str) -> str:
    """从合约代码/合约标识提取品种前缀（``OI505`` → ``OI``、``a2611`` → ``A``）。"""
    match = re.match(r"([A-Za-z]+)", str(symbol or "").strip())
    return match.group(1).upper() if match else ""


def normalize_symbol(vt_symbol: str) -> str:
    """去掉交易所后缀并大写：``OI505.CZCE`` → ``OI505``。"""
    text = str(vt_symbol or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.strip()


class FeeCalculator:
    """费率查询 + 单层往返费用 + 自适应 ``min_exit_pnl``。"""

    def __init__(
        self,
        table_path: Path | str | None = None,
        table: dict | None = None,
        default_fee: float = DEFAULT_FEE,
    ) -> None:
        self.default_fee: float = float(default_fee)
        self.default_exchange: str = ""
        self.products: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}

        if table is None:
            path = Path(table_path) if table_path else DEFAULT_TABLE_PATH
            table = self._load(path)

        self.products = {
            str(key).upper(): value for key, value in (table.get("products") or {}).items()
        }
        self.contracts = {
            str(key).upper(): value for key, value in (table.get("contracts") or {}).items()
        }
        meta = table.get("meta") or {}
        default = meta.get("default") or {}
        if "open" in default:
            self.default_fee = float(default["open"])
        self.default_exchange = str(default.get("exchange", ""))

    # ------------------------------------------------------------ 载入
    @staticmethod
    def _load(path: Path) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------ 查询
    def _spec_from(self, record: dict) -> FeeSpec:
        return FeeSpec(
            exchange=str(record.get("exchange", self.default_exchange)),
            unit=str(record.get("unit", UNIT_FIXED)),
            open=float(record.get("open", self.default_fee)),
            close_yesterday=float(record.get("close_yesterday", self.default_fee)),
            close_today=float(record.get("close_today", self.default_fee)),
        )

    def default_spec(self) -> FeeSpec:
        return FeeSpec(
            exchange=self.default_exchange,
            unit=UNIT_FIXED,
            open=self.default_fee,
            close_yesterday=self.default_fee,
            close_today=self.default_fee,
        )

    def spec(self, near_symbol: str, far_symbol: str = "", slug: str = "") -> FeeSpec:
        """按三层回落找到费率规格：合约 slug → 单腿合约 → 品种 → 兜底。"""
        near = normalize_symbol(near_symbol)
        far = normalize_symbol(far_symbol)

        candidates: list[str] = []
        for raw in (slug, f"{near}-{far}" if near and far else "", f"{near}_{far}" if near and far else ""):
            text = str(raw or "").strip().replace("_", "-").upper()
            if text:
                candidates.append(text)
                candidates.append(text.replace("-", ""))    # OI505-OI509 → OI505OI509
        for symbol in (near, far):
            if symbol:
                candidates.append(symbol.upper())
                candidates.append(f"{symbol.upper()}-{symbol.upper()}")

        for candidate in candidates:
            record = self.contracts.get(candidate)
            if record:
                return self._spec_from(record)

        for symbol in (near, far):
            prefix = product_prefix(symbol)
            if not prefix:
                continue
            record = self.products.get(prefix)
            if record:
                return self._spec_from(record)

        return self.default_spec()

    # ------------------------------------------------------------ 计算
    def edge_fee(self, spec: FeeSpec, kind: str, price: float, size: float, volume: float = 1.0) -> float:
        return spec.edge_fee(kind, price, size, volume)

    def round_trip(
        self,
        spec: FeeSpec,
        size: float,
        volume: float,
        *,
        near_open_price: float,
        far_open_price: float,
        close_kind: str = KIND_CLOSE,
        near_close_price: float | None = None,
        far_close_price: float | None = None,
        buffer: float = 1.0,
    ) -> FeeBreakdown:
        """一层「开 + 平」四条腿费用（已乘手数）。

        平仓价缺省用开仓价估算（万分之费率下与目标价差异 < 0.1%，见方案 3.2）。
        """
        close_near_price = near_open_price if near_close_price is None else near_close_price
        close_far_price = far_open_price if far_close_price is None else far_close_price
        return FeeBreakdown(
            open_near=spec.edge_fee(KIND_OPEN, near_open_price, size, volume),
            open_far=spec.edge_fee(KIND_OPEN, far_open_price, size, volume),
            close_near=spec.edge_fee(close_kind, close_near_price, size, volume),
            close_far=spec.edge_fee(close_kind, close_far_price, size, volume),
            buffer=float(buffer),
        )

    def min_exit_pnl(
        self,
        spec: FeeSpec,
        size: float,
        tick_size: float,
        *,
        near_price: float = 0.0,
        far_price: float = 0.0,
    ) -> float:
        """自适应最低净盈利（元）：``max(4 × 单腿单边开仓费, 1 × 一跳金额)``。

        ``near_price`` / ``far_price`` 用于万分之品种；两腿都为 0（例如回测里拿不到
        腿价）时该分支退化为 0，此时只有一跳金额生效 —— 回测请改用
        ``min_exit_pnl_override`` 显式给定阈值。
        """
        leg_fee = max(
            spec.edge_fee(KIND_OPEN, near_price, size, 1.0),
            spec.edge_fee(KIND_OPEN, far_price, size, 1.0),
        )
        return max(4.0 * leg_fee, float(tick_size) * float(size))

    def lot_open_fee(self, spec: FeeSpec, price: float, size: float) -> float:
        """单腿 1 手开仓费（元），用于日志与阈值核对。"""
        return spec.edge_fee(KIND_OPEN, price, size, 1.0)

    # ------------------------------------------------------------ 交易日
    @staticmethod
    def close_kind(open_trading_day: str, current_trading_day: str) -> str:
        """按**交易日**（不是自然日）判定平今 / 平昨。"""
        if open_trading_day and current_trading_day and open_trading_day == current_trading_day:
            return KIND_TODAY
        return KIND_CLOSE


def spec_from_record(record: dict, fallback: FeeSpec) -> FeeSpec:
    """把 ``spreads.json`` 的自定义 ``fee`` 段转成 ``FeeSpec``。

    缺省字段回落到品种费率表，便于只覆盖实际被加收的那一项。
    """
    record = record or {}
    unit = str(record.get("unit") or fallback.unit).strip()
    if unit not in (UNIT_FIXED, UNIT_RATE):
        unit = fallback.unit

    def number(key: str, default: float) -> float:
        try:
            value = float(record.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return max(0.0, value)

    return FeeSpec(
        exchange=str(record.get("exchange") or fallback.exchange),
        unit=unit,
        open=number("open", fallback.open),
        close_yesterday=number("close_yesterday", fallback.close_yesterday),
        close_today=number("close_today", fallback.close_today),
    )


def load_calculator(table_path: Path | str | None = None) -> FeeCalculator:
    return FeeCalculator(table_path)
