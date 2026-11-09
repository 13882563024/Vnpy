"""复盘记录统一结构：决策信号 tick / 报单 / 成交。

三类记录各自落盘于 ``data/<slug>/``（模式前缀 ``sim_``/``real_``，按交易日分文件）：

  ``<mode>signals_<交易日>.jsonl``       每次决策的 Signal（spread/mean/sd/开平对手价/方向）
  ``<mode>orders_hist_<交易日>.jsonl``   终态报单（Order.to_dict + archived_at/archive_reason）
  ``<mode>trades_<交易日>.jsonl``        成交（一笔完整开→平，含毛利/手续费/净利）

本模块把三类记录归一化成同一套字段，便于按时间轴对齐分析与 CSV/JSON 导出：

公共字段（三类都有）
  kind         记录类型：signal / order / trade
  ts           记录时间（epoch 秒）：信号=决策 tick；报单=下单时间；成交=平仓时间
  time         同上的本地时间字符串（YYYY-MM-DD HH:MM:SS，便于 Excel 查看）
  trading_day  交易日：信号=决策当日；报单=报单交易日；成交=平仓交易日
  slug / mode  合约 slug 与通道模式（simulate / real，旧记录缺字段时由调用方补齐）
  side         价差持仓方向 LONG / SHORT（信号=决策时持仓方向；报单=该单归属方向；
               成交=该笔交易方向）
  volume       手数（信号无手数语义，为 None）
  price        价格：信号=合成价差；报单=挂单价（市价单 0）；成交=平仓价差
  reason       原因：信号=跳过/过滤器阻止原因；报单=策略原因；成交=平仓原因

专属字段
  signal：spread / mean / sd / signal_ready / long_cross_price / short_cross_price /
          exit_price / near_volume / far_volume / filter_passed / filter_blocked_by /
          skip_reason / action_count / actions / actions_cn
  order ：order_id / action / action_cn / direction / status / status_cn / target_tier /
          open_price / cum_volume / cum_amount / last_trade_price / order_sys_id /
          ctp_ref / error_msg / archive_reason / archived_at
  trade ：entry_price / exit_price / gross_pnl / fee / net_pnl / open_ts / close_ts /
          open_trading_day / close_trading_day

历史文件兼容：旧记录缺字段（如早期 signals 无 pos_side/对手价、trades 无 mode）
一律填 None/空串，不抛异常；口径不变（价差点数、金额元）。
"""
from __future__ import annotations

import csv
import io
import json
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Optional, Sequence

from strategy.order_book import (
    ACTION_CN,
    STATUS_CN,
    order_action_of,
    order_direction_of,
    order_position_side_of,
)

KIND_SIGNAL = "signal"
KIND_ORDER = "order"
KIND_TRADE = "trade"

# 公共字段（三类记录对齐口径，CSV 前 10 列）
BASE_FIELDS: tuple[str, ...] = (
    "kind", "ts", "time", "trading_day", "slug", "mode",
    "side", "volume", "price", "reason",
)

SIGNAL_FIELDS: tuple[str, ...] = (
    "spread", "mean", "sd", "signal_ready", "pos_side",
    "long_cross_price", "short_cross_price", "exit_price",
    "near_volume", "far_volume", "filter_passed", "filter_blocked_by",
    "skip_reason", "action_count", "actions", "actions_cn",
)

ORDER_FIELDS: tuple[str, ...] = (
    "order_id", "action", "action_cn", "direction", "status", "status_cn",
    "target_tier", "open_price", "cum_volume", "cum_amount",
    "last_trade_price", "order_sys_id", "ctp_ref", "error_msg",
    "archive_reason", "archived_at",
)

TRADE_FIELDS: tuple[str, ...] = (
    "entry_price", "exit_price", "gross_pnl", "fee", "net_pnl",
    "open_ts", "close_ts", "open_trading_day", "close_trading_day",
)

_ACTION_TYPE_CN = {"OPEN": "开仓", "ADD": "加仓", "CLOSE": "平仓"}
_SIDE_CN = {"LONG": "多", "SHORT": "空"}


def _num(v) -> Optional[float]:
    """宽松数值转换：None/""/非数值 → None（旧记录字段缺失时不留 0 误导）。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt_time(ts) -> str:
    """epoch 秒 → 本地时间字符串（缺失/非法 → 空串）。"""
    v = _num(ts)
    if not v:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))


def _actions_text(actions) -> str:
    """动作列表 → 中文摘要（CSV 可读列；结构化内容保留在 actions 列）。"""
    if not actions:
        return ""
    parts = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        kind = _ACTION_TYPE_CN.get(str(a.get("type", "")), str(a.get("type", "")))
        side = _SIDE_CN.get(str(a.get("side") or ""), "")
        price = a.get("price") or 0
        market = "市价" if a.get("market") else f"限价{price}"
        reason = str(a.get("reason") or "")
        part = f"{kind}{side}{a.get('volume', 0)}手 {market}"
        parts.append(f"{part}（{reason}）" if reason else part)
    return "；".join(parts)


def signal_record(rec: dict, *, slug: str = "", mode: str = "") -> dict:
    """决策信号 tick（每次 on_tick 一条）→ 统一结构。"""
    ts = _num(rec.get("ts")) or 0.0
    actions = rec.get("actions") or []
    if not isinstance(actions, list):
        actions = []
    pos_side = rec.get("pos_side") or None
    skip = str(rec.get("skip_reason") or "")
    blocked = rec.get("filter_blocked_by") or None
    return {
        "kind": KIND_SIGNAL,
        "ts": ts,
        "time": fmt_time(ts),
        "trading_day": str(rec.get("trading_day") or ""),
        "slug": str(rec.get("slug") or slug),
        "mode": str(rec.get("mode") or mode),
        "side": pos_side,
        "volume": None,
        "price": _num(rec.get("spread")),
        "reason": skip or (f"过滤器阻止: {blocked}" if blocked else ""),
        # ---- 信号口径（计算 mean/sd 与开平对手价） ----
        "spread": _num(rec.get("spread")),
        "mean": _num(rec.get("mean")),
        "sd": _num(rec.get("sd")),
        "signal_ready": rec.get("signal_ready"),
        "pos_side": pos_side,
        "long_cross_price": _num(rec.get("long_cross_price")),
        "short_cross_price": _num(rec.get("short_cross_price")),
        "exit_price": _num(rec.get("exit_price")),
        "near_volume": rec.get("near_volume"),
        "far_volume": rec.get("far_volume"),
        "filter_passed": rec.get("filter_passed"),
        "filter_blocked_by": blocked,
        "skip_reason": skip or None,
        "action_count": len(actions),
        "actions": actions,
        "actions_cn": _actions_text(actions),
    }


def order_record(rec: dict, *, slug: str = "", mode: str = "") -> dict:
    """终态报单（orders_hist 记录）→ 统一结构。"""
    ts = _num(rec.get("order_time")) or 0.0
    action = order_action_of(rec)
    status = str(rec.get("status") or "")
    return {
        "kind": KIND_ORDER,
        "ts": ts,
        "time": fmt_time(ts),
        "trading_day": str(rec.get("trading_day") or ""),
        "slug": str(rec.get("slug") or slug),
        "mode": str(rec.get("mode") or mode),
        "side": order_position_side_of(rec) or None,
        "volume": int(_num(rec.get("volume")) or 0),
        "price": _num(rec.get("price")),
        "reason": str(rec.get("reason") or ""),
        # ---- 报单信息 ----
        "order_id": str(rec.get("local_id") or ""),
        "action": action,
        "action_cn": ACTION_CN.get(action, action),
        "direction": order_direction_of(rec),
        "status": status,
        "status_cn": STATUS_CN.get(status, status),
        "target_tier": rec.get("target_tier", -1),
        "open_price": _num(rec.get("open_price")),
        "cum_volume": int(_num(rec.get("cum_volume")) or 0),
        "cum_amount": _num(rec.get("cum_amount")),
        "last_trade_price": _num(rec.get("last_trade_price")),
        "order_sys_id": str(rec.get("order_sys_id") or ""),
        "ctp_ref": str(rec.get("ctp_ref") or ""),
        "error_msg": str(rec.get("error_msg") or ""),
        "archive_reason": str(rec.get("archive_reason") or ""),
        "archived_at": _num(rec.get("archived_at")),
    }


def trade_record(rec: dict, *, slug: str = "", mode: str = "") -> dict:
    """成交记录（一笔完整开→平）→ 统一结构。ts 取平仓时间。"""
    close_ts = _num(rec.get("close_ts")) or 0.0
    return {
        "kind": KIND_TRADE,
        "ts": close_ts,
        "time": fmt_time(close_ts),
        "trading_day": str(rec.get("close_trading_day") or ""),
        "slug": str(rec.get("slug") or slug),
        "mode": str(rec.get("mode") or mode),
        "side": str(rec.get("side") or "") or None,
        "volume": int(_num(rec.get("volume")) or 0),
        "price": _num(rec.get("exit_price")),
        "reason": str(rec.get("reason") or ""),
        # ---- 成交明细 ----
        "entry_price": _num(rec.get("entry_price")),
        "exit_price": _num(rec.get("exit_price")),
        "gross_pnl": _num(rec.get("gross_pnl")),
        "fee": _num(rec.get("fee")),
        "net_pnl": _num(rec.get("net_pnl")),
        "open_ts": _num(rec.get("open_ts")),
        "close_ts": close_ts,
        "open_trading_day": str(rec.get("open_trading_day") or ""),
        "close_trading_day": str(rec.get("close_trading_day") or ""),
    }


def build_records(signals: Iterable[dict] = (), orders: Iterable[dict] = (),
                  trades: Iterable[dict] = (), *, slug: str = "",
                  mode: str = "", sort: bool = True) -> list[dict]:
    """三类原始记录 → 统一结构列表（默认按 ts 升序，便于时间轴对齐复盘）。"""
    rows: list[dict] = []
    rows += [signal_record(r, slug=slug, mode=mode) for r in signals]
    rows += [order_record(r, slug=slug, mode=mode) for r in orders]
    rows += [trade_record(r, slug=slug, mode=mode) for r in trades]
    if sort:
        rows.sort(key=lambda r: (r.get("ts") or 0.0, r.get("kind") or ""))
    return rows


def csv_columns(records: Sequence[dict] = ()) -> list[str]:
    """CSV 列顺序：公共字段 → 信号 → 报单 → 成交 专属字段 → 其余首见字段。"""
    cols = list(BASE_FIELDS) + list(SIGNAL_FIELDS) + list(ORDER_FIELDS) + list(TRADE_FIELDS)
    seen = set(cols)
    for r in records:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def columns_for(kind: str) -> list[str]:
    """单类记录的列（公共字段 + 该类专属字段），用于分开导出三份文件。"""
    extra = {KIND_SIGNAL: SIGNAL_FIELDS,
             KIND_ORDER: ORDER_FIELDS,
             KIND_TRADE: TRADE_FIELDS}.get(kind, ())
    return list(BASE_FIELDS) + list(extra)


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def to_csv(records: Sequence[dict], columns: Optional[Sequence[str]] = None) -> str:
    """统一结构记录 → CSV 文本（缺列留空；列表/字典列写成 JSON 字符串）。"""
    cols = list(columns) if columns else csv_columns(records)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(cols)
    for r in records:
        writer.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue()


def to_json(records: Sequence[dict], *, slug: str = "", date: str = "",
            mode: str = "", sources: Optional[dict] = None) -> str:
    """统一结构记录 → JSON 文本（与 /api/export 的 JSON 同结构）。"""
    counts = {k: sum(1 for r in records if r["kind"] == k)
              for k in (KIND_SIGNAL, KIND_ORDER, KIND_TRADE)}
    body = {"slug": slug, "date": date, "mode": mode,
            "sources": dict(sources or {}), "counts": counts,
            "records": list(records)}
    return json.dumps(body, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 落盘读取
_FILE_KIND = {KIND_SIGNAL: "signals", KIND_ORDER: "orders_hist", KIND_TRADE: "trades"}
MODE_PREFIX = {"simulate": "sim_", "real": "real_"}
ALL_KINDS = (KIND_SIGNAL, KIND_ORDER, KIND_TRADE)


def history_filenames(prefix: str, kind: str, date: str,
                      *, legacy_fallback: bool = False) -> list[str]:
    """某类记录在某交易日的候选文件名（date 兼容 YYYYMMDD / YYYY-MM-DD）。

    legacy_fallback=True 时追加无模式前缀旧文件（历史迁移数据，均为模拟时代产生）。
    """
    names = [f"{prefix}{kind}_{date}.jsonl"]
    iso = ""
    if "-" not in date and len(date) == 8:
        iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        names.append(f"{prefix}{kind}_{iso}.jsonl")
    if legacy_fallback:
        names.append(f"{kind}_{date}.jsonl")
        if iso:
            names.append(f"{kind}_{iso}.jsonl")
    return names


def read_day(store, slug: str, date: str, *, mode: str = "simulate",
             kinds: Iterable[str] = ALL_KINDS
             ) -> tuple[list[dict], dict[str, str]]:
    """读某交易日三类记录并归一化 → (统一结构记录, {文件类别: 命中文件名})。

    与 /api/export、命令行导出共用同一口径（real 模式不回退无前缀旧文件）。
    """
    prefix = MODE_PREFIX.get(mode, "sim_")
    want = set(kinds)
    raw = {"signals": [], "orders_hist": [], "trades": []}
    sources: dict[str, str] = {}
    for rec_kind, file_kind in _FILE_KIND.items():
        if rec_kind not in want:
            continue
        for fn in history_filenames(prefix, file_kind, date,
                                    legacy_fallback=mode == "simulate"):
            recs = store.read_jsonl(slug, fn)
            if recs:
                sources[file_kind] = fn
                raw[file_kind] = recs
                break
    rows = build_records(raw["signals"], raw["orders_hist"], raw["trades"],
                         slug=slug, mode=mode)
    return rows, sources


def read_range(store, slug: str, days: Iterable[str], *, mode: str = "",
               kinds: Iterable[str] = ALL_KINDS
               ) -> tuple[list[dict], dict[str, dict[str, str]]]:
    """读多个交易日的记录（每天按实际存在的模式）→ 合并排序的统一结构记录。

    mode 非空时强制该模式（否则逐日探测）。返回 (rows, sources)，
    sources[交易日][文件类别] = 命中的文件名（跨模式/跨天都记在 mode 列上区分）。
    """
    want = set(kinds)
    rows: list[dict] = []
    sources: dict[str, dict[str, str]] = {}
    for day in days:
        mode_ = mode or detect_mode(store, slug, day)
        if not mode_:
            continue
        day_rows, day_sources = read_day(store, slug, day, mode=mode_, kinds=want)
        if not day_rows:
            continue
        rows += day_rows
        sources[day] = day_sources
    rows.sort(key=lambda r: (r.get("ts") or 0.0, r.get("kind") or ""))
    return rows, sources


def detect_mode(store, slug: str, date: str) -> str:
    """某交易日实际存在记录的模式（real 优先，其次 simulate；都没有返回空串）。"""
    for mode in ("real", "simulate"):
        for kind in ("signals", "orders_hist", "trades"):
            for fn in history_filenames(MODE_PREFIX[mode], kind, date,
                                        legacy_fallback=mode == "simulate"):
                if store.path(slug, fn).exists():
                    return mode
    return ""


def available_days(store, slug: str) -> list[str]:
    """该合约有复盘记录（信号/报单/成交/流水）的交易日，升序。"""
    days: set[str] = set()
    for p in store.slug_dir(slug).glob("*.jsonl"):
        name = p.stem
        for kind in ("signals", "orders_hist", "trades", "orders_events"):
            idx = name.find(f"{kind}_")
            if idx >= 0:
                day = name[idx + len(kind) + 1:]
                if day:
                    days.add(day)
                break
    return sorted(days)


def locate_file(store, slug: str, date: str, mode: str,
                kind: str) -> Optional[Path]:
    """定位某类记录在某交易日实际存在的文件（不解析内容）。

    kind 用文件类别：signals / orders_hist / trades / orders_events。
    """
    for fn in history_filenames(MODE_PREFIX.get(mode, "sim_"), kind, date,
                                legacy_fallback=mode == "simulate"):
        p = store.path(slug, fn)
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def file_line_count(path: Optional[Path]) -> int:
    """非空行数（不解析 JSON：信号 tick 一天可达数万行，统计只数行）。"""
    if not path:
        return 0
    n = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def tail_records(path: Optional[Path], limit: Optional[int] = None) -> list[dict]:
    """解析文件记录：limit=None/<=0 = 全量；否则只解析最后 limit 条。

    信号 tick 一天可达数万行——汇总只数行数，明细按需取尾部或全量。
    """
    if not path:
        return []
    all_lines = limit is None or limit <= 0
    buf: deque[str] = deque() if all_lines else deque(maxlen=limit)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line:
                buf.append(line)
    out: list[dict] = []
    for line in buf:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
