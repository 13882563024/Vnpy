import json
import re
import shutil
from pathlib import Path

SRC = Path(r"C:\Users\Mesky\Desktop\2701-2703_config.json")
REG = Path("config/spreads.json")
FEES = Path("fees/fee_table.json")

data = json.loads(SRC.read_text(encoding="utf-8"))
fees = json.loads(FEES.read_text(encoding="utf-8"))["products"]

existing = json.loads(REG.read_text(encoding="utf-8")) if REG.exists() else {}
shutil.copy2(REG, Path("artifacts/spreads.json.before-desktop-config"))

registry = {}
fee_report = []

def product_of(leg: str) -> str:
    match = re.match(r"([A-Za-z]+)", leg)
    return match.group(1).upper() if match else leg.upper()

for item in data["contracts"]:
    exchange = str(item.get("exchange") or "DCE").upper()
    near, far = item["near_leg"], item["far_leg"]
    product = product_of(near)
    digits = lambda leg: re.sub(r"^[A-Za-z]+", "", leg)
    if exchange == "CZCE":
        name = f"{product}_{digits(near)}_{digits(far)}"
    else:
        name = f"{product}_{digits(near)}_{digits(far)}"
    spread = item["instrument"].strip()
    near_vt = f"{near}.{exchange}"
    far_vt = f"{far}.{exchange}"
    strategy = {
        "live_trading": False,
        "mean_window_minutes": int(item["signal"]["n_minutes"]),
        "sd_window_minutes": int(item["signal"]["m_minutes"]),
        "entry_k": float(item["signal"]["x_sd"]),
        "layer_step": float(item["layering"]["layer_step"]),
        "max_positions": int(item["layering"]["max_positions"]),
        "layer_volume": 1.0,
        "width_ticks": float(item["filters"]["width_ticks"]),
        "min_depth": float(item["filters"]["min_depth"]),
        "vr_max": float(item["filters"]["volume_ratio_max"]),
        "sync_seconds": float(item["filters"]["sync_s"]),
        "roll_entry_cutoff_days": int(item["filters"]["roll_entry_cutoff_days"]),
        "roll_exit_days": int(item["filters"]["roll_exit_days"]),
        "stop_loss": float(item["exit"]["stop_loss"]),
        "min_exit_pnl_override": float(item["exit"]["min_exit_pnl"]),
        "max_hold_days": int(item["exit"]["max_hold_days"]),
        "close_only": bool(item["close_only"]),
        "exit_preplace": bool(item["exit_preplace"]),
        "market_close_timeout": 0.0,
        "entry_partial_timeout": 0.0,
    }
    previous = existing.get(name, {})
    registry[name] = {
        "display_name": f"{exchange}.{spread}",
        "legs": [near_vt, far_vt],
        "active_symbol": near_vt,
        "exchange_spread": spread,
        "exchange": exchange,
        "product": product,
        "size": float(item["multiplier"]),
        "pricetick": float(item["tick_size"]),
        "executor": "exchange_spread",
        "price_formula": "near - far",
        "strategy": strategy,
        "contract_status": previous.get("contract_status", "pending_verification"),
        "trading_enabled": bool(previous.get("trading_enabled", False)),
        "stop_loss": float(item["exit"]["stop_loss"]),
        "vr_max": float(item["filters"]["volume_ratio_max"]),
        "remark": item.get("name_cn", ""),
        "margin_per_lot": float(item["margin_per_lot"] or 0.0),
        "trading_sessions": "night_2300",
    }
    fee = fees.get(product)
    fee_report.append((name, product, exchange, fee))

REG.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(f"configured {len(registry)} contracts")
for name, product, exchange, fee in fee_report:
    if fee is None:
        print(f"{name:14s} {product:4s} 费率表无此品种")
    else:
        print(f"{name:14s} {product:4s} {fee['unit']} 开{fee['open']:g}/平昨{fee['close_yesterday']:g}/平今{fee['close_today']:g}")
