"""持久化：文件夹 + JSON/JSONL（按 slug 隔离，蓝图 §8 / §10）。

文件布局（data/<slug>/），模拟/实盘数据文件带 sim_/real_ 前缀（E 类需求）：
  config.json            每合约策略配置
  state.json             状态快照（熔断、开关等）
  sim_position.json      模拟实时持仓（重启自动加载）
  real_position.json     实盘实时持仓（重启自动加载）
  <sim_|real_>orders_live.jsonl           实时报单（未终态集合的权威落盘）
  <sim_|real_>orders_hist_<交易日>.jsonl  历史报单（终态档案，网页回看，启动时加载当日）
  <sim_|real_>orders_events_<交易日>.jsonl 报单变更流水（状态迁移时间线）
  <sim_|real_>trades_<交易日>.jsonl       成交明细（启动时加载当日）
  <sim_|real_>ticks_<交易日>.jsonl        原始 tick（两腿行情快照 + 合成价差，复盘用）
  <sim_|real_>signals_<交易日>.jsonl      决策信号 tick（每次 evaluate 的 Signal：spread/mean/sd/
                                          开平对手价/方向 + 输出 actions，复盘用）
  <sim_|real_>klines_cache.json  K线缓存（网页显示 + 布林带初始化）
  sim_orders.jsonl / sim_positions.json   （预留：模拟层自己的状态，未接入）

三类复盘记录（信号 tick / 报单 / 成交）的字段口径与统一导出见 strategy/records.py
（GET /api/export/{slug}?date=&format=json|csv）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


def _ensure_serializable(obj: Any) -> Any:
    return obj


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ---------- 路径 ----------
    def slug_dir(self, slug: str) -> Path:
        d = self.root / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(self, slug: str, filename: str) -> Path:
        return self.slug_dir(slug) / filename

    def list_slugs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and not p.name.startswith("."))

    # ---------- JSON ----------
    def read_json(self, slug: str, filename: str, default: Any = None) -> Any:
        p = self.path(slug, filename)
        if not p.exists():
            return default
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default

    def write_json(self, slug: str, filename: str, data: Any) -> None:
        p = self.path(slug, filename)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    # ---------- JSONL ----------
    def append_jsonl(self, slug: str, filename: str, record: dict) -> None:
        p = self.path(slug, filename)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_jsonl(self, slug: str, filename: str) -> list[dict]:
        p = self.path(slug, filename)
        if not p.exists():
            return []
        out: list[dict] = []
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return out

    def write_jsonl(self, slug: str, filename: str, records: list[dict]) -> None:
        """全量写回（实时区报单集合的权威落盘）。"""
        p = self.path(slug, filename)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, p)

    def remove_file(self, slug: str, filename: str) -> None:
        p = self.path(slug, filename)
        if p.exists():
            p.unlink()
