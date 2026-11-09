"""交易日与时段规则（推荐文档 §1）。

- 夜盘数据按中国期货交易日映射到下一交易日；
- 集合竞价时段不参与交易；
- 策略不交易当月合约。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional


def trading_day_of(exchange_ts: str) -> str:
    """把交易所时间字符串映射到所属交易日（YYYY-MM-DD）。

    夜盘 20:55 之后（含次日凌晨）归属下一交易日。
    输入格式："20260506 22:40:16.500" 或 "2026-05-06 22:40:16"。
    """
    dt = parse_exchange_ts(exchange_ts)
    if dt is None:
        return ""
    # 夜盘：20:55 之后 → 下一交易日；凌晨（<03:00）也属于夜盘 → 下一交易日
    if dt.hour >= 20 or dt.hour < 3:
        d = dt.date() + timedelta(days=1)
        return d.isoformat()
    return dt.date().isoformat()


def parse_exchange_ts(s: str) -> Optional[datetime]:
    """解析交易所时间戳，支持 "20260506 22:40:16.500" 与 "2026-05-06 22:40:16"。"""
    s = s.strip()
    m = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s)
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, sec)


def in_call_auction(exchange_ts: str) -> bool:
    """是否处于集合竞价时段（不参与交易）。

    日盘集合竞价 08:55–09:00；夜盘集合竞价 20:55–21:00。
    """
    dt = parse_exchange_ts(exchange_ts)
    if dt is None:
        return False
    hhmm = dt.hour * 100 + dt.minute
    return (855 <= hhmm < 900) or (2055 <= hhmm < 2100)


def parse_contract_month(inst: str) -> Optional[date]:
    """解析合约月份（如 "OI611" → 2026-11-01；"ao2609" → 2026-09-01）。

    合约代码 = 品种前缀 + 数字：
      - 3 位（郑商所惯例，如 RM609/OI611）：首位为年份个位（2020+首位），后 2 位为月份；
      - 4 位（大商所/上期所，如 ao2609/j2609）：前 2 位年份、后 2 位月份。
    """
    m = re.search(r"(\d{3,4})$", inst)
    if not m:
        return None
    num = m.group(1)
    if len(num) == 3:   # 郑商所 RM609 → 2026-09；OI611 → 2026-11
        yy, mm = 2020 + int(num[0]), int(num[1:])
    else:               # 大商所/上期所 ao2609 → 2026-09
        yy, mm = 2000 + int(num[:2]), int(num[2:])
    if not (1 <= mm <= 12):
        return None
    return date(yy, mm, 1)
