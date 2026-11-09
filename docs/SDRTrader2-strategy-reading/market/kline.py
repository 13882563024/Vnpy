"""K 线聚合器（对齐 BMR2 market/kline.py）：tick → K 线。

- 按周期对齐时间桶（如 5 分钟）；
- 维护已闭合 K 线历史（closes 序列）与当前未闭合 K 线（current，实时价）；
- 决策不需要等 K 线闭合：统计窗口用「历史闭合 + 当前实时」（BMR2 set_live 模式）。
"""
from __future__ import annotations

from typing import Optional


class KlineAggregator:
    def __init__(self, period_minutes: int = 5, max_history: int = 2000) -> None:
        self.period_minutes = period_minutes
        self.period_seconds = period_minutes * 60
        self.max_history = max_history
        self.closed: list[dict] = []          # 已闭合 K 线（升序）
        self.current: Optional[dict] = None   # 当前未闭合 K 线

    # ---------- 时间桶对齐 ----------
    @staticmethod
    def _align(ts: float, period: int) -> int:
        return int(ts // period) * period

    # ---------- 喂入 tick ----------
    def update(self, price: float, ts: float) -> Optional[dict]:
        """喂入一个 tick。若触发 K 线闭合，返回已闭合的 K 线（否则 None）。"""
        bucket = self._align(ts, self.period_seconds)
        if self.current is None or self.current["ts"] != bucket:
            # 上一根闭合
            closed = self.current
            if closed is not None:
                # 同 ts 幂等覆盖（注入历史与实时闭合可能重叠，防同分钟重复柱）
                if self.closed and self.closed[-1]["ts"] == closed["ts"]:
                    self.closed[-1] = closed
                else:
                    self.closed.append(closed)
                if len(self.closed) > self.max_history:
                    self.closed = self.closed[-self.max_history:]
            self.current = {"ts": bucket, "open": price, "high": price,
                            "low": price, "close": price, "volume": 1}
            return closed
        c = self.current
        c["high"] = max(c["high"], price)
        c["low"] = min(c["low"], price)
        c["close"] = price
        c["volume"] += 1
        return None

    # ---------- 查询 ----------
    def closes(self) -> list[float]:
        """已闭合 K 线收盘价序列（升序）。"""
        return [c["close"] for c in self.closed]

    def live_close(self) -> Optional[float]:
        """当前未闭合 K 线的实时价。"""
        return self.current["close"] if self.current else None

    def snapshot(self) -> Optional[dict]:
        return dict(self.current) if self.current else None

    # ---------- 强制闭合（对齐 BMR2 kline.py：停采/断流兜底） ----------
    def flush(self) -> Optional[dict]:
        """立即闭合当前未闭合 K 线（停止采集/程序退出时调用），
        返回该 K 线并入历史；无未闭合 K 线时返回 None。"""
        closed = self.current
        if closed is not None:
            self.closed.append(closed)
            if len(self.closed) > self.max_history:
                self.closed = self.closed[-self.max_history:]
            self.current = None
        return closed

    def flush_if_stale(self, now: float, grace: float) -> Optional[dict]:
        """收盘/断流兜底：当前未闭合 K 线的 bucket 窗口（ts+period）结束后
        超过 grace 秒仍无新 tick → 强制闭合返回该 K 线；否则返回 None。"""
        if self.current is None:
            return None
        if now < self.current["ts"] + self.period_seconds + grace:
            return None
        return self.flush()

    def reset(self) -> None:
        self.closed = []
        self.current = None
