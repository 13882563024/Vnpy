import json
from pathlib import Path

path = Path("config/spreads.json")
data = json.loads(path.read_text(encoding="utf-8"))
cfg = data["RU_2701_2703"]
cfg["trading_enabled"] = False
cfg["strategy"]["live_trading"] = False
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("RU_2701_2703 trading_enabled =", cfg["trading_enabled"])
