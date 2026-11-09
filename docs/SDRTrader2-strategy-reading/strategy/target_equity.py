"""目标权益全平（账户级止盈保护）：估算口径与触发判定。

用户确认的口径（**本地记账口径**）：

    预计平仓后权益 = 权益基数
                   + 当日已实现净盈亏（Σ Trade.net_pnl）
                   + Σ 各合约持仓按当前对手价平仓的净盈

    持仓净盈 = 方向 × (平仓对手价 − 建仓均价) × 乘数 × 手数
               − 该持仓「开 + 平」手续费

触发条件：`target_equity > 0` 且有持仓、**全部持仓合约均可估算**、预计权益 ≥ 目标
→ 所有持仓合约全部平仓，并锁成只平不开（同人工全平语义，手动「启动交易」恢复）。

口径说明：
- 权益基数：实盘 = CTP `balance`（`MarginManager.equity`，昨结算权益）；模拟 = 配置权益基数；
- 手续费与结算同口径（`strategy/fees.py` + `engine.trade_fee`：开 2 腿 + 平 2 腿，
  同一交易日开平按平今），且 `Trade.net_pnl` 已在平仓时一次性扣过开+平手续费，
  故已实现盈亏**不再重复计费**；
- 平仓对手价 = 平多 `near.bid − far.ask`、平空 `near.ask − far.bid`（与执行口径一致）；
- 跨日持仓的毛利以**建仓均价**为基准：柜台 `balance` 已按昨结算价重估过持仓，
  两者口径存在差异（详见 `docs/target-equity.md` §4）。

本模块是纯计算层（不访问 broker / 磁盘），便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PositionEstimate:
    """单合约持仓按对手价平仓的估算（由引擎 `estimate_position_exit` 提供）。"""
    slug: str
    side: str               # LONG / SHORT
    volume: int
    exit_price: float       # 平仓对手价（价差口径）
    gross_pnl: float        # 毛利（元）
    fee: float              # 该持仓「开 + 平」手续费合计（元）

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fee

    def to_dict(self) -> dict:
        return {
            "slug": self.slug, "side": self.side, "volume": self.volume,
            "exit_price": round(self.exit_price, 4),
            "gross_pnl": round(self.gross_pnl, 2),
            "fee": round(self.fee, 2),
            "net_pnl": round(self.net_pnl, 2),
        }


@dataclass
class EquityEstimate:
    """账户级「按对手价全平后预计权益」估算结果。"""
    base_equity: float = 0.0        # 权益基数（CTP balance / 配置权益）
    realized_pnl: float = 0.0       # 当日已实现净盈亏（Σ Trade.net_pnl）
    target: float = 0.0             # 目标权益（0 = 关闭）
    positions: list[PositionEstimate] = field(default_factory=list)
    unestimated: list[str] = field(default_factory=list)   # 无法估算的持仓合约（含原因）

    @property
    def has_position(self) -> bool:
        return bool(self.positions)

    @property
    def gross_pnl(self) -> float:
        return sum(p.gross_pnl for p in self.positions)

    @property
    def fee(self) -> float:
        return sum(p.fee for p in self.positions)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fee

    @property
    def estimable(self) -> bool:
        """全部持仓合约都可估算（任一不可估算 → 整体不判定，避免拿残缺数据平仓）。"""
        return not self.unestimated

    @property
    def exit_equity(self) -> float:
        return self.base_equity + self.realized_pnl + self.net_pnl

    @property
    def hit(self) -> bool:
        return (self.target > 0 and self.has_position and self.estimable
                and self.exit_equity >= self.target)

    def reason(self) -> str:
        return (f"目标权益 预计平仓后 {self.exit_equity:,.0f} ≥ 目标 {self.target:,.0f}"
                f"（权益基数 {self.base_equity:,.0f} + 当日已实现 {self.realized_pnl:+,.0f}"
                f" + 持仓毛利 {self.gross_pnl:+,.0f} − 开平手续费 {self.fee:,.0f}）")

    def to_dict(self) -> dict:
        return {
            "base_equity": round(self.base_equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "fee": round(self.fee, 2),
            "net_pnl": round(self.net_pnl, 2),
            "exit_equity": round(self.exit_equity, 2),
            "target": round(self.target, 2),
            "hit": self.hit,
            "estimable": self.estimable,
            "has_position": self.has_position,
            "positions": [p.to_dict() for p in self.positions],
            "unestimated": list(self.unestimated),
        }


def estimate_exit_equity(*, base_equity: float, realized_pnl: float,
                         positions: list[PositionEstimate] | None = None,
                         unestimated: list[str] | None = None,
                         target: float = 0.0) -> EquityEstimate:
    """汇总「按当前对手价全平后的预计权益」并判定是否达标。"""
    return EquityEstimate(
        base_equity=float(base_equity or 0.0),
        realized_pnl=float(realized_pnl or 0.0),
        target=float(target or 0.0),
        positions=list(positions or []),
        unestimated=list(unestimated or []),
    )
