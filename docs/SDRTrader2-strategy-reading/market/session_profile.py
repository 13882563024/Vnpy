"""
交易时段配置 — 简化自 pyctick
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionSegment:
    """交易时段片段: (HH:MM:SS, HH:MM:SS)"""
    start: str
    end: str


@dataclass
class SessionProfile:
    """交易时段模板"""
    name: str
    day_segments: list[SessionSegment] = field(default_factory=list)
    night_segments: list[SessionSegment] = field(default_factory=list)
    break_periods: list[SessionSegment] = field(default_factory=list)


# ── 6 种内置交易时段模板 ──

# 股指期货 (IF/IH/IC/IM + 国债 T/TF/TS)
PROFILE_INDEX_FUTURES = SessionProfile(
    name="index_futures",
    day_segments=[
        SessionSegment("09:30:00", "11:30:00"),
        SessionSegment("13:00:00", "15:00:00"),
    ],
    night_segments=[],
    break_periods=[
        SessionSegment("11:30:00", "13:00:00"),
    ],
)

# 国债期货 (TL)
PROFILE_BOND_FUTURES = SessionProfile(
    name="bond_futures",
    day_segments=[
        SessionSegment("09:30:00", "11:30:00"),
        SessionSegment("13:00:00", "15:15:00"),
    ],
    night_segments=[],
    break_periods=[
        SessionSegment("11:30:00", "13:00:00"),
    ],
)

# 夜盘到 02:30 (有色/贵金属/能源)
PROFILE_NIGHT_0230 = SessionProfile(
    name="night_0230",
    day_segments=[
        SessionSegment("09:00:00", "10:15:00"),
        SessionSegment("10:30:00", "11:30:00"),
        SessionSegment("13:30:00", "15:00:00"),
    ],
    night_segments=[
        SessionSegment("21:00:00", "02:30:00"),
    ],
    break_periods=[
        SessionSegment("10:15:00", "10:30:00"),
        SessionSegment("11:30:00", "13:30:00"),
        SessionSegment("02:30:00", "09:00:00"),
    ],
)

# 夜盘到 01:00 (多数商品)
PROFILE_NIGHT_0100 = SessionProfile(
    name="night_0100",
    day_segments=[
        SessionSegment("09:00:00", "10:15:00"),
        SessionSegment("10:30:00", "11:30:00"),
        SessionSegment("13:30:00", "15:00:00"),
    ],
    night_segments=[
        SessionSegment("21:00:00", "01:00:00"),
    ],
    break_periods=[
        SessionSegment("10:15:00", "10:30:00"),
        SessionSegment("11:30:00", "13:30:00"),
        SessionSegment("01:00:00", "09:00:00"),
    ],
)

# 夜盘到 23:00 (少数品种)
PROFILE_NIGHT_2300 = SessionProfile(
    name="night_2300",
    day_segments=[
        SessionSegment("09:00:00", "10:15:00"),
        SessionSegment("10:30:00", "11:30:00"),
        SessionSegment("13:30:00", "15:00:00"),
    ],
    night_segments=[
        SessionSegment("21:00:00", "23:00:00"),
    ],
    break_periods=[
        SessionSegment("10:15:00", "10:30:00"),
        SessionSegment("11:30:00", "13:30:00"),
        SessionSegment("23:00:00", "09:00:00"),
    ],
)

# 无夜盘 (农产品/其他)
PROFILE_DAY_ONLY = SessionProfile(
    name="day_only",
    day_segments=[
        SessionSegment("09:00:00", "10:15:00"),
        SessionSegment("10:30:00", "11:30:00"),
        SessionSegment("13:30:00", "15:00:00"),
    ],
    night_segments=[],
    break_periods=[
        SessionSegment("10:15:00", "10:30:00"),
        SessionSegment("11:30:00", "13:30:00"),
    ],
)


# ── 品种前缀 → 时段模板映射 ──

def extract_prefix(instrument: str) -> str:
    """从合约代码中提取品种字母前缀并转为小写。例: 'ag2406' → 'ag'"""
    prefix: list[str] = []
    for c in instrument:
        if c.isalpha():
            prefix.append(c.lower())
        else:
            break
    return "".join(prefix)


def is_in_trading_session(instrument_id: str, time_hms: str) -> bool:
    """检查给定时间(HH:MM:SS)是否在该合约的交易时段内（不含周末归属判断）。"""
    prefix = extract_prefix(instrument_id)
    profile = lookup_session_profile(prefix)

    # 检查日盘时段
    for seg in profile.day_segments:
        if seg.start <= time_hms < seg.end:
            return True

    # 检查夜盘时段
    for seg in profile.night_segments:
        if seg.start <= seg.end:
            if seg.start <= time_hms < seg.end:
                return True
        else:
            # 跨越午夜：21:00:00 ~ 23:59:59 或 00:00:00 ~ 02:30:00
            if time_hms >= seg.start or time_hms < seg.end:
                return True

    return False


PREFIX_TO_PROFILE: dict[str, SessionProfile] = {}

def _register(prefixes: list[str], profile: SessionProfile) -> None:
    for p in prefixes:
        PREFIX_TO_PROFILE[p] = profile

# night_0230
_register(
    ["au", "ag", "al", "zn", "pb", "ni", "sc", "lu", "fu"],
    PROFILE_NIGHT_0230,
)
# night_0100
_register(
    [
        "cu", "sn", "hc", "jm", "sf", "sm",
        "a", "b", "m", "cs", "rr",
        "ma", "fg", "ur", "eb", "eg",
        "pp", "l", "v", "pvc", "wr", "sp",
        "cj", "cy", "rm", "pm",
        "wh", "ri", "lr", "rs", "jr",
        "ao", "bc",
    ],
    PROFILE_NIGHT_0100,
)
# night_2300
_register(
    ["i", "cf", "c", "p", "sa", "ta", "rb", "oi", "j", "y", "ru", "sr", "nr"],
    PROFILE_NIGHT_2300,
)
# index_futures
_register(
    ["if", "ih", "ic", "im", "t", "tf", "ts"],
    PROFILE_INDEX_FUTURES,
)
# bond_futures
_register(["tl"], PROFILE_BOND_FUTURES)
# day_only
_register(
    ["lh", "jd", "si", "lc", "ap", "ec"],
    PROFILE_DAY_ONLY,
)


def lookup_session_profile(prefix: str) -> SessionProfile:
    """根据品种前缀查找交易时段模板。未匹配时默认 night_0100"""
    return PREFIX_TO_PROFILE.get(prefix, PROFILE_NIGHT_0100)


# 内置模板按名称引用（合约级 session.template 配置用）
ALL_TEMPLATES: dict[str, SessionProfile] = {
    "index_futures": PROFILE_INDEX_FUTURES,
    "bond_futures": PROFILE_BOND_FUTURES,
    "night_0230": PROFILE_NIGHT_0230,
    "night_0100": PROFILE_NIGHT_0100,
    "night_2300": PROFILE_NIGHT_2300,
    "day_only": PROFILE_DAY_ONLY,
}


def _parse_segments(items) -> list[SessionSegment]:
    """["09:00-10:15", "9:00-10:15", ...] → SessionSegment 列表（统一规范为 HH:MM:SS）。"""
    out: list[SessionSegment] = []
    for s in items or []:
        try:
            start, end = str(s).strip().split("-")
        except ValueError:
            continue
        def _norm(t: str) -> str:
            """HH:MM / H:MM / HH:MM:SS → HH:MM:SS（补零补秒，保证字典序与 datetime 输出一致）。"""
            t = t.strip()
            parts = t.split(":")
            if len(parts) == 2:
                return f"{parts[0].zfill(2)}:{parts[1]}:00"
            if len(parts) == 3:
                return f"{parts[0].zfill(2)}:{parts[1]}:{parts[2].zfill(2)}"
            return t
        out.append(SessionSegment(_norm(start), _norm(end)))
    return out


def session_profile_from_config(session_cfg: dict | None, prefix: str = "") -> SessionProfile:
    """从合约配置构建交易时段（不采用内置模板/品种前缀推断）。

    - 不使用任何内置模板与品种前缀推断：日/夜盘均统一默认；
    - session.day_segments / night_segments：显式覆盖（"HH:MM-HH:MM" 列表；
      空列表 = 显式无该盘段）；
    - 日盘未配置默认 09:00-10:15 / 10:30-11:30 / 13:30-15:00（商品标准日盘）；
    - 夜盘未配置默认 21:00-23:00。
    """
    cfg = session_cfg or {}
    # None/缺键 → 未配置走默认；非 None（含空列表）→ 显式覆盖（空列表 = 显式无该盘段）
    day = cfg.get("day_segments")
    night = cfg.get("night_segments")
    return SessionProfile(
        name="",
        day_segments=(_parse_segments(day) if day is not None
                      else [SessionSegment("09:00:00", "10:15:00"),
                            SessionSegment("10:30:00", "11:30:00"),
                            SessionSegment("13:30:00", "15:00:00")]),
        night_segments=(_parse_segments(night) if night is not None
                        else [SessionSegment("21:00:00", "23:00:00")]),
        break_periods=[],
    )


def _in_profile_segments(profile: SessionProfile, time_hms: str) -> bool:
    """检查 HH:MM:SS 是否落在该模板的日/夜盘段内。"""
    for seg in profile.day_segments:
        if seg.start <= time_hms < seg.end:
            return True
    for seg in profile.night_segments:
        if seg.start <= seg.end:
            if seg.start <= time_hms < seg.end:
                return True
        else:
            # 跨越午夜：21:00:00 ~ 23:59:59 或 00:00:00 ~ 02:30:00
            if time_hms >= seg.start or time_hms < seg.end:
                return True
    return False


def is_in_session_at(profile: SessionProfile, ts: float) -> bool:
    """时间戳是否处于该时段模板的有效交易时段（含周末/夜盘归属处理）。

    规则（与 is_in_session_now 一致）：
    - 周一至周五：仅看 HH:MM:SS 是否落在时段模板内；
    - 周日：21:00 起的夜盘属于下一交易日，时段模板覆盖则有效，其余无效；
    - 周六：仅允许跨午夜夜盘尾段（周五夜盘延续，如 00:00–02:30），白天无效。
    """
    import datetime
    t = datetime.datetime.fromtimestamp(ts)
    hms = t.strftime("%H:%M:%S")
    if t.weekday() == 6:      # 周日：只有夜盘段可能有效
        if hms < "21:00:00":
            return False
    elif t.weekday() == 5:    # 周六：仅跨午夜夜盘尾段（02:30 前）可能有效
        if hms >= "02:30:00":
            return False
    return _in_profile_segments(profile, hms)


def is_in_session_now(prefix: str, ts: float) -> bool:
    """当前时刻（时间戳）是否处于该品种的有效交易时段（含周末/夜盘归属处理）。

    按品种前缀推断模板后委托 is_in_session_at（与合约级 session 配置共用判定）。"""
    return is_in_session_at(lookup_session_profile(extract_prefix(prefix)), ts)


def _time_to_seconds(hms: str) -> int:
    """HH:MM:SS → 秒数"""
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def trading_day_from_tick(instrument_id: str, exchange_timestamp: str) -> str:
    """
    计算交易日 — 对应 C++ trading_day_from_tick()
    exchange_timestamp 格式: "YYYYMMDD HH:MM:SS.mmm"
    返回 "YYYY-MM-DD" 格式交易日字符串。
    """
    if len(exchange_timestamp) < 17:
        return exchange_timestamp[:8]

    year = int(exchange_timestamp[0:4])
    month = int(exchange_timestamp[4:6])
    day = int(exchange_timestamp[6:8])

    prefix = extract_prefix(instrument_id)
    profile = lookup_session_profile(prefix)
    if profile is None:
        return f"{year:04d}-{month:02d}-{day:02d}"

    time_part = exchange_timestamp[9:17]
    tick_seconds = _time_to_seconds(time_part)

    day_offset = 0
    for seg in profile.night_segments:
        start = _time_to_seconds(seg.start)
        end = _time_to_seconds(seg.end)

        if start <= end:
            if start <= tick_seconds <= end:
                day_offset = 1
                break
        else:
            if tick_seconds >= start:
                day_offset = 1
                break
            if tick_seconds <= end:
                day_offset = 0
                break

    if day_offset:
        day += day_offset

    import datetime
    # 用 timedelta 方式构造日期（安全处理跨月，如 4月30日+1天 → 5月1日）
    base = datetime.date(year, month, 1)
    dt = base + datetime.timedelta(days=day - 1)
    wd = dt.weekday()
    if wd == 5:      # Saturday → Monday
        dt += datetime.timedelta(days=2)
    elif wd == 6:    # Sunday → Monday
        dt += datetime.timedelta(days=1)

    return dt.strftime("%Y-%m-%d")
