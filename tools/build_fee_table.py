"""把交易所手续费 xlsx 转换成 ``fees/fee_table.json``。

为什么不用 pandas/openpyxl：本仓库的运行时依赖已被 README 第 3 节的 lock 口径锁死，
而 xlsx 本质是 zip + XML，标准库 ``zipfile`` + ``xml.etree`` 足够读出本表所需的
「工作表 → 行 → 单元格」三层结构。这样转换工具零新增依赖，不会动 lock 文件。

输入
----
``docs/全品种手续费表_交易所标准_20260919(1).xlsx``

- sheet「期货手续费」：品种级费率（交易所 / 品种 / 代码 / 计费单位 / 开仓 / 平仓(平昨) / 平今）
- sheet「特定合约费率备查」：合约级费率（该 sheet 的代码列在「品种」列，见下）

输出（三层回落的数据源）
------------------------
``fees/fee_table.json``::

    {
      "meta":      {"source": ..., "generated_at": ..., "default": {...}},
      "products":  {"OI": {"exchange": "CZCE", "unit": "元", ...}},
      "contracts": {"au2510": {"exchange": "SHFE", "unit": "元", ...}}
    }

回落顺序（``fees/calculator.py`` 实现）：合约 slug → 单腿合约 → 品种代码 → 兜底 2 元/手/边。

用法::

    .venv\\Scripts\\python.exe tools\\build_fee_table.py [xlsx 路径] [-o 输出路径]
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLSX = PROJECT_ROOT / "docs" / "全品种手续费表_交易所标准_20260919(1).xlsx"
DEFAULT_OUTPUT = PROJECT_ROOT / "fees" / "fee_table.json"

SHEET_PRODUCTS = "期货手续费"
SHEET_CONTRACTS = "特定合约费率备查"

UNIT_FIXED = "元"
UNIT_RATE = "万分之"
DEFAULT_FEE = 2.0

# 表内「交易所」中文名 → vnpy Exchange 值；未收录的交易所原样保留中文，仅供排查
EXCHANGE_MAP: dict[str, str] = {
    "郑商所": "CZCE",
    "郑州商品交易所": "CZCE",
    "大商所": "DCE",
    "大连商品交易所": "DCE",
    "上期所": "SHFE",
    "上海期货交易所": "SHFE",
    "能源中心": "INE",
    "上海国际能源交易中心": "INE",
    "中金所": "CFFEX",
    "中国金融期货交易所": "CFFEX",
    "广期所": "GFEX",
    "广州期货交易所": "GFEX",
}

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "exchange": ("交易所",),
    "name": ("品种", "合约"),
    "code": ("代码",),
    "unit": ("计费单位",),
    "open": ("开仓",),
    "close_yesterday": ("平仓(平昨)", "平仓（平昨）", "平仓", "平昨"),
    "close_today": ("平今", "平仓(平今)", "平仓（平今）"),
}


# ---------------------------------------------------------------- xlsx 读取
def _column_index(cell_ref: str) -> int:
    """把 ``"AB12"`` 这样的单元格地址换算成 0 基列号（A=0）。"""
    letters = ""
    for char in cell_ref:
        if char.isalpha():
            letters += char.upper()
        else:
            break
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1 if index else 0


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    """读取共享字符串表；本表用 inlineStr，但没有时也兼容。"""
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    values: list[str] = []
    for item in root.iter(f"{NS}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{NS}t")))
    return values


def _read_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    """rId → worksheet 相对路径。"""
    try:
        raw = archive.read("xl/_rels/workbook.xml.rels")
    except KeyError:
        return {}
    root = ET.fromstring(raw)
    mapping: dict[str, str] = {}
    for rel in root.iter(f"{PKG_REL_NS}Relationship"):
        rid = rel.get("Id", "")
        target = rel.get("Target", "")
        if rid and target:
            mapping[rid] = target.lstrip("/")
    return mapping


def _read_sheet(archive: zipfile.ZipFile, member: str, shared: list[str]) -> list[list[str]]:
    """把一个 worksheet 读成 2 维文本表；空单元格保留空串以保住列位置。"""
    raw = archive.read(member)
    root = ET.fromstring(raw)

    rows: list[list[str]] = []
    for row in root.iter(f"{NS}row"):
        cells: list[str] = []
        for cell in row.iter(f"{NS}c"):
            index = _column_index(cell.get("r", "A1"))
            while len(cells) <= index:
                cells.append("")

            cell_type = cell.get("t", "n")
            if cell_type == "inlineStr":
                text = "".join(node.text or "" for node in cell.iter(f"{NS}t"))
            else:
                value_node = cell.find(f"{NS}v")
                text = (value_node.text or "") if value_node is not None else ""
                if cell_type == "s" and text:
                    try:
                        text = shared[int(text)]
                    except (ValueError, IndexError):
                        text = ""
            cells[index] = text.strip()
        rows.append(cells)
    return rows


def read_workbook(path: Path) -> dict[str, list[list[str]]]:
    """读取整个工作簿 → {工作表名: 二维文本表}。"""
    with zipfile.ZipFile(path) as archive:
        shared = _read_shared_strings(archive)
        rels = _read_relationships(archive)
        book = ET.fromstring(archive.read("xl/workbook.xml"))

        sheets: dict[str, list[list[str]]] = {}
        for sheet in book.iter(f"{NS}sheet"):
            name = sheet.get("name", "")
            target = rels.get(sheet.get(f"{DOC_REL_NS}id", ""), "")
            if not name or not target:
                continue
            member = target if target.startswith("xl/") else f"xl/{target}"
            try:
                sheets[name] = _read_sheet(archive, member, shared)
            except KeyError:
                continue
        return sheets


# ---------------------------------------------------------------- 行 → 费率
def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """定位表头行并返回 {字段: 列号}。"""
    for row_index, row in enumerate(rows[:10]):
        mapping: dict[str, int] = {}
        for column, text in enumerate(row):
            label = text.replace(" ", "")
            for field, aliases in HEADER_ALIASES.items():
                if field in mapping:
                    continue
                if any(label == alias.replace(" ", "") for alias in aliases):
                    mapping[field] = column
        if {"unit", "open", "close_yesterday", "close_today"} <= set(mapping):
            return row_index, mapping
    raise ValueError("未找到手续费表表头（需要 计费单位/开仓/平仓/平今 四列）")


def _to_float(text: str) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_spec(row: list[str], header: dict[str, int]) -> dict | None:
    """解析一行 → 费率规格；非法行返回 None。"""
    def cell(field: str) -> str:
        column = header.get(field, -1)
        if column < 0 or column >= len(row):
            return ""
        return row[column]

    unit = cell("unit").strip()
    if unit not in (UNIT_FIXED, UNIT_RATE):
        return None

    open_fee = _to_float(cell("open"))
    close_yesterday = _to_float(cell("close_yesterday"))
    close_today = _to_float(cell("close_today"))
    if open_fee is None or close_yesterday is None or close_today is None:
        return None

    exchange_cn = cell("exchange").strip()
    return {
        "exchange": EXCHANGE_MAP.get(exchange_cn, exchange_cn),
        "unit": unit,
        "open": open_fee,
        "close_yesterday": close_yesterday,
        "close_today": close_today,
    }


def parse_product_sheet(rows: list[list[str]]) -> dict[str, dict]:
    """品种级费率 sheet → {品种代码: 规格}。"""
    header_row, header = _find_header(rows)
    products: dict[str, dict] = {}
    for row in rows[header_row + 1:]:
        code = row[header["code"]].strip().upper() if header.get("code", -1) >= 0 and header["code"] < len(row) else ""
        if not code:
            continue
        spec = _parse_spec(row, header)
        if spec:
            products[code] = spec
    return products


def parse_contract_sheet(rows: list[list[str]]) -> dict[str, dict]:
    """合约级费率 sheet → {合约代码: 规格}。

    该 sheet 的「代码」列是空的，合约代码放在「品种」列（如 ``au2510``），因此
    两种位置都尝试：优先「代码」列，回退「品种」列。
    """
    header_row, header = _find_header(rows)
    contracts: dict[str, dict] = {}
    for row in rows[header_row + 1:]:
        code = ""
        for field in ("code", "name"):
            column = header.get(field, -1)
            if 0 <= column < len(row) and row[column].strip():
                candidate = row[column].strip()
                if any(char.isdigit() for char in candidate):
                    code = candidate
                    break
        if not code:
            continue
        spec = _parse_spec(row, header)
        if spec:
            contracts[code] = spec
    return contracts


# ---------------------------------------------------------------- 主流程
def build(xlsx_path: Path, output_path: Path) -> dict:
    """执行转换并写盘，返回生成的字典（便于测试直接断言）。"""
    sheets = read_workbook(xlsx_path)
    if SHEET_PRODUCTS not in sheets:
        raise KeyError(f"缺少工作表「{SHEET_PRODUCTS}」，实际有：{list(sheets)}")

    products = parse_product_sheet(sheets[SHEET_PRODUCTS])
    contracts = parse_contract_sheet(sheets.get(SHEET_CONTRACTS, []))

    table = {
        "meta": {
            "source": xlsx_path.name,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "default": {
                "exchange": "",
                "unit": UNIT_FIXED,
                "open": DEFAULT_FEE,
                "close_yesterday": DEFAULT_FEE,
                "close_today": DEFAULT_FEE,
            },
            "product_count": len(products),
            "contract_count": len(contracts),
        },
        "products": dict(sorted(products.items())),
        "contracts": dict(sorted(contracts.items())),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(table, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    return table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交易所手续费 xlsx → fee_table.json")
    parser.add_argument("xlsx", nargs="?", default=str(DEFAULT_XLSX), help="手续费表 xlsx 路径")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="输出 JSON 路径")
    args = parser.parse_args(argv)

    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"[错误] 找不到 xlsx：{xlsx_path}", file=sys.stderr)
        return 1

    table = build(xlsx_path, Path(args.output))
    meta = table["meta"]
    print(
        f"已生成 {args.output}：品种 {meta['product_count']} 条，"
        f"合约 {meta['contract_count']} 条"
    )

    for code in ("J", "OI", "A", "B", "RM"):
        spec = table["products"].get(code)
        if spec is None:
            print(f"[警告] 推荐品种 {code} 未在表中找到", file=sys.stderr)
        else:
            print(f"  {code:>3} {spec['exchange']:<5} {spec['unit']} "
                  f"开{spec['open']} 平昨{spec['close_yesterday']} 平今{spec['close_today']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
