"""GUI 单字段添加跨期合约、采集可视化和总/单合约策略开关。"""
from __future__ import annotations

from datetime import datetime

from vnpy.trader.ui import QtCore, QtWidgets
from vnpy_spreadtrading import APP_NAME

from fees.calculator import FeeCalculator, UNIT_FIXED, UNIT_RATE, spec_from_record
from spreadconsole.control import SpreadTradingControl
from utils.managed_spreads import ManagedSpreadError
from utils.risk_limits import (
    RiskLimits,
    account_usage,
    load_risk_limits,
    save_risk_limits,
)

_FEE_CALC = FeeCalculator()


def _active_order_lines(main_engine, config, saved_rows=None) -> list[str]:
    """当前合约的在途委托：OMS 活跃委托 + 本地保存但引擎还没恢复的记录。"""
    symbols = set(config.legs + [config.exchange_spread_vt_symbol])
    try:
        orders = [
            order for order in main_engine.get_all_active_orders()
            if getattr(order, "vt_symbol", "") in symbols
        ]
    except Exception:
        orders = []
    lines = [
        f"{order.vt_orderid} {order.vt_symbol} "
        f"{order.direction.value}/{order.offset.value} "
        f"{order.traded:g}/{order.volume:g}@{order.price:g} {order.status.value}"
        for order in orders
    ]
    known = {getattr(order, "vt_orderid", "") for order in orders}
    getter = getattr(main_engine, "get_order", None)
    for row in saved_rows or []:
        orderid = str(row.get("orderid", ""))
        if not orderid or orderid in known:
            continue
        # 以 OMS 的最新状态为准：已终结的历史记录不再显示。
        live = getter(orderid) if callable(getter) else None
        if live is not None:
            if not live.is_active():
                continue
            lines.append(
                f"{live.vt_orderid} {live.vt_symbol} "
                f"{live.direction.value}/{live.offset.value} "
                f"{live.traded:g}/{live.volume:g}@{live.price:g} {live.status.value}"
            )
            continue
        lines.append(
            f"{orderid} {row.get('vt_symbol') or '—'} "
            f"成交 {float(row.get('traded', 0) or 0):g}/{float(row.get('volume', 0) or 0):g} "
            f"{row.get('status', '')}（本地记录）"
        )
    return lines


def _slot_lines(strategy, get_order=None) -> list[str]:
    """当前“开/加仓槽位”和“平仓槽位”的占用情况（含方向/开平/报价）。"""
    if strategy is None:
        return ["开/加仓槽位：策略未注册", "平仓槽位：策略未注册"]

    def side(value) -> str:
        return "多" if float(value or 0) > 0 else "空"

    def order_text(orderid: str) -> str:
        order = None
        if callable(get_order):
            try:
                order = get_order(orderid)
            except Exception:
                order = None
        if order is None:
            return f"{orderid}（方向/报价未知）"
        return (f"{orderid} {order.direction.value}/{order.offset.value} "
                f"{order.traded:g}/{order.volume:g}@{order.price:g} {order.status.value}")

    open_parts: list[str] = []
    pending_open = getattr(strategy, "pending_open", None)
    if pending_open is not None:
        open_parts.append(
            f"待发开仓 {side(getattr(pending_open, 'direction', 0))}"
            f"{float(getattr(pending_open, 'volume', 0) or 0):g}手"
            f"@{float(getattr(pending_open, 'price', 0) or 0):g}"
        )
    for job in (getattr(strategy, "jobs", {}) or {}).values():
        if getattr(job, "kind", "") == "open" and not getattr(job, "done", False):
            open_parts.append(
                f"算法 {job.algoid} {side(getattr(job, 'direction', 0))}"
                f"/开 {float(getattr(job, 'volume', 0) or 0):g}手"
            )
    for orderid in (getattr(strategy, "reserved_open_orders", {}) or {}):
        open_parts.append(order_text(str(orderid)))
    open_parts = list(dict.fromkeys(open_parts))

    lines = ["开/加仓槽位：" + ("占用" if open_parts else "空闲")]
    lines.extend(f"  · {part}" for part in open_parts)

    reserved = getattr(strategy, "reserved_exit_orders", {}) or {}
    layers = list(getattr(strategy, "layers", []) or [])
    if not layers:
        lines.append("平仓槽位：无持仓层")
    for index, layer in enumerate(layers, 1):
        tier_id = getattr(layer, "tier_id", "")
        bound_ids = [
            str(orderid) for orderid, tier in reserved.items()
            if not tier or tier == tier_id
        ]
        pieces = [order_text(orderid) for orderid in bound_ids]
        algoid = getattr(layer, "algoid", "")
        if algoid and algoid not in bound_ids:
            pieces.append(f"止盈算法 {algoid}")
        direction = side(getattr(layer, "direction", 0))
        volume = float(getattr(layer, "volume", 0) or 0)
        lines.append(
            f"平仓槽位 第{index}层（{direction}{volume:g}手）："
            + ("占用" if pieces else "空闲")
        )
        lines.extend(f"  · {piece}" for piece in pieces)
    pending_close = getattr(strategy, "pending_close", None)
    if pending_close is not None:
        lines.append(
            f"平仓槽位：待发平仓 {side(getattr(pending_close, 'direction', 0))}"
            f"{float(getattr(pending_close, 'volume', 0) or 0):g}手"
            f"@{float(getattr(pending_close, 'price', 0) or 0):g}"
        )
    if getattr(strategy, "exit_algoid", ""):
        lines.append(f"平仓槽位：止盈算法 {strategy.exit_algoid}")
    for job in (getattr(strategy, "jobs", {}) or {}).values():
        if getattr(job, "kind", "") in ("take_profit", "close") and not getattr(job, "done", False):
            lines.append(
                f"平仓槽位：算法 {job.algoid} {side(getattr(job, 'direction', 0))}"
                f"/{'平' if job.kind == 'close' else '止盈'} "
                f"{float(getattr(job, 'volume', 0) or 0):g}手"
                f"@{float(getattr(job, 'target_spread', 0) or 0):g}"
            )
    return lines


class ReconciliationDialog(QtWidgets.QDialog):
    """核对结果可能很长：用可滚动只读文本框，避免弹窗撑出屏幕或截断内容。"""

    def __init__(self, text: str, parent=None, on_clear=None) -> None:
        super().__init__(parent)
        self.on_clear = on_clear
        self.setWindowTitle("确认核对结果")
        self.setMinimumSize(560, 380)
        self.resize(820, 600)
        screen = QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(min(820, int(available.width() * 0.8)),
                        min(600, int(available.height() * 0.8)))

        layout = QtWidgets.QVBoxLayout(self)
        tip = QtWidgets.QLabel("请确认以下柜台状态。内容较多，可在框内滚动查看；确认前请与主终端核对。")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText(text)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        # 只读文本框仍可选中复制，方便把异常状态转给他人核对。
        self.view.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
            | QtCore.Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(self.view, 1)

        self.buttons = QtWidgets.QDialogButtonBox()
        self.confirm_button = self.buttons.addButton(
            "确认已核对", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.copy_button = self.buttons.addButton(
            "复制全部", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self.clear_button = None
        if callable(on_clear):
            self.clear_button = self.buttons.addButton(
                "清除遗留委托", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
            self.clear_button.clicked.connect(self._clear_leftovers)
        self.cancel_button = self.buttons.addButton(
            "取消", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.confirm_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.copy_button.setAutoDefault(False)
        if self.clear_button is not None:
            self.clear_button.setAutoDefault(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.copy_button.clicked.connect(self._copy_all)
        layout.addWidget(self.buttons)

    def _clear_leftovers(self) -> None:
        if callable(self.on_clear) and self.on_clear():
            self.reject()  # 记录已清，旧的核对内容不再展示

    def _copy_all(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.view.toPlainText())

    def plain_text(self) -> str:
        return self.view.toPlainText()


class SpreadConsoleWidget(QtWidgets.QWidget):
    # CTP 查询接口每秒限流；资金与持仓必须错开发送，拒绝时再退避重试。
    QUERY_GAP_MS = 1100
    QUERY_RETRIES = 3
    # 委托/成交回报到达时立即刷新详情页（跨线程用队列连接）。
    order_changed = QtCore.Signal()
    # 价差数据到达就刷新报价行：否则只能等 1 秒一次的界面定时器，看起来"比腿慢一倍"。
    quote_changed = QtCore.Signal(object)

    def __init__(self, main_engine, event_engine):
        super().__init__()
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.spread_engine = main_engine.get_engine(APP_NAME)
        self.control = SpreadTradingControl(main_engine, self.spread_engine)
        self._reconciliation_pending = False
        self._reconciliation_waits = 0
        self._order_refresh_queued = False
        self._quote_name = None
        self._batch_sync = False
        self._position_query_attempts = 0
        self.setWindowTitle("价差交易 · 跨期合约管理")
        self.setMinimumSize(1050, 660)
        self._build_ui()
        self._populate()
        self.order_changed.connect(
            self._flush_order_status, QtCore.Qt.ConnectionType.QueuedConnection)
        self.quote_changed.connect(
            self._flush_quote, QtCore.Qt.ConnectionType.QueuedConnection)
        if event_engine is not None and hasattr(event_engine, "register"):
            from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE
            from vnpy_spreadtrading.base import EVENT_SPREAD_DATA
            event_engine.register(EVENT_ORDER, self._on_order_event)
            event_engine.register(EVENT_TRADE, self._on_order_event)
            event_engine.register(EVENT_SPREAD_DATA, self._on_spread_event)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(1000)

    def _on_spread_event(self, event) -> None:
        """价差 tick 到达 → 立即更新报价行（只处理当前选中的价差）。"""
        item = getattr(event, "data", None)
        if getattr(item, "name", "") != self._quote_name:
            return
        self.quote_changed.emit(item)

    def _flush_quote(self, item) -> None:
        if getattr(item, "name", "") != self._quote_name:
            return
        self._set_quote(item.bid_price, item.ask_price,
                        item.bid_volume, item.ask_volume)

    def _set_quote(self, bid_price, ask_price, bid_volume=None, ask_volume=None) -> None:
        valid = bool(bid_price and ask_price)
        if bid_volume is not None and ask_volume is not None:
            valid = valid and bool(bid_volume) and bool(ask_volume)
        if valid:
            midpoint = (bid_price + ask_price) / 2
            self.quote.setText(
                f"买 {bid_price:g}  /  卖 {ask_price:g}  /  中间价 {midpoint:g}")
        else:
            self.quote.setText("等待两腿有效买一/卖一行情")

    def _on_order_event(self, _event) -> None:
        """委托/成交回报 → 立即刷新详情页（跨线程经队列连接回到 GUI 线程）。"""
        if self._order_refresh_queued:
            return
        self._order_refresh_queued = True
        self.order_changed.emit()

    def _flush_order_status(self) -> None:
        self._order_refresh_queued = False
        try:
            self._update_order_status()
        except Exception:
            pass  # 详情刷新失败不影响交易主流程

    def _update_order_status(self) -> None:
        name = self._name()
        config = None
        if name:
            try:
                config = self.control.service.get(name)
            except Exception:
                config = None
        if not config:
            self.order_status.setText("")
            return
        spread = self.spread_engine.get_spread(name)
        strategy = self.control._strategy(name)
        if strategy:
            pause = strategy.get_pause_state()
            base = (f"价差持仓 {getattr(spread, 'net_pos', 0):g}；"
                    f"暂停待核对：{'是' if pause['reconciliation_required'] else '否'}；"
                    f"历史算法在途 {len(pause['active_algoids'])}")
        else:
            base = "策略尚未注册；不得开启交易"
        base += "\n" + "\n".join(_slot_lines(strategy, getattr(self.main_engine, "get_order", None)))
        self.order_status.setText(base)

    def _build_ui(self):
        self.connection = QtWidgets.QLabel("行情未连接 · 交易未连接")
        self.master = QtWidgets.QPushButton("开始交易")
        self.master.clicked.connect(self._toggle_master)
        self.risk_button = QtWidgets.QPushButton("全局风控")
        self.risk_button.clicked.connect(self._edit_risk_limits)
        self.reconcile = QtWidgets.QPushButton("核对柜台状态")
        self.reconcile.clicked.connect(self._confirm_reconciliation)
        self.repair_position = QtWidgets.QPushButton("纠正所选合约")
        self.repair_position.clicked.connect(self._repair_selected_position)
        self.repair_all = QtWidgets.QPushButton("核对并纠正全部")
        self.repair_all.clicked.connect(self._repair_all_positions)
        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.connection)
        header.addStretch()
        header.addWidget(self.risk_button)
        header.addWidget(self.reconcile)
        header.addWidget(self.repair_position)
        header.addWidget(self.repair_all)
        header.addWidget(self.master)

        self.contracts = QtWidgets.QListWidget()
        self.contracts.setMinimumWidth(300)
        self.contracts.currentItemChanged.connect(lambda *_: self._show_selected())
        self.add_button = QtWidgets.QPushButton("添加合约")
        self.add_button.clicked.connect(self._add_contract)
        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("跨期合约"))
        left.addWidget(self.contracts)
        left.addWidget(self.add_button)
        left_widget = QtWidgets.QWidget()
        left_widget.setLayout(left)

        self.title = QtWidgets.QLabel("选择一个合约查看详情")
        self.title.setStyleSheet("font-size: 17px; font-weight: bold")
        self.quote = QtWidgets.QLabel("价差报价：等待两腿行情")
        self.legs = QtWidgets.QLabel("")
        self.params = QtWidgets.QLabel("")
        self.params.setWordWrap(True)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.order_status = QtWidgets.QLabel("")
        self.order_status.setWordWrap(True)
        self.per_contract = QtWidgets.QPushButton("允许此合约交易")
        self.per_contract.clicked.connect(self._toggle_contract)
        self.config_button = QtWidgets.QPushButton("查看与编辑策略参数")
        self.config_button.clicked.connect(self._edit_config)
        self.remove_button = QtWidgets.QPushButton("删除合约")
        self.remove_button.clicked.connect(self._remove_contract)
        self.detail = QtWidgets.QFormLayout()
        self.detail.addRow(self.title)
        self.detail.addRow("实时合成", self.quote)
        self.detail.addRow("行情来源", self.legs)
        self.detail.addRow("配置摘要", self.params)
        self.detail.addRow("交易与验证", self.status)
        self.detail.addRow("槽位与持仓", self.order_status)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.per_contract)
        buttons.addWidget(self.config_button)
        buttons.addWidget(self.remove_button)
        # 只有详情表单放进滚动区；三个操作按钮固定在下方，不随内容滚动。
        form_widget = QtWidgets.QWidget()
        form_widget.setLayout(self.detail)
        detail_scroll = QtWidgets.QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(form_widget)
        detail_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right = QtWidgets.QVBoxLayout()
        right.addWidget(detail_scroll, 1)
        right.addLayout(buttons)
        detail_widget = QtWidgets.QWidget()
        detail_widget.setLayout(right)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(1, 3)

        self.notice = QtWidgets.QLabel("添加合约即采集两腿 tick；默认不交易。停止交易会保留已有委托，且不会自动止损。")
        self.notice.setWordWrap(True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.addLayout(header)
        outer.addWidget(splitter)
        outer.addWidget(self.notice)

    def _name(self):
        item = self.contracts.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None

    def _populate(self, selected=None):
        selected = selected or self._name()
        self.contracts.clear()
        try:
            configs = self.control.service.list()
        except ManagedSpreadError as exc:
            self.notice.setText(str(exc))
            return
        for config in configs.values():
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, config.name)
            self.contracts.addItem(item)
        for index in range(self.contracts.count()):
            if self.contracts.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == selected:
                self.contracts.setCurrentRow(index)
                break
        if self.contracts.currentRow() < 0 and self.contracts.count():
            self.contracts.setCurrentRow(0)
        self._refresh()

    def _add_contract(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "添加跨期合约", "交易所.完整组合代码（如 DCE.SP a2701&a2703）")
        if not ok:
            return
        try:
            config, created = self.control.add(text)
            self._populate(config.name)
            if config.name in self.control.errors:
                self.notice.setText(self.control.errors[config.name])
            else:
                self.notice.setText("合约已添加并自动订阅两腿；待行情及柜台验证后方可启用交易" if created else "合约已存在，已切换至该合约")
        except ManagedSpreadError as exc:
            QtWidgets.QMessageBox.warning(self, "无法添加合约", str(exc))

    def _edit_risk_limits(self):
        limits = load_risk_limits()
        usage = account_usage(self.main_engine)
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("全局资金占用上限")
        form = QtWidgets.QFormLayout(dialog)

        enabled = QtWidgets.QCheckBox("启用：达到已用资金上限后不再开仓/加仓")
        enabled.setChecked(limits.enabled)
        ratio = QtWidgets.QDoubleSpinBox()
        ratio.setRange(1, 100)
        ratio.setDecimals(1)
        ratio.setSuffix(" %")
        ratio.setValue(limits.max_margin_ratio * 100)
        current = QtWidgets.QLabel(
            f"当前已用资金 {usage.ratio:.1%}（权益 {usage.balance:,.0f} / "
            f"可用 {usage.available:,.0f}）"
            if usage is not None else
            "当前没有账户资金数据；启用后策略会拒绝开仓，直到资金数据返回"
        )
        current.setWordWrap(True)
        hint = QtWidgets.QLabel(
            "口径：已用资金 = 权益 − 可用资金（含占用保证金、冻结现金/手续费）。"
            "只拦截开仓/加仓，平仓、止盈、止损不受影响。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666")

        form.addRow(enabled)
        form.addRow("已用资金上限", ratio)
        form.addRow(current)
        form.addRow(hint)
        actions = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        form.addRow(actions)

        if dialog.exec_() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            save_risk_limits(RiskLimits(
                enabled=enabled.isChecked(),
                max_margin_ratio=ratio.value() / 100.0,
            ))
            self.notice.setText(
                f"全局风控已保存：{'启用' if enabled.isChecked() else '关闭'}，"
                f"已用资金上限 {ratio.value():.1f}%"
            )
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "保存失败", str(exc))
        self._refresh()

    def _confirm_reconciliation(self, batch: bool = False):
        if self._reconciliation_pending:
            return
        if not self.control.md_ready() or not self.control.td_ready():
            QtWidgets.QMessageBox.warning(self, "无法查询", "请先连接交易端和行情端")
            return
        self._batch_sync = bool(batch)
        gateway = self.control._gateway()
        # 不在这里提前清空旧快照：查询本身会在发出请求时作废旧快照。
        # 若柜台限流导致本轮查询没发出去，上一次已核对的快照仍然有效，
        # 不会因为多点了一次就丢失核对结果。
        self._reconciliation_pending = True
        self._reconciliation_waits = 0
        self._position_query_attempts = 0
        self._order_query_attempts = 0
        self.reconcile.setEnabled(False)
        self.reconcile.setText("查询柜台中…")
        self.notice.setText("已向柜台查询资金，稍后查询持仓（避免柜台限流）…")
        try:
            if hasattr(gateway, "query_account"):
                gateway.query_account()
        except Exception as exc:
            self._fail_reconciliation("查询请求失败", f"无法向柜台发起资金查询：{exc}")
            return
        QtCore.QTimer.singleShot(self.QUERY_GAP_MS, self._request_position_query)

    def _request_position_query(self):
        """单独发起持仓查询；柜台限流（-1/-2/-3）时退避重试，不当作空仓。"""
        try:
            self.control._gateway().query_position()
        except Exception as exc:
            self._position_query_attempts += 1
            if getattr(exc, "retryable", False) and self._position_query_attempts <= self.QUERY_RETRIES:
                self.notice.setText(
                    f"柜台限流，正在重试持仓查询（{self._position_query_attempts}/{self.QUERY_RETRIES}）…"
                )
                QtCore.QTimer.singleShot(1300, self._request_position_query)
                return
            self._fail_reconciliation("查询请求失败", f"无法向柜台发起持仓查询：{exc}")
            return
        self.notice.setText("已查询资金/持仓，正在查询当日委托…")
        QtCore.QTimer.singleShot(self.QUERY_GAP_MS, self._request_order_query)

    def _request_order_query(self):
        """查询当日委托，拿到每笔单的最终状态（CTP 的 ReqQryOrder）。"""
        td = getattr(self.control._gateway(), "td_api", None)
        if td is None or not hasattr(td, "query_orders"):
            self._show_reconciliation_result()
            return
        try:
            self.control._gateway().query_orders()
        except Exception as exc:
            self._order_query_attempts = getattr(self, "_order_query_attempts", 0) + 1
            if getattr(exc, "retryable", False) and self._order_query_attempts <= self.QUERY_RETRIES:
                self.notice.setText("委托查询被柜台限流，正在重试…")
                QtCore.QTimer.singleShot(1300, self._request_order_query)
                return
            self.notice.setText(f"委托查询失败（{exc}）；仍按持仓核对，历史委托状态未知")
            self._show_reconciliation_result()
            return
        self.notice.setText("已查询当日委托，正在汇总全部合约…")
        QtCore.QTimer.singleShot(900, self._show_reconciliation_result)

    def _fail_reconciliation(self, title: str, text: str):
        self._reconciliation_pending = False
        self._batch_sync = False
        self.reconcile.setEnabled(True)
        self.reconcile.setText("核对柜台状态")
        QtWidgets.QMessageBox.warning(self, title, text)
        self._refresh()


    def _show_reconciliation_result(self):
        try:
            report = self.control.reconciliation_report()
        except Exception as exc:
            self._reconciliation_pending = False
            self.reconcile.setEnabled(True)
            self.reconcile.setText("核对柜台状态")
            QtWidgets.QMessageBox.warning(self, "读取核对结果失败", str(exc))
            self._refresh()
            return

        # 本地仓位纠正必须等 CTP 持仓查询完整结束（含空持仓回报），不能用旧缓存。
        td = getattr(self.control._gateway(), "td_api", None)
        if getattr(td, "orders_query_pending", False):
            if self._reconciliation_waits < 12:
                self._reconciliation_waits += 1
                self.notice.setText("等待柜台返回当日委托…")
                QtCore.QTimer.singleShot(800, self._show_reconciliation_result)
                return
            td.orders_query_pending = False  # 超时：不再等，按已有回报继续
        if getattr(td, "position_query_pending", False):
            if self._reconciliation_waits < 12:
                self._reconciliation_waits += 1
                self.notice.setText("等待柜台完整返回持仓快照…")
                QtCore.QTimer.singleShot(800, self._show_reconciliation_result)
                return
            self._fail_reconciliation("持仓查询未完成", "柜台持仓查询未完整返回，不能确认核对或纠正本地仓位；请再点一次“核对柜台状态”")
            return
        problem = self.control._snapshot_problem(td)
        if problem:
            self._fail_reconciliation("持仓快照不可用", f"{problem}；请重新点“核对柜台状态”")
            return
        # 资金查询独立异步返回，继续短暂等待账户资金。
        if report["account_known"] and not report["accounts"] and self._reconciliation_waits < 12:
            self._reconciliation_waits += 1
            self.notice.setText("等待柜台返回资金/持仓数据…")
            QtCore.QTimer.singleShot(800, self._show_reconciliation_result)
            return

        self._reconciliation_pending = False
        self.reconcile.setEnabled(True)
        self.reconcile.setText("核对柜台状态")

        lines: list[str] = []
        if report["accounts"]:
            for account in report["accounts"]:
                lines.append(
                    f"账户 {account['gateway']}：可用 {account['available']:,.0f} 元，"
                    f"权益 {account['balance']:,.0f} 元"
                )
        elif report["account_known"]:
            lines.append("账户：柜台尚未返回资金数据")
        else:
            lines.append("账户：当前接口未提供账户查询")

        if report["contracts"]:
            for item in report["contracts"]:
                state = "有敞口，开启该合约会被拒绝" if not item["ok"] else "无敞口"
                lines.append(
                    f"\n{item['display']}（验证 {item['verify']}）：{state}\n"
                    f"在途委托 {item['orders']}，单腿持仓 {item['positions']}，"
                    f"引擎净仓 {item['net_pos']:g}，策略层 {item['layers']}，"
                    f"在途算法 {item['active_algos']}"
                )
                if item.get("leg_differences") is not None:
                    for leg in item["leg_differences"]:
                        if abs(leg["difference"]) > 1e-9 or (leg["long"] > 1e-9 and leg["short"] > 1e-9):
                            lines.append(
                                f"持仓差异 {leg['symbol']}：本地 {leg['local']:g} 手，"
                                f"柜台净仓 {leg['cabinet']:g} 手（多 {leg['long']:g} / 空 {leg['short']:g}）；"
                                "本地仓位不能替代柜台仓位"
                            )
                else:
                    lines.append("持仓差异：尚无本次完整柜台快照，不能判断本地多仓或少仓")
                for title, values in (
                    ("在途委托明细", item["order_details"]),
                    ("单腿持仓明细", item["position_details"]),
                    ("策略层明细", item["layer_details"]),
                    ("在途算法", item["algo_details"]),
                    ("暂停期间未终结委托", item["pending_details"]),
                ):
                    if values:
                        lines.append(f"{title}：\n  - " + "\n  - ".join(values[:8]))
        else:
            lines.append("合约：注册表为空")

        text = "\n".join(lines)
        if report["warnings"]:
            text += "\n\n注意：\n- " + "\n- ".join(report["warnings"][:8])
        if report.get("notes"):
            text += (
                "\n\n状态提醒（账户本来的持仓/在途委托，不算敞口，不影响核对）：\n- "
                + "\n- ".join(report["notes"][:8])
            )
        if report["exposures"]:
            text += (
                "\n\n有敞口的合约（引擎与柜台对不齐，开启交易会被拒绝）：\n- "
                + "\n- ".join(report["exposures"][:8])
            )
        text += (
            "\n\n核对确认只记录账户状态，不会撤销委托、不会平仓，也不会把对不齐的合约变成可交易。"
            "\n程序只能展示已查询到的状态，请再与主终端的委托/成交记录核对。"
        )
        dialog = ReconciliationDialog(text, self)
        accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if accepted:
            try:
                self.control.confirm_reconciliation()
                unresolved = [item["name"] for item in report["contracts"]
                              if not self.control.is_reconciled(item["name"])]
                if self._batch_sync:
                    self._run_batch_sync(unresolved)
                elif unresolved:
                    self.notice.setText(
                        f"已核对可比对合约；{', '.join(unresolved)} 尚缺完整逐腿数据，交易保持关闭")
                elif report["exposures"]:
                    self.notice.setText(
                        "柜台核对已确认；有敞口的合约仍不能开启交易，无敞口合约可逐个开启")
                else:
                    self.notice.setText("柜台核对已确认；可逐个开启合约交易许可")
            except Exception as exc:
                QtWidgets.QMessageBox.warning(self, "核对确认失败", str(exc))
        self._batch_sync = False
        self._refresh()

    def _run_batch_sync(self, unresolved: list[str]) -> None:
        """核对确认后，一次性把全部合约的本地持仓/委托对齐到柜台。"""
        results = self.control.sync_all(force=True)
        ok = [name for name, kind, _ in results if kind == "ok"]
        skip = [name for name, kind, _ in results if kind == "skip"]
        fails = [(name, message) for name, kind, message in results if kind == "fail"]
        summary = f"核对并纠正完成：成功 {len(ok)}，无需处理 {len(skip)}"
        if fails:
            summary += f"，失败 {len(fails)}"
        if unresolved:
            summary += f"；缺完整逐腿数据：{', '.join(unresolved)}"
        self.notice.setText(summary)
        if fails:
            QtWidgets.QMessageBox.warning(
                self, "部分合约未纠正",
                summary + "\n\n" + "\n\n".join(f"{name}：{message}" for name, message in fails[:6]),
            )

    def _repair_all_positions(self):
        """一次完成“核对 + 全部合约纠正”，不用逐个点。"""
        if self._reconciliation_pending:
            QtWidgets.QMessageBox.information(
                self, "柜台查询中", "请等待本轮查询完成")
            return
        self.notice.setText("正在核对全部合约，确认后将自动纠正…")
        self._confirm_reconciliation(batch=True)

    def _repair_selected_position(self):
        name = self._name()
        if not name:
            QtWidgets.QMessageBox.information(self, "请选择合约", "先选中需要纠正的合约")
            return
        if self._reconciliation_pending:
            QtWidgets.QMessageBox.information(self, "柜台查询中", "请等待持仓查询完成并确认核对")
            return
        answer = QtWidgets.QMessageBox.question(
            self, "按柜台同步本地持仓",
            f"把 {name} 的本地账对齐到本次柜台快照：\n"
            "· 柜台两腿空仓 → 清零本地价差并归档旧层账；\n"
            "· 柜台两腿数量匹配 → 按柜台仓位同步本地价差，并生成接管层（用柜台两腿均价）；\n"
            "· 两腿不匹配或多空不分 → 拒绝，需人工处理。\n\n"
            "若仍有在途委托，会先提醒一次；确认后照样同步（不会撤单或平仓）。"
            "核对确认状态会保留，同步后可直接开启交易。确认继续？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.notice.setText(self.control.sync_spread_position(name))
        except Exception as exc:
            if "强制纠正" in str(exc):
                confirm = QtWidgets.QMessageBox.warning(
                    self, "仍有在途委托",
                    f"{exc}\n\n仍要按柜台快照同步本地持仓吗？",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if confirm == QtWidgets.QMessageBox.StandardButton.Yes:
                    try:
                        self.notice.setText(
                            self.control.sync_spread_position(name, force=True))
                    except Exception as forced_exc:
                        QtWidgets.QMessageBox.warning(self, "纠正未执行", str(forced_exc))
                return self._refresh()
            QtWidgets.QMessageBox.warning(self, "纠正未执行", str(exc))
        self._refresh()

    def _toggle_contract(self):
        name = self._name()
        if not name:
            return
        enabled = name not in self.control.enabled
        try:
            self.control.enable(name, enabled)
            self.notice.setText("该合约已获交易许可；仍须点击总开始交易" if enabled else "已暂停此合约策略；原委托保留，仍可能成交")
        except Exception as exc:
            forced = False
            if enabled:
                answer = QtWidgets.QMessageBox.warning(
                    self, "检查未通过",
                    f"{exc}\n\n仍要开启该合约的交易许可吗？\n"
                    "未核对的部分不会被修复，可能出现错误开平仓。",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                    try:
                        self.control.enable(name, True, force=True)
                        self.notice.setText("已强制开启该合约交易许可（检查未通过，请自行确认仓位）")
                        forced = True
                    except Exception as forced_exc:
                        exc = forced_exc
            if not forced:
                QtWidgets.QMessageBox.warning(self, "交易许可未开启", str(exc))
        self._refresh()

    def _toggle_master(self):
        if self.control.master_running:
            self.control.stop_all()
            self.notice.setText("策略已暂停：现有委托不撤销，可能继续成交；已有持仓不再自动止损")
        else:
            names = sorted(self.control.enabled)
            if not names:
                QtWidgets.QMessageBox.information(self, "没有可启动的合约", "先逐个开启合约的交易许可")
                return
            force = False
            try:
                for name in names:
                    self.control.validate(name)
            except Exception as exc:
                answer = QtWidgets.QMessageBox.warning(
                    self, "启动检查未通过",
                    f"{exc}\n\n仍要强制开始吗？\n"
                    "强制开始不会修复仓位/委托，只会跳过这次检查。",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
                force = True
            answer = QtWidgets.QMessageBox.question(
                self, "确认实盘交易", "即将为以下合约启用实盘策略：\n" + "\n".join(names)
                + "\n\n停止后旧委托不会自动撤销，也不会自动平仓。确认开始？")
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            try:
                started = self.control.start_all(force=force)
                self.notice.setText("策略已启用：" + "、".join(started))
            except Exception as exc:
                text = str(exc)
                if "报价" in text or "买一" in text or "卖一" in text:
                    answer = QtWidgets.QMessageBox.warning(
                        self, "行情报价不可用",
                        f"{text}\n\n休盘或断线时没有有效报价。仍要启动策略吗？\n"
                        "启动后在有新报价前不会发单。",
                        QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                        QtWidgets.QMessageBox.StandardButton.No,
                    )
                    if answer == QtWidgets.QMessageBox.StandardButton.Yes:
                        try:
                            started = self.control.start_all(force=True)
                            self.notice.setText("策略已启用（行情恢复后才会计价发单）：" + "、".join(started))
                        except Exception as forced_exc:
                            QtWidgets.QMessageBox.warning(
                                self, "启动失败，已暂停策略", str(forced_exc))
                    return self._refresh()
                QtWidgets.QMessageBox.warning(self, "启动失败，已暂停策略", text)
        self._refresh()

    def _edit_config(self):
        name = self._name()
        if not name:
            return
        config = self.control.service.get(name)
        if not config:
            return
        strategy = self.control._strategy(name)
        pause = strategy.get_pause_state() if strategy else None
        warnings: list[str] = []
        if self.control.master_running and name in self.control.enabled:
            warnings.append("策略正在运行：参数改动要重新开始交易才生效。")
        if (
            self.control._unreconciled_account(config)
            or (pause and (pause["active_algoids"] or pause["orders"]
                           or pause["trades_by_order"] or pause["layers"]))
        ):
            warnings.append("该合约仍有持仓、暂停成交或在途委托；改参数不会修复这些状态，请另行核对。")
        if warnings:
            answer = QtWidgets.QMessageBox.warning(
                self, "提醒",
                "\n".join(warnings) + "\n\n仍要打开参数编辑吗？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        def number(key, default):
            try:
                return float(config.strategy.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f"{config.display_name or name} · 策略参数")
        dialog.resize(620, 760)
        outer = QtWidgets.QVBoxLayout(dialog)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        info = QtWidgets.QLabel(
            f"近月：{config.near_symbol}    远月：{config.far_symbol}\n"
            f"组合：{config.exchange_spread_vt_symbol or '—'}"
        )
        body.addWidget(info)

        signal_box = QtWidgets.QGroupBox("信号窗口与分层")
        signal_form = QtWidgets.QFormLayout(signal_box)
        mean_minutes = QtWidgets.QSpinBox()
        mean_minutes.setRange(1, 1440)
        mean_minutes.setSuffix(" 分钟")
        mean_minutes.setValue(int(number("mean_window_minutes", 3)))
        sd_minutes = QtWidgets.QSpinBox()
        sd_minutes.setRange(1, 1440)
        sd_minutes.setSuffix(" 分钟")
        sd_minutes.setValue(int(number("sd_window_minutes", 30)))
        entry_k = QtWidgets.QDoubleSpinBox()
        entry_k.setRange(0.1, 10.0)
        entry_k.setDecimals(2)
        entry_k.setSingleStep(0.1)
        entry_k.setSuffix(" sd")
        entry_k.setValue(number("entry_k", 1.0))
        layer_step = QtWidgets.QDoubleSpinBox()
        layer_step.setRange(0.0, 10.0)
        layer_step.setDecimals(2)
        layer_step.setSingleStep(0.05)
        layer_step.setSuffix(" sd/层")
        layer_step.setValue(number("layer_step", 0.3))
        signal_form.addRow("N 均值窗口", mean_minutes)
        signal_form.addRow("M 标准差窗口", sd_minutes)
        signal_form.addRow("X 开仓阈值", entry_k)
        signal_form.addRow("层距", layer_step)
        body.addWidget(signal_box)

        position_box = QtWidgets.QGroupBox("仓位与风控")
        position_form = QtWidgets.QFormLayout(position_box)
        max_positions = QtWidgets.QSpinBox()
        max_positions.setRange(1, 100)
        max_positions.setValue(int(number("max_positions", 5)))
        layer_volume = QtWidgets.QDoubleSpinBox()
        layer_volume.setRange(1, 1000)
        layer_volume.setValue(number("layer_volume", 1))
        stop_loss = QtWidgets.QDoubleSpinBox()
        stop_loss.setRange(1, 100_000_000)
        stop_loss.setDecimals(0)
        stop_loss.setValue(
            config.stop_loss if config.stop_loss > 0 else number("stop_loss", 10000)
        )
        margin_per_lot = QtWidgets.QDoubleSpinBox()
        margin_per_lot.setRange(0, 100_000_000)
        margin_per_lot.setDecimals(0)
        margin_per_lot.setValue(config.margin_per_lot)
        margin_per_lot.setSpecialValueText("未填写")
        per_layer = QtWidgets.QLabel()
        per_layer.setStyleSheet("color: #666")

        def refresh_per_layer():
            per_layer.setText(
                f"整仓止损 ÷ 最多层数 = {stop_loss.value() / max(1, max_positions.value()):.0f} 元/层"
            )

        stop_loss.valueChanged.connect(refresh_per_layer)
        max_positions.valueChanged.connect(refresh_per_layer)
        refresh_per_layer()
        position_form.addRow("最多持仓层数", max_positions)
        position_form.addRow("每层手数", layer_volume)
        position_form.addRow("整仓止损（元）", stop_loss)
        position_form.addRow("每层止损", per_layer)
        position_form.addRow("每手保证金（元）", margin_per_lot)
        body.addWidget(position_box)

        exit_box = QtWidgets.QGroupBox("退出与挂单超时")
        exit_form = QtWidgets.QFormLayout(exit_box)
        max_hold_days = QtWidgets.QSpinBox()
        max_hold_days.setRange(0, 99999)
        max_hold_days.setValue(max(0, int(number("max_hold_days", 10))))
        min_exit_pnl = QtWidgets.QDoubleSpinBox()
        min_exit_pnl.setRange(0, 100_000_000)
        min_exit_pnl.setDecimals(0)
        min_exit_pnl.setValue(number("min_exit_pnl_override", 0.0))
        min_exit_pnl.setSpecialValueText("自适应")
        close_timeout = QtWidgets.QDoubleSpinBox()
        close_timeout.setRange(0, 1_000_000_000)
        close_timeout.setDecimals(1)
        close_timeout.setSuffix(" 秒")
        close_timeout.setValue(number("market_close_timeout", 5.0))
        close_timeout.setSpecialValueText("不启用")
        entry_timeout = QtWidgets.QDoubleSpinBox()
        entry_timeout.setRange(0, 1_000_000_000)
        entry_timeout.setDecimals(1)
        entry_timeout.setSuffix(" 秒")
        entry_timeout.setValue(number("entry_partial_timeout", 10.0))
        entry_timeout.setSpecialValueText("不启用")
        close_only = QtWidgets.QCheckBox("只平仓：忽略所有开仓/加仓信号")
        close_only.setChecked(bool(config.strategy.get("close_only", False)))
        exit_preplace = QtWidgets.QCheckBox("开/加仓成交后立即预挂止盈")
        exit_preplace.setChecked(bool(config.strategy.get("exit_preplace", True)))
        exit_form.addRow("最大持仓日", max_hold_days)
        exit_form.addRow("净止盈金额（元/层）", min_exit_pnl)
        exit_form.addRow("平仓单超时（0=不撤不重发）", close_timeout)
        exit_form.addRow("开仓部分成交超时（0=不撤）", entry_timeout)
        exit_form.addRow(close_only)
        exit_form.addRow(exit_preplace)
        body.addWidget(exit_box)

        table_spec = FeeCalculator().spec(
            config.near_symbol, config.far_symbol, slug=config.slug)
        fee_record = config.fee or {}
        fee_box = QtWidgets.QGroupBox("手续费（影响预挂止盈价）")
        fee_form = QtWidgets.QFormLayout(fee_box)
        fee_custom = QtWidgets.QCheckBox("自定义手续费（不勾选用品种费率表）")
        fee_custom.setChecked(bool(fee_record))
        fee_unit = QtWidgets.QComboBox()
        fee_unit.addItems([UNIT_FIXED, UNIT_RATE])
        fee_unit.setCurrentText(str(fee_record.get("unit") or table_spec.unit))
        fee_open = QtWidgets.QDoubleSpinBox()
        fee_open.setRange(0, 1_000_000)
        fee_open.setDecimals(4)
        fee_open.setValue(float(fee_record.get("open", table_spec.open)))
        fee_close_yesterday = QtWidgets.QDoubleSpinBox()
        fee_close_yesterday.setRange(0, 1_000_000)
        fee_close_yesterday.setDecimals(4)
        fee_close_yesterday.setValue(
            float(fee_record.get("close_yesterday", table_spec.close_yesterday)))
        fee_close_today = QtWidgets.QDoubleSpinBox()
        fee_close_today.setRange(0, 1_000_000)
        fee_close_today.setDecimals(4)
        fee_close_today.setValue(
            float(fee_record.get("close_today", table_spec.close_today)))
        fee_hint = QtWidgets.QLabel(
            f"费率表 {table_spec.unit}：开 {table_spec.open:g} / 平昨 "
            f"{table_spec.close_yesterday:g} / 平今 {table_spec.close_today:g} 每手每边"
            "\n来源：交易所标准费率表（不含期货公司加收）"
        )
        fee_hint.setStyleSheet("color: #666")
        fee_hint.setWordWrap(True)

        def refresh_fee_enabled():
            enabled = fee_custom.isChecked()
            for widget in (fee_unit, fee_open, fee_close_yesterday, fee_close_today):
                widget.setEnabled(enabled)

        fee_custom.toggled.connect(refresh_fee_enabled)
        refresh_fee_enabled()
        fee_form.addRow(fee_custom)
        fee_form.addRow("计费单位", fee_unit)
        fee_form.addRow("开仓费", fee_open)
        fee_form.addRow("平昨费", fee_close_yesterday)
        fee_form.addRow("平今费", fee_close_today)
        fee_form.addRow(fee_hint)
        body.addWidget(fee_box)

        filter_box = QtWidgets.QGroupBox("盘口过滤")
        filter_form = QtWidgets.QFormLayout(filter_box)
        width_ticks = QtWidgets.QDoubleSpinBox()
        width_ticks.setRange(0, 100)
        width_ticks.setDecimals(2)
        width_ticks.setSuffix(" tick")
        width_ticks.setValue(number("width_ticks", 1.0))
        min_depth = QtWidgets.QDoubleSpinBox()
        min_depth.setRange(0, 1_000_000)
        min_depth.setDecimals(2)
        min_depth.setValue(number("min_depth", 1.0))
        vr_max = QtWidgets.QDoubleSpinBox()
        vr_max.setRange(0, 100)
        vr_max.setDecimals(2)
        vr_max.setValue(config.vr_max or number("vr_max", 0.0))
        vr_max.setSpecialValueText("不限制")
        filter_form.addRow("盘口宽度上限", width_ticks)
        filter_form.addRow("最小深度（手）", min_depth)
        filter_form.addRow("量比上限", vr_max)
        body.addWidget(filter_box)

        contract_box = QtWidgets.QGroupBox("合约与备注")
        contract_form = QtWidgets.QFormLayout(contract_box)
        size_text = f"{config.size:g}" if config.size > 0 else "待柜台验证（启动交易时自动取单腿合约）"
        tick_text = f"{config.pricetick:g}" if config.pricetick > 0 else "待柜台验证"
        contract_form.addRow("合约乘数", QtWidgets.QLabel(size_text))
        contract_form.addRow("最小价格变动", QtWidgets.QLabel(tick_text))
        contract_form.addRow("执行方式", QtWidgets.QLabel("交易所组合单（柜台验证后启用；不自动退两腿）"))
        remark = QtWidgets.QLineEdit(config.remark)
        remark.setMaxLength(120)
        remark.setPlaceholderText("中文备注，例如：a 1-3 正套，重点观察")
        contract_form.addRow("中文备注", remark)
        body.addWidget(contract_box)

        actions = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        actions.accepted.connect(dialog.accept)
        actions.rejected.connect(dialog.reject)
        outer.addWidget(actions)

        if dialog.exec_() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        strategy_values = {
            "mean_window_minutes": mean_minutes.value(),
            "sd_window_minutes": sd_minutes.value(),
            "entry_k": entry_k.value(),
            "layer_step": layer_step.value(),
            "max_positions": max_positions.value(),
            "layer_volume": layer_volume.value(),
            "width_ticks": width_ticks.value(),
            "min_depth": min_depth.value(),
            "vr_max": vr_max.value(),
            "max_hold_days": max_hold_days.value(),
            "min_exit_pnl_override": min_exit_pnl.value(),
            "market_close_timeout": close_timeout.value(),
            "entry_partial_timeout": entry_timeout.value(),
            "close_only": close_only.isChecked(),
            "exit_preplace": exit_preplace.isChecked(),
        }
        try:
            old_mean = int(strategy.mean_window_minutes) if strategy else int(number("mean_window_minutes", 3))
        except (TypeError, ValueError):
            old_mean = int(number("mean_window_minutes", 3))
        try:
            old_sd = int(strategy.sd_window_minutes) if strategy else int(number("sd_window_minutes", 30))
        except (TypeError, ValueError):
            old_sd = int(number("sd_window_minutes", 30))
        fee_values = (
            {
                "unit": fee_unit.currentText(),
                "open": fee_open.value(),
                "close_yesterday": fee_close_yesterday.value(),
                "close_today": fee_close_today.value(),
            }
            if fee_custom.isChecked() else {}
        )
        try:
            self.control.service.edit_settings(
                name,
                strategy=strategy_values,
                stop_loss=stop_loss.value(),
                vr_max=vr_max.value(),
                remark=remark.text(),
                margin_per_lot=margin_per_lot.value(),
                fee=fee_values,
            )
            reset_note = ""
            if strategy:
                strategy.update_setting(strategy_values)
                strategy.live_trading = False
                strategy._load_static_config()
                strategy._rebuild_filters()
                if (mean_minutes.value(), sd_minutes.value()) != (old_mean, old_sd):
                    strategy._rebuild_stat()
                    reset_note = "；统计窗口已重置，需重新积累 N/M 分钟行情"
            self.notice.setText("参数已保存并更新当前暂停策略；重新核对柜台后方可启动" + reset_note)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "保存失败", str(exc))
        self._refresh()

    def _remove_contract(self):
        name = self._name()
        if not name:
            return
        config = self.control.service.get(name)
        if not config:
            return
        if self.control.master_running:
            QtWidgets.QMessageBox.warning(self, "请先停止交易", "删除合约前必须先停止总交易，避免移除仍在发单的策略")
            return
        text = (
            f"即将删除：{config.display_name or name}\n"
            f"{config.near_symbol} − {config.far_symbol}\n\n"
            "将同时移除策略实例、价差注册和 config/spreads.json 条目；"
            "已采集的历史 tick 不会被删除。\n"
            "若仍有在途委托、算法或持仓，会先提示并等你确认；"
            "本程序从不自动撤单或平仓，这些会留在柜台由你管理。"
        )
        if not self.control.td_ready():
            text += "\n\n警告：交易端未连接，程序无法核对柜台持仓。仅在您已确认账户无该组合持仓时才继续。"
        reply = QtWidgets.QMessageBox.warning(
            self, "确认删除合约", text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.control.remove(name, force_offline=not self.control.td_ready())
            self._populate()
            self.notice.setText(f"已删除 {removed.display_name or removed.name}；历史 tick 保留在数据库中")
        except Exception as exc:
            if "强制删除" in str(exc):
                confirm = QtWidgets.QMessageBox.warning(
                    self, "仍有未了结状态",
                    f"{exc}\n\n删除后本程序不再跟踪该合约的委托和持仓，"
                    "委托仍可能在柜台成交、仓位仍需你自己管理。确实要删除吗？",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if confirm == QtWidgets.QMessageBox.StandardButton.Yes:
                    try:
                        removed = self.control.remove(
                            name, force_offline=not self.control.td_ready(),
                            force_exposure=True)
                        self._populate()
                        self.notice.setText(
                            f"已删除 {removed.display_name or removed.name}；"
                            "柜台委托/持仓不再由本程序跟踪")
                        return
                    except Exception as forced_exc:
                        exc = forced_exc
            QtWidgets.QMessageBox.warning(self, "删除失败", str(exc))
            self._refresh()

    def _show_selected(self):
        self._refresh()

    def _describe_config(self, config):
        strategy = config.strategy or {}

        def raw(key, default):
            value = strategy.get(key, default)
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)

        try:
            max_positions = max(1, int(float(strategy.get("max_positions", 5))))
        except (TypeError, ValueError):
            max_positions = 5
        try:
            layer_volume = float(strategy.get("layer_volume", 1) or 1)
        except (TypeError, ValueError):
            layer_volume = 1.0
        try:
            stop_loss = config.stop_loss if config.stop_loss > 0 else float(strategy.get("stop_loss", 10000))
        except (TypeError, ValueError):
            stop_loss = 10000.0
        margin = config.margin_per_lot
        margin_text = (
            f"{margin:g} 元/手（满层约 {margin * layer_volume * max_positions:g} 元）"
            if margin > 0 else "未填写"
        )
        try:
            vr = config.vr_max if config.vr_max > 0 else float(strategy.get("vr_max", 0) or 0)
        except (TypeError, ValueError):
            vr = 0.0
        close_only = bool(strategy.get("close_only", False))
        exit_preplace = bool(strategy.get("exit_preplace", True))
        close_timeout = float(strategy.get("market_close_timeout", 5.0) or 0.0)
        entry_timeout = float(strategy.get("entry_partial_timeout", 10.0) or 0.0)
        close_text = "不启用" if close_timeout <= 0 else f"{close_timeout:g}s"
        entry_text = "不启用" if entry_timeout <= 0 else f"{entry_timeout:g}s"
        table_spec = _FEE_CALC.spec(
            config.near_symbol, config.far_symbol, slug=config.slug)
        fee_spec = (
            spec_from_record(config.fee, table_spec) if config.fee else table_spec
        )
        fee_source = "自定义" if config.fee else "费率表"
        return (
            f"N={raw('mean_window_minutes', 3)} 分钟  "
            f"M={raw('sd_window_minutes', 30)} 分钟  "
            f"X={raw('entry_k', 1.0)} sd  层距={raw('layer_step', 0.3)} sd  |  "
            f"层数≤{max_positions}  每层{layer_volume:g}手  "
            f"整仓止损={stop_loss:g} 元（{stop_loss / max_positions:.0f} 元/层）  |  "
            f"盘口≤{raw('width_ticks', 1.0)} tick  "
            f"深度≥{raw('min_depth', 1.0)}  量比≤{'不限' if vr <= 0 else f'{vr:g}'}  |  "
            f"保证金/手={margin_text}  |  "
            f"手续费={fee_source} {fee_spec.unit} "
            f"{fee_spec.open:g}/{fee_spec.close_yesterday:g}/{fee_spec.close_today:g}\n"
            f"退出：{'只平仓' if close_only else '可开仓'}  "
            f"预挂止盈={'是' if exit_preplace else '否'}  "
            f"最大持仓 {raw('max_hold_days', 10)} 日  "
            f"超时：平仓 {close_text} / 开仓部分成交 {entry_text}"
        )

    def _refresh(self):
        try:
            self.control.refresh()
        except Exception as exc:
            try:
                self.control.stop_all(reason="界面状态刷新失败")
            except Exception:
                pass  # 配置损坏时仍展示原始错误，禁止 GUI 继续交易
            self.notice.setText(f"价差状态刷新失败，已暂停策略：{exc}")
        self.connection.setText(f"行情：{'已连接' if self.control.md_ready() else '未连接'}   交易：{'已连接' if self.control.td_ready() else '未连接'}")
        self.master.setText("停止交易" if self.control.master_running else "开始交易")
        if not self._reconciliation_pending:
            moment = self.control.last_reconciliation_at
            self.reconcile.setText(
                f"已核对 {moment:%H:%M:%S}" if isinstance(moment, datetime)
                else "核对柜台状态"
            )
        try:
            configs = self.control.service.list()
        except ManagedSpreadError:
            return
        for index in range(self.contracts.count()):
            item = self.contracts.item(index)
            config = configs.get(item.data(QtCore.Qt.ItemDataRole.UserRole))
            if not config:
                continue
            strategy = self.control._strategy(config.name)
            is_on = bool(config.name in self.control.enabled and self.control.master_running
                         and strategy and strategy.trading and strategy.live_trading and not strategy.paused)
            remark_text = f"  ·  {config.remark}" if config.remark else ""
            item.setText(
                f"{'●' if is_on else '○'}  "
                f"{config.display_name or config.exchange_spread or config.name}{remark_text}"
            )
        name = self._name()
        config = configs.get(name)
        self.per_contract.setEnabled(bool(config))
        self.config_button.setEnabled(bool(config))
        self.remove_button.setEnabled(bool(config) and not self.control.master_running)
        if not config:
            self._quote_name = None
            self.title.setText("选择一个合约查看详情")
            self.quote.setText("价差报价：等待两腿行情")
            self.legs.setText("")
            self.params.setText("")
            self.status.setText("")
            self.order_status.setText("")
            return
        self._quote_name = name
        title = config.display_name or config.exchange_spread or config.name
        self.title.setText(f"{title}  ·  {config.remark}" if config.remark else title)
        size_text = f"{config.size:g}" if config.size > 0 else "待柜台验证"
        tick_text = f"{config.pricetick:g}" if config.pricetick > 0 else "待柜台验证"
        self.legs.setText(
            f"{config.near_symbol} − {config.far_symbol}  ·  "
            f"乘数 {size_text}  ·  最小变动 {tick_text}  ·  原始 tick 自动落库"
        )
        self.params.setText(self._describe_config(config))
        spread = self.spread_engine.get_spread(name)
        if spread is not None:
            self._set_quote(spread.bid_price, spread.ask_price,
                            spread.bid_volume, spread.ask_volume)
        else:
            self._set_quote(0, 0)
        strategy = self.control._strategy(name)
        active = bool(name in self.control.enabled and self.control.master_running and strategy
                      and strategy.trading and strategy.live_trading and not strategy.paused)
        self.per_contract.setText("暂停此合约交易" if name in self.control.enabled else "允许此合约交易")
        error = self.control.errors.get(name, "")
        state = "交易中" if active else "策略暂停 · 不新发单、不撤已有委托"
        if config.contract_status == "verified":
            state += " · 合约已验证"
        elif config.contract_status == "pending_verification":
            state += " · 等待柜台合约验证"
        elif config.contract_status == "combination_unavailable":
            state += " · 组合合约未挂牌"
        state += " · 柜台核对：" + (
            "已确认" if self.control.is_reconciled(name) else "未确认"
        )
        self.status.setText(state + (" · " + error if error else ""))
        self._update_order_status()

    def closeEvent(self, event):
        # 面板被关闭也不得留下不可见的策略继续发单；暂停不会撤既有委托。
        self.control.stop_all(reason="关闭价差管理窗口")
        super().closeEvent(event)
