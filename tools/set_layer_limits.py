import json
from pathlib import Path

path = Path("config/spreads.json")
data = json.loads(path.read_text(encoding="utf-8"))

for name, cfg in data.items():
    strategy = cfg.setdefault("strategy", {})
    strategy["max_positions"] = 1
    strategy["close_only"] = False
    cfg["strategy"] = strategy

path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

for name, cfg in data.items():
    strategy = cfg["strategy"]
    print(f"{name:14s} max_positions={strategy['max_positions']} close_only={strategy['close_only']}")
