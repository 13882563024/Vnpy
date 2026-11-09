"""交易日、集合竞价、合约月份与交易时段。

设计取舍（与方案 11.2 第 4 条一致）：**不依赖交易日历文件**，用「自然日 × 5/7」
近似交易日数，换月判断用合约月份，与参考实现同口径。唯一比参考实现多做的一点：
夜盘映射到下一交易日时跳过周末（周五夜盘属于下周一），否则周五夜盘的
``open_trading_day`` 会落在周六，导致平今/平昨与持仓天数都算错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# 夜盘起点（含）：20:55 之后的成交归入下一交易日
NIGHT_START_HOUR = 20
# 凌晨仍属夜盘的截止小时（不含）
NIGHT_END_HOUR = 3


# ---------------------------------------------------------------- 交易日
def _roll_forward_to_weekday(day: date) -> date:
    """把落在周末的日期推到下一个周一（节假日仍需人工处理）。"""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def trading_day_of(moment: datetime) -> date:
    """把自然时间映射到所属交易日。

    夜盘（20:55 之后到次日凌晨 3:00 前）归入下一交易日，并跳过周末。
    """
    if moment.hour >= NIGHT_START_HOUR or moment.hour < NIGHT_END_HOUR:
        return _roll_forward_to_weekday(moment.date() + timedelta(days=1))
    return _roll_forward_to_weekday(moment.date())


def trading_day_str(moment: datetime) -> str:
    return trading_day_of(moment).isoformat()


def parse_trading_day(text: str) -> date | None:
    """``"2026-11-02"`` → ``date``；无效返回 None。"""
    try:
        return date.fromisoformat(str(text).strip())
    except (ValueError, TypeError):
        return None


def trading_days_between(start: str, end: str) -> int | None:
    """两个交易日之间的近似交易日数（自然日 × 5/7）。"""
    first, second = parse_trading_day(start), parse_trading_day(end)
    if first is None or second is None:
        return None
    return int((second - first).days * 5 / 7)


# ---------------------------------------------------------------- 集合竞价
def in_call_auction(moment: datetime) -> bool:
    """是否处于集合竞价时段（日盘 08:55–09:00、夜盘 20:55–21:00），不报单。"""
    hhmm = moment.hour * 100 + moment.minute
    return (855 <= hhmm < 900) or (2055 <= hhmm < 2100)


# ---------------------------------------------------------------- 合约月份
def parse_contract_month(symbol: str) -> date | None:
    """解析合约进入当月的日期。

    合约代码 = 品种前缀 + 数字：
      - 3 位（郑商所惯例，如 ``RM609`` / ``OI611``）：首位为年份个位，后 2 位为月份；
      - 4 位（大商所/上期所，如 ``ao2609`` / ``j2609``）：前 2 位年、后 2 位月。

    入参可以是裸合约代码，也可以是 ``OI611.CZCE`` 这样的 vt_symbol（后缀会被去掉）。
    """
    text = str(symbol or "").strip().split(".", 1)[0]
    match = re.search(r"(\d{3,4})$", text)
    if not match:
        return None
    number = match.group(1)
    if len(number) == 3:
        year, month = 2020 + int(number[0]), int(number[1:])
    else:
        year, month = 2000 + int(number[:2]), int(number[2:])
    if not 1 <= month <= 12:
        return None
    return date(year, month, 1)


def days_to_contract_month(symbol: str, trading_day: str) -> int | None:
    """距合约进入当月的自然日数（``OI611`` 在 2026-10-20 → 12 天）。"""
    month = parse_contract_month(symbol)
    current = parse_trading_day(trading_day)
    if month is None or current is None:
        return None
    return (month - current).days


def trading_days_to_contract(symbol: str, trading_day: str) -> int | None:
    """距合约进入当月的近似交易日数（自然日 × 5/7），用于 REC / REX 判定。"""
    days = days_to_contract_month(symbol, trading_day)
    if days is None:
        return None
    return int(days * 5 / 7)


def next_trading_day_enters_month(symbol: str, trading_day: str) -> bool:
    """下一交易日该合约是否进入当月（进入则整仓强平，REX）。"""
    days = days_to_contract_month(symbol, trading_day)
    if days is None:
        return False
    # 下一交易日的自然日跨度：周五（weekday=4）到下周一跨 3 个自然日
    current = parse_trading_day(trading_day)
    step = 3 if current is not None and current.weekday() == 4 else 1
    return days - step <= 0


# ---------------------------------------------------------------- 交易时段
SESSION_TEMPLATES: dict[str, dict[str, list[str]]] = {
    # 日盘：09:00-10:15 / 10:30-11:30 / 13:30-15:00
    "day_only": {"day": ["09:00-10:15", "10:30-11:30", "13:30-15:00"], "night": []},
    # 夜盘到 23:00（多数农产品/黑色）
    "night_2300": {
        "day": ["09:00-10:15", "10:30-11:30", "13:30-15:00"],
        "night": ["21:00-23:00"],
    },
    # 夜盘到 01:00（多数商品）
    "night_0100": {
        "day": ["09:00-10:15", "10:30-11:30", "13:30-15:00"],
        "night": ["21:00-01:00"],
    },
    # 夜盘到 02:30（有色/贵金属/能源）
    "night_0230": {
        "day": ["09:00-10:15", "10:30-11:30", "13:30-15:00"],
        "night": ["21:00-02:30"],
    },
    # 股指/国债
    "index_futures": {"day": ["09:30-11:30", "13:00-15:00"], "night": []},
    # 不限制（集合竞价仍会拦截）
    "always": {"day": ["00:00-24:00"], "night": []},
}

DEFAULT_SESSION = "night_2300"


def _parse_segment(text: str) -> tuple[int, int] | None:
    """"HH:MM-HH:MM" → (起始分钟, 结束分钟)；结束 <= 起始表示跨零点。"""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", text)
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = (int(group) for group in match.groups())
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def _parse_segments(items: list[str]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    for item in items:
        parsed = _parse_segment(item)
        if parsed is not None:
            segments.append(parsed)
    return segments


@dataclass
class TradingSessions:
    """交易时段：模板名或显式段列表。

    ``spec`` 支持三种写法：

    - 模板名：``"night_2300"`` / ``"day_only"`` / ``"always"``
    - 显式：``"day:09:00-10:15,10:30-11:30;night:21:00-23:00"``
    - 空串：不限制（仍拦截集合竞价）
    """

    spec: str = ""
    segments: list[tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.segments = self._build(self.spec)

    @staticmethod
    def _build(spec: str) -> list[tuple[int, int]]:
        text = str(spec or "").strip()
        if not text:
            return []
        template = SESSION_TEMPLATES.get(text.lower())
        if template is not None:
            return _parse_segments(template["day"] + template["night"])
        if ":" in text and "-" in text:
            raw: list[str] = []
            for chunk in re.split(r"[;]", text):
                _, _, body = chunk.partition(":")
                raw.extend(part for part in body.split(",") if part.strip())
            return _parse_segments(raw)
        return []

    @property
    def unrestricted(self) -> bool:
        return not self.segments

    def is_open(self, moment: datetime) -> bool:
        """是否处于可交易时段（不含集合竞价）。"""
        if in_call_auction(moment):
            return False
        if not self.segments:
            return True
        minutes = moment.hour * 60 + moment.minute
        for start, end in self.segments:
            if start <= end:
                if start <= minutes < end:
                    return True
            else:      # 跨零点（如 21:00-01:00）
                if minutes >= start or minutes < end:
                    return True
        return False

    def describe(self) -> str:
        if not self.segments:
            return "不限时段"
        parts = []
        for start, end in self.segments:
            parts.append(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}")
        return " / ".join(parts)
