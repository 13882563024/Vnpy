"""数据读取：replay ticks.jsonl（旧版 SDRTrader 采集的跨期合成 tick）。

每行一条已合成的跨期 tick：
{
  "t": "09:29:00.562",          # 事件时间（HH:MM:SS.mmm）
  "ts": 1786584540.562122,      # 事件时间戳（秒）
  "near": {"inst": "OI611", "last": ..., "bid": ..., "ask": ...,
           "volume": ..., "ts": "...", "trading_day": "2026-08-13", "recv_ns": ...},
  "far":  {同上},
  "spread": 179.5               # = near_mid - far_mid（与推荐文档口径一致）
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class LegQuote:
    """单腿报价快照。"""
    inst: str
    last: float
    bid: float
    ask: float
    volume: int
    ts: str = ""
    trading_day: str = ""
    recv_ns: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def width(self) -> float:
        return self.ask - self.bid


@dataclass
class SpreadTick:
    """一条跨期合成 tick。"""
    t: str                       # HH:MM:SS.mmm
    ts: float                    # 事件时间戳（秒）
    near: LegQuote
    far: LegQuote
    spread: float                # near_mid - far_mid
    recv_ns: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def near_mid(self) -> float:
        return self.near.mid

    @property
    def far_mid(self) -> float:
        return self.far.mid


def _parse_leg(d: dict) -> LegQuote:
    return LegQuote(
        inst=str(d.get("inst", "")),
        last=float(d.get("last", 0.0)),
        bid=float(d.get("bid", 0.0)),
        ask=float(d.get("ask", 0.0)),
        volume=int(d.get("volume", 0)),
        ts=str(d.get("ts", "")),
        trading_day=str(d.get("trading_day", "")),
        recv_ns=int(d.get("recv_ns", 0)),
    )


def iter_replay_ticks(path: Path) -> Iterator[SpreadTick]:
    """逐行读取 replay ticks.jsonl。"""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            near = _parse_leg(d.get("near") or {})
            far = _parse_leg(d.get("far") or {})
            yield SpreadTick(
                t=str(d.get("t", "")),
                ts=float(d.get("ts", 0.0)),
                near=near,
                far=far,
                spread=float(d.get("spread", near.mid - far.mid)),
                recv_ns=int(d.get("recv_ns", 0)),
            )


def load_replay_day(slug_dir: Path, date: str) -> list[SpreadTick]:
    """读取某个 slug 某交易日的全部 replay tick。"""
    p = slug_dir / "replay" / date / "ticks.jsonl"
    if not p.exists():
        return []
    return list(iter_replay_ticks(p))


def find_replay_dates(slug_dir: Path) -> list[str]:
    """列出该 slug 可用的 replay 交易日（升序）。"""
    replay_dir = slug_dir / "replay"
    if not replay_dir.exists():
        return []
    return sorted(d.name for d in replay_dir.iterdir()
                  if d.is_dir() and (d / "ticks.jsonl").exists())
