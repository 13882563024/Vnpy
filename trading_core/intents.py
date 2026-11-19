"""可重放的报单意图，不执行 I/O 以外的交易动作；同一决策重试不能生成第二笔请求。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite

from .journal import Journal, Outcome


@dataclass(frozen=True)
class OrderIntent:
    account: str
    day: str
    strategy: str
    decision_id: str  # 调用方的稳定决策号，重试必须复用
    tier_id: str
    purpose: str
    symbol: str
    side: str
    lots: int
    price: float
    config_version: str
    reason: dict

    def reserve(self, journal: Journal) -> Outcome:
        """先持久化。仅 NEW 可交下一道门禁；不是报单许可或柜台受理凭证。

        进程崩溃后重复请求返回 DUPLICATE，不能据此重发。需另查柜台与本地关联。
        """
        if self.purpose not in {'open', 'add', 'take_profit', 'risk_exit'}:
            raise ValueError('未知报单用途')
        if self.side not in {'buy', 'sell'} or type(self.lots) is not int or self.lots <= 0:
            raise ValueError('无效方向或组合手数')
        if not isfinite(self.price) or not isinstance(self.reason, dict) or not self.reason:
            raise ValueError('限价或报单依据无效')
        if not all(isinstance(v, str) and v.strip() for v in (
                self.tier_id, self.symbol, self.config_version)):
            raise ValueError('缺少层、合约或配置版本')
        return journal.append(self.account, self.day, 'order_intent',
                              (self.strategy, self.decision_id), asdict(self))[1]
