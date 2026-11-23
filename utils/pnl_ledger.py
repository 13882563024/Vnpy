"""按价差记账的已实现收益台账：跨重启累计 + 当日。

只做一件事：把每笔已实现收益累加到 ``~/.vntrader/spread_realized.json``，
格式 ``{价差名: {"total": 累计, "day": "YYYY-MM-DD", "today": 当日}}``。

为什么不放在策略设置里：那是配置，重启/热更新会被当成参数保存；这里的是运行
结果。也不依赖成交流水（今晚实测 ``trades.csv`` 存在漏记），直接由策略在平仓
结算处记账，口径唯一。

任何读写失败都不抛给调用方——记账坏了不能把交易带崩。
"""
from __future__ import annotations

import json
from pathlib import Path


def _path() -> Path:
    from vnpy.trader.utility import get_file_path

    return Path(get_file_path("spread_realized.json"))


def read_all() -> dict:
    """读取完整本地文件，供只读界面一次构建所有行。"""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load(name: str, day: str) -> tuple[float, float]:
    """返回 (累计收益, 当日收益)；不是同一天的旧"当日"按 0 处理。"""
    row = read_all().get(name) or {}
    try:
        total = float(row.get("total", 0.0) or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    try:
        today = float(row.get("today", 0.0) or 0.0) if str(row.get("day", "")) == day else 0.0
    except (TypeError, ValueError):
        today = 0.0
    return total, today


def record(name: str, amount: float, day: str) -> tuple[float, float]:
    """记一笔已实现收益，返回台账里的 (累计, 当日)。"""
    data = read_all()
    row = dict(data.get(name) or {})
    try:
        total = float(row.get("total", 0.0) or 0.0) + float(amount)
    except (TypeError, ValueError):
        total = float(amount)
    try:
        today = (float(row.get("today", 0.0) or 0.0) if str(row.get("day", "")) == day else 0.0)
        today += float(amount)
    except (TypeError, ValueError):
        today = float(amount)
    data[name] = {"total": round(total, 2), "day": day, "today": round(today, 2)}
    _write(data)
    return total, today
