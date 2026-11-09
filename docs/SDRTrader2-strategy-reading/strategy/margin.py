"""账户级保证金风控：开仓保证金占用 ≤ 账户权益 × ratio_limit（跨合约合计）。

数据来源（实盘 CppBroker）：
- 账户权益：CTP QueryTradingAccount → balance（静态权益）；查询失败回退全局静态配置；
- 每手保证金：优先合约配置 margin_per_lot（注册表/网页），未配置时用
  CTP QueryInstrumentMarginRate × 最新价 × 合约乘数 估算；
- 模拟通道无查询能力：权益/保证金率用静态配置与 margin_per_lot。

占用口径：Σ(各合约当前持仓手数 × 每手保证金) + 本次拟开仓手数 × 每手保证金。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# 每手保证金 = price × multiplier × rate；price 取近月腿最新价（由调用方提供）
def estimate_margin_per_lot(price: float, multiplier: float, rate: float) -> float:
    if price <= 0 or rate <= 0:
        return 0.0
    return price * multiplier * rate


class MarginManager:
    def __init__(self, *, account_equity: float = 1_000_000.0,
                 ratio_limit: float = 0.7) -> None:
        self.account_equity = float(account_equity or 0.0)   # 静态回退权益（元）
        self.ratio_limit = float(ratio_limit or 0.0)          # 上限比例（0.7 = 70%）
        self._ctp_balance: Optional[float] = None             # CTP 查询到的权益（优先）
        self._ctp_available: Optional[float] = None           # CTP 查询到的可用资金
        self._ctp_margin: Optional[float] = None              # CTP 查询到的当前占用保证金（CurrMargin）
        self._last_query_at = 0.0
        self._margin_rate: dict[str, float] = {}              # slug → 保证金率（缓存）

    # ─── 配置来源（由 web 层注入） ───
    set_margin_per_lot: Optional[Callable[[str], float]] = None   # slug → margin_per_lot
    latest_leg_price: Optional[Callable[[str], float]] = None     # slug → 近月腿最新价

    # ─── 权益 ───
    @property
    def equity(self) -> float:
        """账户权益：CTP 查询优先，失败回退静态配置。"""
        if self._ctp_balance and self._ctp_balance > 0:
            return self._ctp_balance
        return self.account_equity

    def available(self, occupied: float = 0.0) -> float:
        """可用资金：CTP 查询优先；无查询时按 权益 − 占用 估算。"""
        if self._ctp_available is not None:
            return self._ctp_available
        return max(self.equity - occupied, 0.0)

    async def refresh(self, broker, slugs: list[str]) -> None:
        """周期刷新：账户权益 + 各合约保证金率（实盘通道查询；失败静默保留旧值）。"""
        if broker is None or not getattr(broker, "is_real", False):
            return
        if not hasattr(broker, "query_account"):
            return
        try:
            acc = await broker.query_account()
            if acc and float(acc.get("balance", 0.0) or 0.0) > 0:
                self._ctp_balance = float(acc["balance"])
            if acc and acc.get("available") is not None:
                self._ctp_available = float(acc["available"] or 0.0)
            if acc and acc.get("margin") is not None:
                self._ctp_margin = float(acc["margin"] or 0.0)
        except Exception:  # noqa: BLE001
            pass
        if hasattr(broker, "query_margin_rate"):
            for slug in slugs:
                try:
                    mr = await broker.query_margin_rate(slug)
                    rate = max(float(mr.get("long_margin_rate", 0.0) or 0.0),
                               float(mr.get("short_margin_rate", 0.0) or 0.0))
                    if rate > 0:
                        self._margin_rate[slug] = rate
                except Exception:  # noqa: BLE001
                    pass
        self._last_query_at = time.time()

    async def refresh_account(self, broker) -> None:
        """开仓前实时刷新账户权益/可用（占比计算用；不刷各合约保证金率）。

        仅实盘通道生效；查询失败保留旧值（不因单次失败误放行/误拦截）。
        """
        if broker is None or not getattr(broker, "is_real", False):
            return
        if not hasattr(broker, "query_account"):
            return
        try:
            acc = await broker.query_account()
            if acc and float(acc.get("balance", 0.0) or 0.0) > 0:
                self._ctp_balance = float(acc["balance"])
            if acc and acc.get("available") is not None:
                self._ctp_available = float(acc["available"] or 0.0)
            if acc and acc.get("margin") is not None:
                self._ctp_margin = float(acc["margin"] or 0.0)
        except Exception:  # noqa: BLE001
            pass

    # ─── 每手保证金 ───
    def margin_per_lot(self, slug: str) -> float:
        """每手开仓保证金：配置 margin_per_lot 优先，未配置用 rate×价×乘数 估算。"""
        if self.set_margin_per_lot is not None:
            cfg = self.set_margin_per_lot(slug)
            if cfg and cfg > 0:
                return cfg
        rate = self._margin_rate.get(slug, 0.0)
        price = 0.0
        if self.latest_leg_price is not None:
            price = self.latest_leg_price(slug) or 0.0
        if price <= 0 or rate <= 0:
            return 0.0
        mult = self.set_multiplier(slug) if self.set_multiplier else 10.0
        return estimate_margin_per_lot(price, mult, rate)

    set_multiplier: Optional[Callable[[str], float]] = None     # slug → 合约乘数

    # ─── 占用与上限检查 ───
    def occupied(self, positions: dict[str, int]) -> float:
        """当前占用保证金：CTP 查询的真实保证金（CurrMargin）优先，
        未查到（模拟/查询失败）时回退 Σ(持仓手数 × 每手保证金)。
        """
        if self._ctp_margin and self._ctp_margin > 0:
            return self._ctp_margin
        return sum(self.margin_per_lot(slug) * hands
                   for slug, hands in positions.items() if hands > 0)

    def can_open(self, positions: dict[str, int], slug: str,
                 add_hands: int) -> tuple[bool, float]:
        """开/加仓前检查：占用 + 新增 ≤ 权益 × ratio_limit。

        占用优先 CTP 真实保证金（与 CTP 客户端占比一致）；新增按每手保证金估算。
        返回 (是否允许, 预计占用率)。占用率 > 1 表示超过权益（异常场景）。
        """
        per_lot = self.margin_per_lot(slug)
        total = self.occupied(positions) + per_lot * max(0, add_hands)
        ratio = total / self.equity if self.equity > 0 else 1.0
        allowed = ratio <= self.ratio_limit if self.ratio_limit > 0 else True
        return allowed, ratio

    def state_dict(self) -> dict:
        return {
            "equity": self.equity,
            "ratio_limit": self.ratio_limit,
            "occupied": self.occupied({}),   # 由调用方用实际持仓覆盖
            "margin_rate": dict(self._margin_rate),
        }
