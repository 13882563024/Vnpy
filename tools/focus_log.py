from pathlib import Path

log = Path.home() / ".vntrader/log/vt_20261105.log"
lines = log.read_text(encoding="utf-8", errors="replace").splitlines()

filters = {
    "errors": ("代码：", "被拒", "失败"),
    "czce": ("SPD",),
    "shfe": ("SP ru", "RU_2701_2703"),
    "fills": ("全部成交", "已撤销", "拒单"),
}
out = []
for title, needles in filters.items():
    out.append(f"===== {title} =====")
    for line in lines:
        if ("2026-11-05 22:" in line or "2026-11-05 23:" in line) and any(n in line for n in needles):
            out.append(line)
Path("artifacts/today_log_focus.txt").write_text("\n".join(out), encoding="utf-8")
print(len(out))
