"""风控：订单失败熔断（规划方案 §6.4 / BMRTrader2 设计 §11.1）。

- 连续发单失败计数 consecutive_failures：REJECTED / TIMEOUT → +1，TRADED → 复位；
  发单请求本身抛出异常同样 +1；
- 达到阈值（默认 10 次，可配置）→ 触发熔断：该合约策略全部停手（含平仓单），
  允许撤单与用户控制指令（人工操作优先于熔断）；
- 熔断状态持久化（state.json，重启不丢失）；
- 恢复：人工确认复位（reset），复位后计数清零、恢复正常决策。
其余风控维度暂缓，接口预留（组合级敞口/当日回撤熔断等）。
"""
from __future__ import annotations

from core.persistence import Store


class RiskManager:
    def __init__(self, slug: str, store: Store, threshold: int = 4) -> None:
        self.slug = slug
        self.store = store
        self.threshold = threshold
        self.consecutive_failures = 0
        self.breaker_open = False
        self._load()

    # ---- 持久化（state.json 子项） ----
    def _load(self) -> None:
        state = self.store.read_json(self.slug, "state.json", {}) or {}
        risk = state.get("risk", {}) or {}
        self.consecutive_failures = int(risk.get("consecutive_failures", 0))
        self.breaker_open = bool(risk.get("breaker_open", False))

    def _save(self) -> None:
        state = self.store.read_json(self.slug, "state.json", {}) or {}
        state["risk"] = {
            "consecutive_failures": self.consecutive_failures,
            "breaker_open": self.breaker_open,
        }
        self.store.write_json(self.slug, "state.json", state)

    # ---- 事件 ----
    def on_order_failure(self) -> bool:
        """发单失败（REJECTED / TIMEOUT / 发单异常）。返回是否本次触发熔断。"""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            self.breaker_open = True
            self._save()
            return True
        self._save()
        return False

    def on_traded(self) -> None:
        """TRADED → 复位失败计数。"""
        self.consecutive_failures = 0
        self._save()

    def reset(self) -> None:
        """人工复位熔断。"""
        self.consecutive_failures = 0
        self.breaker_open = False
        self._save()

    # ---- 查询 ----
    @property
    def open(self) -> bool:
        return self.breaker_open

    def state_dict(self) -> dict:
        return {
            "consecutive_failures": self.consecutive_failures,
            "breaker_open": self.breaker_open,
            "threshold": self.threshold,
        }
