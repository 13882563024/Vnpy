"""统计窗口：最近 N 分钟 spread 均值、最近 M 分钟 spread 标准差（推荐文档 §2）。

- StatWindow：滚动时间窗口（按秒滑动），增量维护 sum/sum_sq，均摊 O(1)；
- KlineStats：K 线口径统计（对齐 BMR2 set_live 模式）——统计基准为 K 线序列，
  决策仍由引擎每 tick 实时触发，不等 K 线闭合。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from market.kline import KlineAggregator


class RollingWindow:
    """按秒的时间窗口，支持均值与标准差。"""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._q: deque[tuple[float, float]] = deque()   # (ts, spread)
        self._sum = 0.0
        self._sum_sq = 0.0

    def clear(self) -> None:
        self._q.clear()
        self._sum = 0.0
        self._sum_sq = 0.0

    def push(self, ts: float, value: float) -> None:
        """追加一条；ts 必须单调不减。"""
        self._q.append((ts, value))
        self._sum += value
        self._sum_sq += value * value
        self._expire(ts)

    def _expire(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._q and self._q[0][0] < cutoff:
            _, v = self._q.popleft()
            self._sum -= v
            self._sum_sq -= v * v

    def mean(self) -> Optional[float]:
        n = len(self._q)
        if n == 0:
            return None
        return self._sum / n

    def std(self, ddof: int = 1) -> Optional[float]:
        """样本标准差（ddof=1）。"""
        n = len(self._q)
        if n <= ddof:
            return None
        m = self._sum / n
        var = max(self._sum_sq / n - m * m, 0.0)
        return (var * n / (n - ddof)) ** 0.5 if ddof == 1 else var ** 0.5

    def __len__(self) -> int:
        return len(self._q)


@dataclass
class StatWindow:
    """N 分钟均值 + M 分钟标准差（滚动时间窗口，按秒滑动）。"""
    n_seconds: float
    m_seconds: float
    _mean_w: RollingWindow = field(init=False)
    _sd_w: RollingWindow = field(init=False)

    def __post_init__(self) -> None:
        self._mean_w = RollingWindow(self.n_seconds)
        self._sd_w = RollingWindow(self.m_seconds)

    def push(self, ts: float, spread: float) -> None:
        self._mean_w.push(ts, spread)
        self._sd_w.push(ts, spread)

    def mean(self) -> Optional[float]:
        return self._mean_w.mean()

    def sd(self) -> Optional[float]:
        return self._sd_w.std(ddof=1)

    def ready(self) -> bool:
        m, s = self.mean(), self.sd()
        return m is not None and s is not None and s > 0

    def clear(self) -> None:
        self._mean_w.clear()
        self._sd_w.clear()


class KlineStats:
    """m1 K 线口径统计（用户口径：最近 (N-1) 条闭合 K 线 + 1 条实时 K 线）。

    - 固定聚合 1 分钟 K 线（m1）；
    - mean = 最近 (N-1) 条已闭合 K 线收盘价 + 当前未闭合（实时）K 线，共 N 条；
    - sd   = 最近 (M-1) 条已闭合 K 线收盘价 + 当前未闭合（实时）K 线，共 M 条；
    - 实时 K 线纳入统计 → 统计量随实时价连续变化（确保实时性，BMR2 set_live 模式）；
    - 决策由引擎每 tick 触发（不等 K 线闭合）。
    推荐文档 N3/M30 在 m1 K 线下即 N=3 条、M=30 条（含实时）。
    """

    def __init__(self, n_roots: int = 3, m_roots: int = 30,
                 period_minutes: int = 1) -> None:
        self.agg = KlineAggregator(period_minutes=period_minutes)
        self.n_roots = max(1, n_roots)
        self.m_roots = max(2, m_roots)
        # K 线闭合回调（对齐 BMR2：闭合 K 线入历史 + 落盘 klines_cache.json）。
        # 由引擎挂 _record_kline；None 表示不消费。
        self.on_kline_closed = None

    def push(self, ts: float, spread: float) -> None:
        """喂入一个 tick（更新 m1 K 线聚合）。闭合时触发 on_kline_closed 回调。"""
        closed = self.agg.update(spread, ts)
        if closed is not None and self.on_kline_closed is not None:
            self.on_kline_closed(closed)

    def _window(self, roots: int) -> list[float]:
        """最近 (roots-1) 条已闭合 K 线收盘价 + 当前实时价 = roots 条实时窗口。"""
        if roots <= 0:
            return []
        closes = self.agg.closes()
        base = closes[-(roots - 1):] if roots > 1 else []
        live = self.agg.live_close()
        if live is not None:
            base = base + [live]
        return base[-roots:]

    def mean(self) -> Optional[float]:
        """最近 N 条（含实时 K 线）收盘价均值。"""
        w = self._window(self.n_roots)
        if not w:
            return None
        return sum(w) / len(w)

    def sd(self, ddof: int = 1) -> Optional[float]:
        """最近 M 条（含实时 K 线）收盘价标准差。"""
        w = self._window(self.m_roots)
        if len(w) <= ddof:
            return None
        m = sum(w) / len(w)
        var = sum((x - m) ** 2 for x in w) / max(len(w) - 1, 1)
        return var ** 0.5

    def ready(self) -> bool:
        m, s = self.mean(), self.sd()
        return m is not None and s is not None and s > 0

    def live_close(self) -> Optional[float]:
        """当前未闭合 K 线实时价（图表/诊断用）。"""
        return self.agg.live_close()

    def clear(self) -> None:
        self.agg.reset()
