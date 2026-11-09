"""``config/spreads.json``：用户维护的合约对注册表。

程序**不拼接**交易所组合代码（方案 2.1 / 5.3.1）：``SP`` / ``SPD`` 前缀与 3/4 位月份
两所不同，且换月后会变，全部由用户填写原始字符串。程序只做两项校验：
非空、能在合约列表里查到。

一份条目长这样::

    "OI_505_509": {
      "legs": ["OI505.CZCE", "OI509.CZCE"],     # 第一腿 = near，第二腿 = far
      "active_symbol": "OI505.CZCE",            # two_leg 的主动腿（默认 near）
      "exchange_spread": "SPD OI505&OI509",     # 交易所标准组合代码，原样使用
      "exchange": "CZCE",                       # 组合合约所在交易所（拼 vt_symbol 用）
      "product": "OI",
      "remark": "OI 1-5 正套",                  # 中文备注（界面展示用）
      "margin_per_lot": 5000,                   # 每手保证金（元，用户填写，0=未填）
      "size": 10,                               # 单腿合约乘数
      "pricetick": 1,                           # 单腿最小变动价位
      "vr_max": 0,                              # 成交量比例过滤，0 = 不启用
      "executor": "two_leg",                    # two_leg | exchange_spread
      "stop_loss": 10000,                       # 整仓止损（元），按层摊：/max_positions
      "trading_sessions": "night_2300",
      "slug": ""                                # 费率表合约 slug（可选）
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "spreads.json"

EXECUTOR_TWO_LEG = "two_leg"
EXECUTOR_EXCHANGE_SPREAD = "exchange_spread"
EXECUTORS = (EXECUTOR_TWO_LEG, EXECUTOR_EXCHANGE_SPREAD)

DEFAULT_PRICE_FORMULA = "near - far"


@dataclass
class SpreadConfig:
    """一个价差的静态配置。"""

    name: str = ""
    display_name: str = ""
    remark: str = ""                  # 中文备注（仅展示，不参与交易逻辑）
    contract_status: str = "legacy"
    trading_enabled: bool = False
    margin_per_lot: float = 0.0       # 每手保证金（元，用户填写；0 表示未填）
    legs: list[str] = field(default_factory=list)
    active_symbol: str = ""
    exchange_spread: str = ""
    exchange: str = ""
    product: str = ""
    size: float = 0.0
    pricetick: float = 0.0
    vr_max: float = 0.0
    executor: str = EXECUTOR_TWO_LEG
    stop_loss: float = 0.0
    trading_sessions: str = ""
    slug: str = ""
    min_volume: float = 1.0
    price_formula: str = DEFAULT_PRICE_FORMULA
    # 自定义手续费：{unit, open, close_yesterday, close_today}；空 = 用品种费率表
    fee: dict = field(default_factory=dict)
    # 该价差专属的策略参数覆盖（覆盖策略类默认值；命令行 --setting 优先级更高）
    strategy: dict = field(default_factory=dict)

    @property
    def near_symbol(self) -> str:
        return self.legs[0] if self.legs else ""

    @property
    def far_symbol(self) -> str:
        return self.legs[1] if len(self.legs) > 1 else ""

    @property
    def symbol_pair(self) -> tuple[str, str]:
        return self.near_symbol, self.far_symbol

    @property
    def exchange_spread_vt_symbol(self) -> str:
        """组合合约的 vt_symbol：``SP a2611&a2701`` + ``DCE`` → ``SP a2611&a2701.DCE``。

        ``exchange_spread`` 若已自带 ``.EXCHANGE`` 后缀则原样返回（不做任何拼接）。
        """
        raw = str(self.exchange_spread or "").strip()
        if not raw:
            return ""
        if "." in raw:
            return raw
        if not self.exchange:
            return ""
        return f"{raw}.{self.exchange}"

    def leg_settings(self) -> list[dict]:
        """``vnpy_spreadtrading.add_spread`` 需要的 leg_settings。"""
        settings: list[dict] = []
        for index, vt_symbol in enumerate(self.legs):
            is_near = index == 0
            settings.append({
                "vt_symbol": vt_symbol,
                "variable": "near" if is_near else "far",
                "trading_direction": 1 if is_near else -1,
                "trading_multiplier": 1 if is_near else -1,
            })
        return settings

    def validate(self) -> list[str]:
        """返回配置问题列表（空列表 = 通过）。"""
        problems: list[str] = []
        if len(self.legs) != 2:
            problems.append(f"legs 必须是两腿，实际 {len(self.legs)}")
        if not self.active_symbol:
            problems.append("缺少 active_symbol")
        elif self.active_symbol not in self.legs:
            problems.append(f"active_symbol {self.active_symbol} 不在 legs 内")
        if self.executor not in EXECUTORS:
            problems.append(f"executor 只能是 {EXECUTORS}，实际 {self.executor}")
        if self.executor == EXECUTOR_EXCHANGE_SPREAD and not self.exchange_spread:
            problems.append("executor=exchange_spread 但未填 exchange_spread（禁止发单，不自动降级 two_leg）")
        if self.size <= 0:
            problems.append(f"size 必须 > 0（不能使用组合合约的 VolumeMultiple=0），实际 {self.size}")
        if self.margin_per_lot < 0:
            problems.append(f"每手保证金不能为负，实际 {self.margin_per_lot}")
        if self.fee:
            unit = str(self.fee.get("unit") or "元")
            if unit not in ("元", "万分之"):
                problems.append(f"fee.unit 只能是 元 或 万分之，实际 {unit}")
            for key in ("open", "close_yesterday", "close_today"):
                try:
                    value = float(self.fee.get(key, 0))
                except (TypeError, ValueError):
                    problems.append(f"fee.{key} 必须是数值")
                    continue
                if value < 0:
                    problems.append(f"fee.{key} 不能为负")
        return problems


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_spread_config(name: str, raw: dict) -> SpreadConfig:
    legs = [str(item).strip() for item in (raw.get("legs") or []) if str(item).strip()]
    strategy = raw.get("strategy") or {}
    return SpreadConfig(
        name=name,
        display_name=str(raw.get("display_name") or ""),
        remark=str(raw.get("remark") or ""),
        margin_per_lot=_to_float(raw.get("margin_per_lot")),
        contract_status=str(raw.get("contract_status") or "legacy"),
        trading_enabled=bool(raw.get("trading_enabled", False)),
        legs=legs,
        active_symbol=str(raw.get("active_symbol") or (legs[0] if legs else "")).strip(),
        exchange_spread=str(raw.get("exchange_spread") or ""),
        exchange=str(raw.get("exchange") or "").strip().upper(),
        product=str(raw.get("product") or "").strip().upper(),
        size=_to_float(raw.get("size")),
        pricetick=_to_float(raw.get("pricetick")),
        vr_max=_to_float(raw.get("vr_max")),
        executor=str(raw.get("executor") or EXECUTOR_TWO_LEG).strip().lower(),
        stop_loss=_to_float(raw.get("stop_loss")),
        trading_sessions=str(raw.get("trading_sessions") or ""),
        slug=str(raw.get("slug") or ""),
        min_volume=_to_float(raw.get("min_volume"), 1.0) or 1.0,
        price_formula=str(raw.get("price_formula") or DEFAULT_PRICE_FORMULA),
        fee=dict(raw.get("fee") or {}) if isinstance(raw.get("fee"), dict) else {},
        strategy=dict(strategy) if isinstance(strategy, dict) else {},
    )


def load_raw(path: Path | str | None = None) -> dict:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_spread_configs(path: Path | str | None = None) -> dict[str, SpreadConfig]:
    """读取整张注册表 → {价差名: SpreadConfig}。"""
    return {
        name: parse_spread_config(name, raw)
        for name, raw in load_raw(path).items()
        if isinstance(raw, dict)
    }


def get_spread_config(name: str, path: Path | str | None = None) -> SpreadConfig | None:
    return load_spread_configs(path).get(name)
