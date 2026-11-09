"""策略配置：从 config/contracts.json 注册表加载（规划方案 §7）。

每品种一个策略单元；通用默认参数 + 品种覆盖（stop_loss / min_exit_pnl /
volume_ratio_max / multiplier / tick_size）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# 品种代码 → 中文名（前端左侧列表显示；按腿合约前缀匹配，大小写不敏感）。
# 只用于显示，不影响交易/下单（下单代码仍是完整组合合约如 "SP a2611&a2701"）。
PRODUCT_NAME_CN: dict[str, str] = {
    # 大商所 / 郑商所 / 上期所主力品种（含用户提供的 14 个品种）
    "A": "豆一", "B": "豆二", "C": "玉米", "CS": "淀粉", "I": "铁矿石",
    "J": "焦炭", "JM": "焦煤", "M": "豆粕", "OI": "菜油", "P": "棕榈油",
    "PG": "液化气", "PK": "花生", "RM": "菜粕", "RU": "橡胶", "V": "PVC",
    "Y": "豆油",
    # 郑商所
    "FG": "玻璃", "MA": "甲醇", "TA": "PTA", "SR": "白糖", "CF": "棉花",
    "CY": "棉纱", "AP": "苹果", "CJ": "红枣", "UR": "尿素", "SA": "纯碱",
    "PF": "短纤", "SH": "烧碱", "PX": "对二甲苯", "SF": "硅铁", "SM": "锰硅",
    "ZC": "动力煤", "RI": "早籼稻", "LR": "晚籼稻", "JR": "粳稻", "RS": "菜籽",
    "WH": "强麦", "PM": "普麦",
    # 上期所 / 能源中心
    "RB": "螺纹钢", "HC": "热卷", "CU": "铜", "AL": "铝", "ZN": "锌",
    "PB": "铅", "NI": "镍", "SN": "锡", "AU": "黄金", "AG": "白银",
    "BU": "沥青", "SP": "纸浆", "SS": "不锈钢", "AO": "氧化铝",
    "BR": "丁二烯橡胶", "SC": "原油", "LU": "低硫燃料油", "NR": "20号胶",
    "BC": "国际铜", "EC": "集运指数",
    # 大商所其他 / 中金所 / 广期所
    "L": "塑料", "EG": "乙二醇", "EB": "苯乙烯", "RR": "粳米", "LH": "生猪",
    "IF": "沪深300", "IH": "上证50", "IC": "中证500", "IM": "中证1000",
    "T": "10年国债", "TF": "5年国债", "TS": "2年国债", "TL": "30年国债",
    "SI": "工业硅", "LC": "碳酸锂",
}


def product_prefix(text: str) -> str:
    """从腿合约代码/合约标识提取品种前缀（`a2611`→`A`、`OI701`→`OI`、`pg2701`→`PG`）。"""
    m = re.match(r"([A-Za-z]+)", str(text or "").strip())
    return m.group(1).upper() if m else ""


def product_name_cn(text: str) -> str:
    """品种中文名；未收录返回空字符串（前端回退为只显示合约代码）。"""
    return PRODUCT_NAME_CN.get(product_prefix(text), "")


@dataclass
class SignalParams:
    """信号参数。m1 K 线口径下语义为 K 线条数（含实时 K 线）：
    mean = 最近 (N-1) 条闭合 + 1 条实时，sd = 最近 (M-1) 条闭合 + 1 条实时。
    """
    n_minutes: int = 3
    m_minutes: int = 30
    x_sd: float = 1.0


@dataclass
class LayeringParams:
    max_positions: int = 5
    layer_step: float = 0.3


@dataclass
class FilterParams:
    width_ticks: int = 1
    sync_s: float = 0.5
    min_depth: int = 1
    volume_ratio_max: int = 0          # 0 = 不启用
    roll_entry_cutoff_days: int = 3
    roll_exit_days: int = 1
    momentum_min: int = 0              # 0 = 不启用


@dataclass
class ExitParams:
    stop_loss: float = 10000.0         # 止损金额（元）
    min_exit_pnl: float = 10.0         # MINADAPT 回归平仓阈值（元）
    max_hold_days: int = 999
    stop_spread_ticks: int = 0
    order_type: str = "market"
    order_timeout_s: float = 60.0      # 挂单超时自动撤单（秒；0 = 不启用，不重挂）


@dataclass
class SessionParams:
    """合约级交易时段配置（阶段6：非交易时段不交易）。

    - day_segments / night_segments：可选覆盖（"HH:MM-HH:MM" 列表）；
      默认 None = 未配置（引擎侧用默认时段：日盘 09:00-10:15/10:30-11:30/13:30-15:00、
      夜盘 21:00-23:00）；空列表 = 显式无该盘段。
    """
    template: str = ""
    day_segments: Optional[list] = None
    night_segments: Optional[list] = None


@dataclass
class StrategyConfig:
    slug: str = ""
    name: str = ""
    name_cn: str = ""                  # 中文备注（留空 → 按品种前缀自动映射，如 a→豆一）
    exchange: str = ""
    multiplier: float = 10.0           # 合约乘数（元/点/手）
    tick_size: float = 1.0
    instrument: str = ""               # SPD 组合合约代码（如 "SPD OI611&OI701"）
    near_leg: str = ""                 # 近月腿合约代码
    far_leg: str = ""                  # 远月腿合约代码
    mode: str = "simulate"
    close_only: bool = False           # 只平仓：开启后不执行任何自动开/加仓（仅平仓）
    exit_preplace: bool = False        # 预挂止盈：开/加仓成交后立即按目标价挂平仓单
    signal: SignalParams = field(default_factory=SignalParams)
    layering: LayeringParams = field(default_factory=LayeringParams)
    filters: FilterParams = field(default_factory=FilterParams)
    exit: ExitParams = field(default_factory=ExitParams)
    session: SessionParams = field(default_factory=SessionParams)

    @property
    def prefix(self) -> str:
        """品种前缀（用于过滤器品种匹配）。优先取近月腿字母，兜底 slug。"""
        import re
        if self.near_leg:
            m = re.match(r"([A-Za-z]+)", self.near_leg)
            if m:
                return m.group(1).upper()
        m = re.match(r"([A-Za-z]+)", self.slug)
        return m.group(1).upper() if m else self.slug.strip().upper()

    @property
    def display_name_cn(self) -> str:
        """显示用中文名：显式备注优先，否则按品种前缀自动映射（未收录为空）。"""
        return (self.name_cn or "").strip() or product_name_cn(
            self.near_leg or self.slug)


DEFAULT_REGISTRY = {
    "signal": {"n_minutes": 3, "m_minutes": 30, "x_sd": 1.0},
    "layering": {"max_positions": 5, "layer_step": 0.3},
    "filters": {
        "width_ticks": 1, "sync_s": 0.5, "min_depth": 1,
        "volume_ratio_max": 0, "roll_entry_cutoff_days": 3,
        "roll_exit_days": 1, "momentum_min": 0,
    },
    "exit": {"max_hold_days": 999, "stop_spread_ticks": 0, "order_type": "market",
             "order_timeout_s": 60.0},
    "mode": "simulate",
    "close_only": False,
    "exit_preplace": False,
}


def load_registry(path: Path) -> dict:
    """读取 contracts.json 注册表。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_strategy_config(slug: str, registry_path: Path) -> Optional[StrategyConfig]:
    """从注册表加载某个品种的策略配置。slug 为品种代码（J/OI/A/B/RM）
    或动态合约 slug（如 a2611-a2701）。"""
    reg = load_registry(registry_path)
    defaults = reg.get("defaults", DEFAULT_REGISTRY)
    contracts = reg.get("contracts", {})
    c = contracts.get(slug) or contracts.get(slug.upper()) or contracts.get(slug.lower())
    if c is None:
        return None

    sig = dict(defaults.get("signal", DEFAULT_REGISTRY["signal"]))
    lay = dict(defaults.get("layering", DEFAULT_REGISTRY["layering"]))
    flt = dict(defaults.get("filters", DEFAULT_REGISTRY["filters"]))
    ext = dict(defaults.get("exit", DEFAULT_REGISTRY["exit"]))
    ses = dict(defaults.get("session", {}) or {})

    # 品种级覆盖
    flt["volume_ratio_max"] = c.get("volume_ratio_max", flt["volume_ratio_max"])
    ext["stop_loss"] = c.get("stop_loss", ext.get("stop_loss", 10000.0))
    ext["min_exit_pnl"] = c.get("min_exit_pnl", ext.get("min_exit_pnl", 10.0))
    ext["order_timeout_s"] = float(c.get("order_timeout_s", ext.get("order_timeout_s", 60.0)))
    # 合约级交易时段覆盖（template + day/night 段；未配置保持默认/自动推断）
    c_ses = c.get("session") or {}
    for k in ("template", "day_segments", "night_segments"):
        if k in c_ses:
            ses[k] = c_ses[k]
    mode = defaults.get("mode", c.get("mode", "simulate"))
    # 只平仓：合约级覆盖优先，其次注册表默认（开启后不执行任何自动开仓）
    close_only = bool(c.get("close_only", defaults.get("close_only", False)))
    # 预挂止盈：开/加仓成交后立即按目标价挂平仓单
    exit_preplace = bool(c.get("exit_preplace", defaults.get("exit_preplace", False)))

    return StrategyConfig(
        slug=slug,
        name=c.get("name", ""),
        name_cn=str(c.get("name_cn", "") or ""),
        exchange=c.get("exchange", ""),
        multiplier=float(c.get("multiplier", 10.0)),
        tick_size=float(c.get("tick_size", 1.0)),
        instrument=str(c.get("instrument", "")),
        near_leg=str(c.get("near_leg", "")),
        far_leg=str(c.get("far_leg", "")),
        mode=mode,
        close_only=close_only,
        exit_preplace=exit_preplace,
        signal=SignalParams(**sig),
        layering=LayeringParams(**lay),
        filters=FilterParams(**flt),
        exit=ExitParams(**ext),
        session=SessionParams(**ses),
    )
