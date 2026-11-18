"""定时代理的计划模型：解析配置、判断到点、按天去重。

**纯逻辑，不依赖 Qt 与柜台**，便于单测：真正的连接/核对/启动由
``spreadconsole.schedule_agent.ScheduleAgent`` 执行。

配置示例（``config/schedule.json``）::

    {
      "enabled": true,
      "tasks": [
        {"time": "08:50", "days": [1, 2, 3, 4, 5], "action": "connect",   "note": "连接 CTP"},
        {"time": "08:55", "days": [1, 2, 3, 4, 5], "action": "reconcile", "note": "核对并纠正"},
        {"time": "08:58", "days": [1, 2, 3, 4, 5], "action": "start",     "note": "开始交易"}
      ]
    }

``days`` 用 ISO 周几：1=周一 … 7=周日。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.json"

# 动作白名单：新增动作必须同时改 ScheduleAgent 的分派
ACTION_PURGE = "purge"
ACTION_CONNECT = "connect"
ACTION_RECONCILE = "reconcile"
ACTION_START = "start"
ACTION_STOP = "stop"
ACTIONS = (ACTION_PURGE, ACTION_CONNECT, ACTION_RECONCILE, ACTION_START, ACTION_STOP)

ACTION_LABELS = {
    ACTION_PURGE: "清理隔夜缓存",
    ACTION_CONNECT: "连接 CTP",
    ACTION_RECONCILE: "核对并纠正",
    ACTION_START: "开始交易",
    ACTION_STOP: "停止交易",
}

WEEKDAY_LABELS = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
TRADING_DAYS = (1, 2, 3, 4, 5)

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_time(text: str) -> int | None:
    """"HH:MM" → 当天的分钟数；格式非法返回 None。"""
    match = _TIME_RE.match(str(text or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def format_time(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def parse_days(text: str) -> tuple[int, ...] | None:
    """解析周几输入："1-5"、"1,3,5"、"1-5,7" → (1,2,3,4,5)。非法返回 None。

    也接受中文"一,三,五"。
    """
    raw = str(text or "").strip().replace("，", ",")
    if not raw:
        return None
    days: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        alias = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "七": 7}
        if chunk in alias:
            days.append(alias[chunk])
            continue
        if "-" in chunk:
            start_text, _, end_text = chunk.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                return None
            if not (1 <= start <= 7 and 1 <= end <= 7) or start > end:
                return None
            days.extend(range(start, end + 1))
            continue
        try:
            day = int(chunk)
        except ValueError:
            return None
        if not 1 <= day <= 7:
            return None
        days.append(day)
    unique = sorted(set(days))
    return tuple(unique) if unique else None


def format_days(days: tuple[int, ...]) -> str:
    """(1,2,3,4,5) → "1-5"；不连续的用逗号。"""
    ordered = sorted(set(int(day) for day in days if 1 <= int(day) <= 7))
    if not ordered:
        return ""
    parts: list[str] = []
    start = previous = ordered[0]
    for day in ordered[1:]:
        if day == previous + 1:
            previous = day
            continue
        parts.append(f"{start}-{previous}" if previous > start else str(start))
        start = previous = day
    parts.append(f"{start}-{previous}" if previous > start else str(start))
    return ",".join(parts)


@dataclass(frozen=True)
class ScheduleTask:
    """一条定时任务。``time`` 与 ``days`` 一起决定触发时刻。"""

    time: str
    days: tuple[int, ...]
    action: str
    enabled: bool = True
    note: str = ""

    @property
    def minute_of_day(self) -> int | None:
        return parse_time(self.time)

    @property
    def normalized_time(self) -> str:
        minute = self.minute_of_day
        return format_time(minute) if minute is not None else str(self.time)

    @property
    def key(self) -> str:
        """稳定标识：同一时间/动作视为同一条（用于当天去重）。"""
        return f"{self.normalized_time}|{self.action}|{','.join(str(d) for d in self.days)}"

    def describe(self) -> str:
        minute = self.minute_of_day
        if minute is None:
            return "时间格式非法"
        days = "".join(WEEKDAY_LABELS.get(day, "?") for day in self.days)
        label = ACTION_LABELS.get(self.action, self.action)
        return f"周{days or '—'} {format_time(minute)} {label}"

    def to_dict(self) -> dict:
        return {
            "time": self.normalized_time,
            "days": list(self.days),
            "action": self.action,
            "enabled": bool(self.enabled),
            "note": self.note,
        }

    @staticmethod
    def from_dict(raw: dict) -> "ScheduleTask | None":
        if not isinstance(raw, dict):
            return None
        action = str(raw.get("action", "")).strip().lower()
        if action not in ACTIONS:
            return None
        if parse_time(raw.get("time", "")) is None:
            return None
        raw_days = raw.get("days", list(TRADING_DAYS))
        if isinstance(raw_days, int):
            raw_days = [raw_days]
        days: list[int] = []
        for item in raw_days or []:
            try:
                day = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= day <= 7 and day not in days:
                days.append(day)
        if not days:
            return None
        return ScheduleTask(
            time=format_time(parse_time(raw["time"])),      # type: ignore[arg-type]
            days=tuple(sorted(days)),
            action=action,
            enabled=bool(raw.get("enabled", True)),
            note=str(raw.get("note", "") or ""),
        )


@dataclass
class SchedulePlan:
    """一组定时任务 + 当天已执行记录（去重靠后者）。"""

    tasks: list[ScheduleTask] = field(default_factory=list)
    enabled: bool = True
    # (日期, task.key) → 执行时刻；只保留当天，跨天自动清理
    _done: dict[tuple[str, str], datetime] = field(default_factory=dict)

    # ------------------------------------------------------------ 读写
    @staticmethod
    def default_tasks() -> list[ScheduleTask]:
        """默认计划：08:49 清理隔夜缓存 → 08:50 连接 → 08:55 核对纠正 → 08:58 开始交易。

        清理必须排在连接之前：CTP 委托当日有效，跨日不重启时引擎里还留着昨天的
        "未成交"记录，不清掉就会让策略误判"止盈单还在挂"而不再补单。
        """
        return [
            ScheduleTask("08:49", TRADING_DAYS, ACTION_PURGE, True, "清理隔夜委托缓存"),
            ScheduleTask("08:50", TRADING_DAYS, ACTION_CONNECT, True, "连接 CTP"),
            ScheduleTask("08:55", TRADING_DAYS, ACTION_RECONCILE, True, "核对并纠正本地账"),
            ScheduleTask("08:58", TRADING_DAYS, ACTION_START, True, "开始交易"),
        ]

    @classmethod
    def default(cls) -> "SchedulePlan":
        return cls(tasks=cls.default_tasks(), enabled=True)

    @classmethod
    def load(cls, path: Path) -> "SchedulePlan":
        """读配置；文件缺失或损坏时返回默认计划（不抛异常，避免拖垮 GUI 启动）。"""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls.default()
        if not isinstance(raw, dict):
            return cls.default()
        tasks: list[ScheduleTask] = []
        for item in raw.get("tasks", []) or []:
            task = ScheduleTask.from_dict(item)
            if task is not None:
                tasks.append(task)
        return cls(tasks=tasks or cls.default_tasks(),
                   enabled=bool(raw.get("enabled", True)))

    def save(self, path: Path) -> None:
        payload = {"enabled": bool(self.enabled),
                   "tasks": [task.to_dict() for task in self.tasks]}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    # ------------------------------------------------------------ 判断
    def _prune(self, now: datetime) -> None:
        today = now.date().isoformat()
        for key in [key for key in self._done if key[0] != today]:
            self._done.pop(key, None)

    def due(self, now: datetime) -> list[ScheduleTask]:
        """本分钟内应当执行、且今天尚未执行过的任务。"""
        if not self.enabled:
            return []
        self._prune(now)
        today = now.date().isoformat()
        minute = now.hour * 60 + now.minute
        weekday = now.isoweekday()
        result: list[ScheduleTask] = []
        for task in self.tasks:
            if not task.enabled or task.minute_of_day != minute:
                continue
            if weekday not in task.days:
                continue
            if (today, task.key) in self._done:
                continue
            result.append(task)
        return result

    def mark_done(self, task: ScheduleTask, now: datetime) -> None:
        self._done[(now.date().isoformat(), task.key)] = now

    def next_at(self, now: datetime) -> tuple[datetime, ScheduleTask] | None:
        """下一次将触发的任务（用于界面提示），最多向后找 8 天。"""
        if not self.enabled:
            return None
        best: tuple[datetime, ScheduleTask] | None = None
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            for task in self.tasks:
                minute = task.minute_of_day
                if not task.enabled or minute is None:
                    continue
                if day.isoweekday() not in task.days:
                    continue
                moment = day.replace(hour=minute // 60, minute=minute % 60,
                                     second=0, microsecond=0)
                if moment <= now:
                    continue
                if best is None or moment < best[0]:
                    best = (moment, task)
        return best
