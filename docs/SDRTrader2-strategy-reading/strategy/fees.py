"""手续费费率规格（费率表 config/fee_table.csv）。

交易所费率表（CSV；`#` 注释、空行忽略）：

    slug, 计费单位, 开仓, 平仓(平昨), 平今
    a2611-a2701,元,2,2,2
    j2611-j2701,万分之,1,1,1.4

计费单位：

- ``元``     每手固定费用（元/手/边）；
- ``万分之`` 按成交金额比例（填 ``1`` 表示万分之 1，
  单边费用 = 价格 × 合约乘数 × 费率 ÷ 10000）。

未命中 slug → 固定 2 元/手/边（与历史默认口径一致）。

一笔跨期交易的 4 条腿（近/远月 × 开/平）分别按上述规则计费；
平仓使用"平今"还是"平昨"由开仓/平仓是否同一交易日决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

UNIT_FIXED = "元"
UNIT_RATE = "万分之"
UNIT_RATE_DIVISOR = 10000.0
DEFAULT_FEE = 2.0          # 未命中费率表时的固定单价（元/手/边）

# 边类型 → FeeSpec 字段
_EDGE_KINDS = ("open", "close", "today")


@dataclass
class FeeSpec:
    """单腿单边的费率规格。"""

    unit: str = UNIT_FIXED
    open: float = DEFAULT_FEE      # 开仓费率
    close: float = DEFAULT_FEE     # 平仓（平昨）费率
    today: float = DEFAULT_FEE     # 平今费率

    @property
    def per_lot(self) -> float:
        """兼容旧口径：单腿单边的固定费用（元）。万分之计费返回 0。"""
        return float(self.open) if self.unit == UNIT_FIXED else 0.0

    def rate_of(self, kind: str) -> float:
        """取指定边类型的费率（open / close=平昨 / today=平今）。"""
        if kind not in _EDGE_KINDS:
            raise ValueError(f"未知边类型: {kind}（应为 {_EDGE_KINDS}）")
        return float(getattr(self, kind))

    def edge_fee(self, price: float, multiplier: float, kind: str) -> float:
        """单腿单边 1 手的费用（元）。

        - 元计费：直接返回费率（元/手）；
        - 万分之：|价| × 乘数 × 费率 ÷ 10000。
        """
        rate = self.rate_of(kind)
        if self.unit == UNIT_RATE:
            return abs(float(price)) * float(multiplier) * rate / UNIT_RATE_DIVISOR
        return rate


def parse_line(line: str) -> Optional[tuple[str, FeeSpec]]:
    """解析一行费率表 → (slug, FeeSpec)；注释/空行/非法行返回 None。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 5:
        return None
    slug, unit = parts[0], parts[1]
    if not slug or unit not in (UNIT_FIXED, UNIT_RATE):
        return None
    try:
        spec = FeeSpec(unit=unit, open=float(parts[2]),
                       close=float(parts[3]), today=float(parts[4]))
    except ValueError:
        return None
    return slug, spec


def load_fee_table(path: Path) -> dict[str, FeeSpec]:
    """读取整张费率表 → {SLUG 大写: FeeSpec}（文件缺失/不可读 → 空表）。"""
    table: dict[str, FeeSpec] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                parsed = parse_line(line)
                if parsed is not None:
                    table[parsed[0].upper()] = parsed[1]
    except OSError:
        pass
    return table


def load_fee_spec(slug: str, path: Path) -> FeeSpec:
    """按合约 slug 查费率表（精确匹配，大小写不敏感）。未命中 → 默认 2 元/手/边。"""
    return load_fee_table(path).get(str(slug).upper(), FeeSpec())
