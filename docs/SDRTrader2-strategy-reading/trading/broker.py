"""Broker 抽象接口（规划方案 §6.2）：策略层只面向 Broker 接口。

实盘（CtpBroker，M5）与模拟（SimulatedBroker）实现同一规范，策略层无感知。
合约 = SPD 组合合约（如 "SPD OI611&OI701"），CTP 原生支持。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderSpec:
    """发单规格。direction = 买卖方向；side = 开平（从回报获得，不靠买卖方向猜）。"""
    slug: str
    direction: str              # buy / sell
    side: str                   # open / close
    volume: int
    price: float
    instrument: str = ""        # SPD 组合合约代码；空则用 slug
    local_id: str = ""          # 引擎先生成本地 id，Broker 沿用（绑定必须正确）
    reason: str = ""
    target_tier: int = -1       # 归属层索引（开仓单=目标新层；平仓单=要平的层；-1=整仓）
    open_price: float = 0.0     # 平仓单对应目标层的建仓价（整仓=加权均价；显示用）
    tier_id: str = ""           # 目标层稳定身份（平仓单=要平的层，替代脆弱索引）
    timeout_seconds: float = 0.0  # 挂单超时秒数（order_timeout_s，本地记录用）
    market: bool = False        # 市价平仓单（止损/强平/全平）：实盘按对手价限价发出，
                                # 成交以柜台回报为准；不长期挂着等成交（见 expire_stale）


@dataclass
class OrderRef:
    local_id: str
    broker_ref: str = ""        # Broker 侧引用（CTP 为 ctp_ref）
    slug: str = ""
    instrument: str = ""
    side: str = ""              # open / close（撤单指令带上，供实盘通道拼撤单请求）
    order_sys_id: str = ""      # 交易所报单系统编号（跨会话唯一，撤单/匹配用）


@dataclass
class Report:
    """订单/成交回报。"""
    local_id: str
    status: str                 # ACCEPTED / TRADED / CANCELLED / REJECTED / TIMEOUT
    slug: str = ""
    cum_volume: int = 0         # 累计成交手数
    cum_amount: float = 0.0     # 累计成交金额（建层加权均价依据）
    trade_price: float = 0.0    # 本次成交价
    trade_volume: int = 0       # 本次成交手数
    error_msg: str = ""
    ts: float = 0.0
    ctp_ref: str = ""           # Broker 侧引用（模拟=SIM 号，实盘=CTP 报单引用）
    order_sys_id: str = ""      # 交易所报单系统编号（跨会话撤单用）


@dataclass
class PositionInfo:
    """持仓（对账用汇总口径）。

    cost 为持仓成本金额（CTP `PositionCost`：Σ 价格 × 乘数 × 手数）；
    部分通道只给价格均价（avg_price），此时 cost=0。持仓校准按「价格优先、
    否则 cost ÷ (手数 × 乘数)」反推腿均价（见 docs/position-calibration.md）。
    """
    slug: str
    direction: str              # long / short
    volume: int
    avg_price: float = 0.0
    cost: float = 0.0


def resolve_slug_for_instrument(store, instrument: str) -> str:
    """CTP 合约代码 → 归属合约 slug（移植自 BMRTrader2）。

    组合代码（如 "SPD a2611&a2701"）精确匹配 config.instrument，唯一命中即返回。

    腿代码（CTP 成交回报 OnRtnTrade 用腿代码，如 "a2611"）按 near_leg/far_leg 匹配；
    但换月并存期同一腿会被两个跨期引用（新合约近月 = 旧合约远月，如 a2701 同属
    a2611&a2701 与 a2701&a2703）——此时单腿代码**无法唯一确定归属**，返回 ""
    交调用方按 order_ref/order_sys_id 等订单身份定位；绝不返回"第一个匹配"
    （旧行为会把新合约的成交记到旧合约名下 → 本地单永远停在 ACTIVE）。
    匹配不到 → ""（非本系统合约的回报应丢弃，防止跨合约 ref 撞号误匹配）。

    治本：data/<slug>/config.json 是网页表单保存的覆盖，通常不含
    instrument/near_leg/far_leg 字段——因此 data 先查、注册表补齐候选。
    """
    if not instrument:
        return ""
    inst = str(instrument)
    # 候选表：data/<slug>/config.json 的非空字段优先，注册表补齐缺失字段
    # （网页覆盖通常只有 mode/close_only 等，不含 instrument/near_leg/far_leg）
    entries: dict[str, dict] = {}
    try:
        for slug in store.list_slugs():
            cfg = store.read_json(slug, "config.json") or {}
            if cfg:
                entries[slug] = dict(cfg)
    except Exception:
        pass
    try:
        from strategy.config import load_registry
        reg_path = store.root.parent / "config" / "contracts.json"
        if reg_path.exists():
            reg = load_registry(reg_path)
            for slug, c in (reg.get("contracts") or {}).items():
                merged = dict(c or {})
                for k, v in (entries.get(slug) or {}).items():
                    if v not in (None, ""):
                        merged[k] = v
                entries[slug] = merged
    except Exception:
        pass

    combo_hits = [s for s, c in entries.items()
                  if str(c.get("instrument") or "") == inst]
    if len(combo_hits) == 1:
        return combo_hits[0]
    if len(combo_hits) > 1:
        return ""                       # 组合代码重复注册（数据异常）→ 歧义，不猜
    leg_hits = [s for s, c in entries.items()
                if inst in (str(c.get("near_leg") or ""), str(c.get("far_leg") or ""))]
    if len(leg_hits) == 1:
        return leg_hits[0]
    return ""                           # 无命中 / 多候选歧义


def resolve_exchange(store, slug: str, instrument: str) -> str:
    """下单/撤单用的交易所：用户添加合约时指定的 exchange 优先（品种前缀推断不可靠，
    如 SP/cs 实际属 DCE 却会被映射成 CZCE）；未配置时回退注册表，最后才按前缀推断。"""
    try:
        cfg = store.read_json(slug, "config.json") or {}
        ex = str(cfg.get("exchange") or "").strip().upper()
        if ex:
            return ex
    except Exception:
        pass
    # 注册表回退：网页与 data/<slug>/config.json 通常不写 exchange，
    # 但 config/contracts.json 里有用户添加合约时指定的交易所（撤单/发单必须精确匹配）
    try:
        from strategy.config import load_registry
        reg_path = store.root.parent / "config" / "contracts.json"
        if reg_path.exists():
            c = (load_registry(reg_path).get("contracts") or {}).get(slug) or {}
            ex2 = str(c.get("exchange") or "").strip().upper()
            if ex2:
                return ex2
    except Exception:
        pass
    from trading.exchange import exchange_of     # 延迟导入避免循环；纯函数，不依赖 vnpy_ctp
    return exchange_of(instrument)


@dataclass
class OrderStatusInfo:
    """报单查询结果（对账用）。"""
    local_id: str
    broker_ref: str = ""
    order_sys_id: str = ""      # 交易所报单系统编号（跨会话唯一：重启后对齐本地单的键）
    slug: str = ""
    status: str = ""            # ACCEPTED/TRADED/CANCELLED/REJECTED/TIMEOUT
    volume: int = 0
    cum_volume: int = 0
    cum_amount: float = 0.0
    price: float = 0.0
    direction: str = ""
    side: str = ""


class Broker(ABC):
    """交易通道抽象。"""

    is_real: bool = False       # 实盘通道标记

    @abstractmethod
    async def send_order(self, spec: OrderSpec) -> OrderRef:
        """发单。异常 = 发单失败（计入熔断 +1）。"""

    @abstractmethod
    async def cancel_order(self, ref: OrderRef) -> None:
        """撤单。异常 = 撤单指令失败（不置 cancelling，可重试）。"""

    @abstractmethod
    async def drain_reports(self, slug: Optional[str] = None) -> list[Report]:
        """拉取待处理回报；无回报返回空列表。"""

    @abstractmethod
    async def query_position(self, slug: Optional[str] = None) -> list[PositionInfo]:
        """查询持仓（对账用）。"""

    async def query_leg_position(self, instrument: str) -> list[PositionInfo]:
        """查询**单腿合约**持仓（持仓校准用：CTP 只按腿登记组合单持仓）。

        返回该腿的多/空持仓行（含今昨仓拆分；上层会按方向汇总），
        `avg_price` 为腿均价或 0；`cost` 为持仓成本金额（CTP PositionCost）。
        默认返回空列表 = 通道不支持腿维度查询（模拟通道无柜台持仓）。
        """
        return []

    @abstractmethod
    async def query_order(self, slug: Optional[str] = None) -> list[OrderStatusInfo]:
        """查询报单（对账用）。"""

    @abstractmethod
    async def query_trading_day(self) -> str:
        """当前交易日（YYYY-MM-DD）。"""

    def ensure_running(self, reason: str = "") -> bool:
        """确保交易通道可用（实盘：拉起/重拉报单引擎子进程）。

        默认无操作并返回 True（模拟通道没有子进程）；实盘通道（CppBroker）
        覆盖实现。"启动交易"与发单/撤单前调用，通道不可用返回 False。
        """
        return True

    def register_restored_order(self, ref: OrderRef) -> None:
        """登记重启恢复的实时报单（实盘通道用；模拟通道无需）。

        为什么：回报匹配依赖 Broker 侧「CTP 引用 → 本地报单」映射，而该映射是
        内存态——重启后为空，柜台侧旧会话报单的成交/撤单回报会匹配不到本地单
        被丢弃，本地永久停在"挂单中"（占住「同 slug 单挂单」互斥槽且不建层/
        不平层）。实盘通道据此把恢复单的 order_sys_id（跨会话唯一）登记进映射，
        使回报与对账能按报单编号命中。默认无操作。
        """
        return None

    @property
    def ctp_logged_in(self) -> bool:
        """交易前置是否已登录（实盘对账门禁）；非实盘通道恒为 False。"""
        return False

    async def on_market_tick(self, slug: str, spread: float) -> None:
        """行情 tick 钩子（模拟层撮合用；实盘为空）。"""
        return None
