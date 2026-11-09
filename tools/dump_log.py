from pathlib import Path
import re

log = Path.home() / ".vntrader/log/vt_20261105.log"
lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
out = Path("artifacts/today_log_tail.txt")
out.parent.mkdir(exist_ok=True)
out.write_text("\n".join(lines[-120:]), encoding="utf-8")

keywords = re.compile(r"错误|失败|拒绝|无法识别|不合法|异常|ERR|error|Error|Ctp:|CTP错误|回报错误|撤单失败|报单")
matches = [line for line in lines if keywords.search(line)]
Path("artifacts/today_log_errors.txt").write_text("\n".join(matches[-80:]), encoding="utf-8")
print("tail", len(lines[-120:]), "errors", len(matches))
