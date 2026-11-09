"""合约代码 → 交易所推断（纯函数，零外部依赖）。

独立成模块的原因：CppBroker（C++ 报单通道）与 broker.resolve_exchange 都要用它，
但两者都不依赖 vnpy_ctp。此前该函数放在 trading/ctp_broker.py 里，导致
「未安装 vnpy_ctp 时 import trading.cpp_broker 连带崩溃」——实盘 C++ 通道与
模拟通道本不该受 CTP Python 包是否存在的影响。
"""
from __future__ import annotations

# 合约代码 → 交易所映射
PRODUCT_EXCHANGE: dict[str, str] = {
    "AP": "CZCE", "CY": "CZCE", "FG": "CZCE", "JR": "CZCE", "LR": "CZCE",
    "MA": "CZCE", "OI": "CZCE", "PF": "CZCE", "PK": "CZCE", "PM": "CZCE",
    "RI": "CZCE", "RM": "CZCE", "RS": "CZCE", "SR": "CZCE", "TA": "CZCE",
    "UR": "CZCE", "WH": "CZCE", "ZC": "CZCE", "CJ": "CZCE", "SA": "CZCE",
    "SF": "CZCE", "SH": "CZCE", "PX": "CZCE",
    "au": "SHFE", "ag": "SHFE", "cu": "SHFE", "al": "SHFE", "zn": "SHFE",
    "pb": "SHFE", "ni": "SHFE", "sn": "SHFE", "rb": "SHFE", "hc": "SHFE",
    "bu": "SHFE", "ru": "SHFE", "sp": "SHFE", "ss": "SHFE", "ao": "SHFE",
    "br": "SHFE", "sc": "INE", "lu": "INE", "nr": "INE", "bc": "INE",
    "ec": "INE",
    "m": "DCE", "y": "DCE", "a": "DCE", "b": "DCE", "c": "DCE", "l": "DCE",
    "p": "DCE", "v": "DCE", "j": "DCE", "jm": "DCE", "i": "DCE", "eg": "DCE",
    "eb": "DCE", "pg": "DCE", "lh": "DCE", "rr": "DCE", "cs": "DCE",
    "IF": "CFFEX", "IH": "CFFEX", "IC": "CFFEX", "IM": "CFFEX",
    "T": "CFFEX", "TF": "CFFEX", "TS": "CFFEX", "TL": "CFFEX",
    "si": "GFEX", "lc": "GFEX",
}


def exchange_of(symbol: str) -> str:
    """从合约代码推断交易所（腿/组合合约代码提取产品字母前缀）。

    组合合约（跨期 SP/SPD、跨品种 SPC/IPD 等）先剥离前缀并取第一腿，
    再按产品字母查交易所表；无法识别时回退 CZCE。
    """
    s = symbol.strip()
    for combo_prefix in ("SPD ", "SPC ", "IPD ", "SP "):
        if s.startswith(combo_prefix):
            s = s.split(" ")[1].split("&")[0]
            break
    prefix = ""
    for i in range(1, min(4, len(s))):
        if s[i].isdigit():
            prefix = s[:i]
            break
    if not prefix:
        prefix = s
    return PRODUCT_EXCHANGE.get(prefix, "CZCE")
