import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath("."))

from vnpy.trader.ui import QtWidgets  # noqa: E402
from spreadconsole.ui.widget import ReconciliationDialog  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

# 离屏环境没有字体目录；显式加载系统中文字体，仅用于截图预览。
from PySide6.QtGui import QFontDatabase  # noqa: E402

for font_path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
    if os.path.exists(font_path):
        QFontDatabase.addApplicationFont(font_path)
        break

lines = ["账户 CTP：可用 128,430 元，权益 356,210 元"]
for name in ("A_2701_2703", "OI_701_703", "J_2701_2703", "M_2701_2705"):
    lines.append(
        f"\n{name}（验证 已验证）：有敞口，开启该合约会被拒绝\n"
        f"在途委托 0，单腿持仓 0，引擎净仓 1，策略层 0，在途算法 1"
    )
    lines.append(f"持仓差异 a2701.DCE：本地 1 手，柜台净仓 0 手（多 0 / 空 0）；本地仓位不能替代柜台仓位")
    lines.append("在途算法：\n  - 7f3c9a21-4e6b-4c01-9d2f-5a6b7c8d9e0f")
text = "\n".join(lines)
text += "\n\n注意：\n- OI_701_703：柜台未提供交易所组合合约，不能开启交易"
text += "\n\n有敞口的合约（只影响这些合约开启交易，不影响核对确认）：\n- A_2701_2703：1 条单腿持仓"
text += (
    "\n\n核对确认只记录账户状态，不会撤销委托、不会平仓，也不会把有敞口的合约变成可交易。"
    "\n程序只能展示已查询到的状态，请再与主终端的委托/成交记录核对。"
)

dialog = ReconciliationDialog(text)
dialog.show()
app.processEvents()
out = os.path.abspath("artifacts/reconciliation-dialog.png")
os.makedirs(os.path.dirname(out), exist_ok=True)
dialog.grab().save(out)
print(out)
