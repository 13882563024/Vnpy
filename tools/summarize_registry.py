import json

data = json.load(open("config/spreads.json", encoding="utf-8"))
for name, cfg in data.items():
    strategy = cfg["strategy"]
    print(
        f"{name:14s} {cfg['exchange']:5s} {cfg['exchange_spread']:20s} "
        f"层数{strategy['max_positions']} 只平仓={strategy['close_only']} "
        f"许可={cfg['trading_enabled']} 状态={cfg['contract_status']}"
    )
