"""Qt shell for an already loaded snapshot. Viewing is read-only; the draft editor can explicitly export a new file."""
from __future__ import annotations

from html import escape
import json
import os
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPushButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from .model import PURPOSE, UNKNOWN, Snapshot, load_snapshot, number, scoped_decisions, tier_returns
from trading_core.journal import Outcome


def text(v):
    if v is None or v == '':
        return UNKNOWN
    if isinstance(v, bool):
        return '是' if v else '否'
    return str(v)


def h(v):
    return escape(text(v)).replace('\n', '<br>')


def table(headers, rows):
    return ('<table width="100%" cellspacing="0" cellpadding="8"><tr>' + ''.join(
        f'<td bgcolor="#e8eef0"><b>{h(x)}</b></td>' for x in headers) + '</tr>' + ''.join(
        '<tr>' + ''.join(f'<td bgcolor="{("#f4f7f7" if i%2 else "#ffffff")}">{h(x)}</td>' for x in row) + '</tr>'
        for i,row in enumerate(rows)) + '</table>')


def note(s):
    return f'<p style="color:#465e68">{h(s)}</p>'


def heading(s):
    return f'<h3>{h(s)}</h3>'


def _safe(obj):
    if isinstance(obj, dict):
        return {k: ('[已隐藏]' if any(x in k.lower() for x in ('pass','secret','token','auth','密码','密钥')) else _safe(v)) for k,v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    return obj


def reason(row):
    r = row['record']; p = r.payload
    info = p.get('reason')
    why = json.dumps(_safe(info), ensure_ascii=False, indent=2) if isinstance(info, dict) and info else UNKNOWN
    return (heading(f"{text(p.get('tier_id'))}　{PURPOSE.get(str(p.get('purpose')), UNKNOWN)}")
            + note(f"{row['feed']}　·　{r.account}　·　交易日 {r.trading_day}　·　{' / '.join('#'+str(n) for n in row['refs'])}")
            + table(('依据', '记录值'), [('状态',row['state']),('策略 / 决策号',' / '.join(r.identity)), ('配置版本',p.get('config_version')),
                        ('方向 / 申请手数 / 限价',' / '.join(text(p.get(k)) for k in ('side','lots','price'))),
                        ('已配对手数 / 组合均价',f"{text(row['paired'])} / {text(row['avg'])}"),
                        ('近腿 / 远腿成交均价',f"{text(row['near_avg'])} / {text(row['far_avg'])}"),
                        ('未配对近腿 / 远腿手数',f"{text(row['residual_near'])} / {text(row['residual_far'])}")])
            + (note('；'.join(row['issues'])) if row['issues'] else '')
            + heading('决策时记录的原因') + f'<p>{h(why)}</p>')


STYLE = '''
QWidget {font-family:"Microsoft YaHei UI","Noto Sans CJK SC",sans-serif;font-size:14px;color:#162f3a;}
QMainWindow, QWidget#body {background:#f7f9f9;}
QWidget#rail {background:#163b49;} QWidget#rail QLabel {color:#f4fafb;} QLabel#railTitle {font-size:22px;font-weight:600;}
QWidget#rail QLabel#railNote {color:#c0d6da;font-size:13px;}
QLineEdit {background:#f1f8f8;border:1px solid #aac2c4;border-radius:4px;padding:8px;}
QListWidget#spreads {background:#163b49;color:white;border:none;outline:0;} QListWidget#spreads::item {padding:7px 8px;border-radius:3px;}
QListWidget#spreads::item:hover {background:#265361;} QListWidget#spreads::item:selected {background:#c4e9e5;color:#143944;}
QListWidget:focus,QPushButton:focus,QLineEdit:focus,QTextBrowser:focus {border:2px solid #268688;}
QLabel#draftTitle {font-size:21px;font-weight:600;} QTableWidget QLineEdit {padding:2px 5px;} QTableWidget {background:white;gridline-color:#dbe3e5;}
QLabel#title {font-size:29px;font-weight:600;} QLabel#sub,QLabel#scope {color:#47616b;} QLabel#scope {font-size:12px;}
QTabWidget::pane {border:1px solid #ccd9dc;background:white;} QTabBar::tab {background:#edf2f3;color:#415d68;padding:10px 14px;border-bottom:2px solid transparent;}
QTabBar::tab:selected {background:white;color:#125c60;border-bottom:2px solid #187276;font-weight:600;} QTextBrowser {background:white;border:none;selection-background-color:#c2e6e3;}
QPushButton {background:white;border:1px solid #adc2c6;border-radius:4px;padding:5px 10px;} QPushButton:hover {background:#e3f2f0;} QPushButton:disabled {color:#788b91;}
'''


class Window(QMainWindow):
    """Construct with a Snapshot; refresh only reopens the same explicit sources."""
    def __init__(self, snapshot: Snapshot, *, follow=None):
        super().__init__(); self.snap = snapshot; self.page = 0; self.rows = []
        self.follow = follow
        # The Windows offscreen plugin lacks the native Windows font database. Register
        # installed fonts in process memory; don't ship or copy licensed system fonts.
        if os.name == 'nt' and QGuiApplication.platformName() == 'offscreen' and 'Microsoft YaHei UI' not in QFontDatabase.families():
            font_dir = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
            for filename in ('msyh.ttc', 'msyhbd.ttc'):
                if (font_dir / filename).is_file():
                    QFontDatabase.addApplicationFont(str(font_dir / filename))
        self.setWindowTitle('价差核对工作台 · 只读'); self.resize(1100, 730); self.setMinimumSize(720, 480)
        self.setStyleSheet(STYLE)
        body = QWidget(); body.setObjectName('body'); self.setCentralWidget(body)
        layout = QHBoxLayout(body); layout.setSpacing(0); layout.setContentsMargins(0,0,0,0)
        rail = QWidget(); rail.setObjectName('rail'); rail.setFixedWidth(224); layout.addWidget(rail)
        left = QVBoxLayout(rail); left.setContentsMargins(16,24,16,16); left.setSpacing(12)
        brand = QLabel('价差核对工作台'); brand.setObjectName('railTitle'); left.addWidget(brand)
        tagline = QLabel('纸面跟随   /   只读' if follow else '离线快照   /   只读'); tagline.setObjectName('railNote'); left.addWidget(tagline); left.addSpacing(10)
        self.search = QLineEdit(); self.search.setPlaceholderText('搜索品种、价差或合约'); self.search.setClearButtonEnabled(True); self.search.setAccessibleName('搜索价差'); left.addWidget(self.search)
        self.list = QListWidget(); self.list.setObjectName('spreads'); self.list.setAccessibleName('配置价差目录'); left.addWidget(self.list,1)
        self.count = QLabel(); self.count.setObjectName('railNote'); left.addWidget(self.count)
        main = QVBoxLayout(); main.setContentsMargins(24,22,24,16); main.setSpacing(10); layout.addLayout(main,1)
        top = QHBoxLayout(); main.addLayout(top); self.title = QLabel(); self.title.setTextFormat(Qt.TextFormat.PlainText); self.title.setObjectName('title'); top.addWidget(self.title,1)
        refresh = QPushButton('重新读取'); refresh.clicked.connect(self.refresh); top.addWidget(refresh)
        self.sub = QLabel(); self.sub.setObjectName('sub'); self.sub.setTextFormat(Qt.TextFormat.PlainText); self.sub.setWordWrap(True); main.addWidget(self.sub); main.addSpacing(6)
        self.tabs = QTabWidget(); self.tabs.setUsesScrollButtons(True); main.addWidget(self.tabs,1); self.pages=[]
        for name in ('价差配置','层级与委托','收益与资金','日志与来源'):
            panel=QWidget(); v=QVBoxLayout(panel); v.setContentsMargins(8,8,8,8)
            view=QTextBrowser(); view.setOpenLinks(False); view.setOpenExternalLinks(False); view.setAccessibleName(name); view.document().setDocumentMargin(10); v.addWidget(view,1); self.pages.append(view)
            if name=='价差配置':
                bar=QHBoxLayout(); bar.addStretch(); self.draft_button=QPushButton('编辑参数草案…'); bar.addWidget(self.draft_button); v.addLayout(bar)
                self.draft_button.clicked.connect(self.open_draft)
            if name=='日志与来源':
                bar=QHBoxLayout(); bar.addStretch(); self.prev=QPushButton('上一页'); self.next=QPushButton('下一页'); bar.addWidget(self.prev); bar.addWidget(self.next); v.addLayout(bar)
                self.prev.clicked.connect(lambda: self.logs(-1)); self.next.clicked.connect(lambda: self.logs(1))
            self.tabs.addTab(panel,name)
        self.scope = QLabel(); self.scope.setObjectName('scope'); self.scope.setTextFormat(Qt.TextFormat.PlainText); self.scope.setWordWrap(True); main.addWidget(self.scope)
        self.search.textChanged.connect(self.filter); self.list.currentItemChanged.connect(self.selection)
        self.filter()
        if follow:
            self.draft_button.hide()
            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.refresh)
            self.refresh()
            self.timer.start()

    def closeEvent(self, event):
        if self.follow:
            self.timer.stop()
        super().closeEvent(event)

    def open_draft(self):
        from .draft_dialog import DraftDialog
        key = self.key()
        if key and self.draft_button.isEnabled():
            dialog = DraftDialog(self.snap, key, self)
            try:
                dialog.exec()
            finally:
                dialog.deleteLater()

    def refresh(self):
        if self.follow:
            if self.follow.poll():
                self.snap = self.follow.snapshot
                self.selection()
            return
        self.snap=load_snapshot(self.snap.sources); self.page=0; self.filter()

    def filter(self, *_):
        prev=self.key(); needle=self.search.text().strip().casefold(); self.list.blockSignals(True); self.list.clear(); idx=0
        for k,c in self.snap.spreads.items():
            if needle not in f"{k} {c.get('remark','')} {c.get('display_name','')} {c.get('legs','')}".casefold(): continue
            i=QListWidgetItem(f"{text(c.get('remark') or c.get('product') or k)}\n{k}"); i.setData(Qt.ItemDataRole.UserRole,k); self.list.addItem(i)
            if k==prev: idx=self.list.count()-1
        self.list.setCurrentRow(idx); self.list.blockSignals(False); self.count.setText(f'显示 {self.list.count()} / {len(self.snap.spreads)} 个价差'); self.selection()

    def key(self):
        i=self.list.currentItem(); return i.data(Qt.ItemDataRole.UserRole) if i else None

    def selection(self,*_):
        key=self.key(); cfg=self.snap.spreads.get(key,{}); self.rows=scoped_decisions(self.snap,key) if key else []
        self.title.setText(text(cfg.get('remark') or cfg.get('product') or key) + '跨期价差' if key else '未找到价差'); self.sub.setText(f"{key}   /   {text(cfg.get('display_name'))}" if key else '请调整搜索条件或检查配置文件。')
        if self.follow:
            self.sub.setText(self.sub.text() + '　/　' + self.follow.status)
        self.scope.setText(f"{self.snap.config_status}    ·    "+'    ·    '.join(f'{f.name}：{f.status}' for f in self.snap.feeds))
        strat=cfg.get('strategy'); strat=strat if isinstance(strat,dict) else {}; legs=cfg.get('legs'); legs=legs if isinstance(legs,list) else []
        from trading_core.config_draft import EDITABLE
        self.draft_button.setEnabled(not self.follow and any(type(strat.get(k)) in (bool, int, float) for k in EDITABLE))
        pairs=[('近腿',legs[0] if legs else None),('远腿',legs[1] if len(legs)>1 else None),('计算公式',cfg.get('price_formula')),('乘数 / 最小变动价位',f"{text(cfg.get('size'))} / {text(cfg.get('pricetick'))}"),('配置层数上限 / 每层手数',f"{text(strat.get('max_positions'))} / {text(strat.get('layer_volume'))}"),('仅平仓配置',strat.get('close_only')),('每手保证金（配置值）',cfg.get('margin_per_lot'))]
        self.pages[0].setHtml((heading('合约与层级参数')+table(('配置项','当前文件值'),pairs) if key else heading('无匹配配置'))+note(f'配置来源：{self.snap.sources.config}')+heading('证据边界')+note('这些数值描述当前配置，不代表历史决策参数、实际持仓或可用资金。工作台不连接柜台，不执行委托。'))
        layers=''.join(reason(x) for x in self.rows) or heading('暂无可归属的委托意图')+note('当前筛选内没有与该组合代码匹配的意图。持仓、委托和成交均未知，不能据此判断空仓。')
        raw=[]
        symbols={x for x in (cfg.get('display_name'),cfg.get('exchange_spread'),key) if isinstance(x,str)}
        for feed in self.snap.feeds:
            for r in reversed(feed.events):
                p=r.payload
                if r.kind not in ('order','order_reject') or not isinstance(p.get('symbol'),str) or p['symbol'] not in symbols: continue
                state=('拒单 '+text(p.get('error_id')) if r.kind=='order_reject' else '柜台状态 '+text(r.identity[3] if len(r.identity)>3 else None))
                raw.append((f'{feed.name} #{r.sequence}\n{r.account} · {r.trading_day}', '/'.join(r.identity[:3]), state+' / '+r.outcome.value, p.get('price'),p.get('traded')))
        if raw:
            layers+=heading('组合代码匹配的柜台委托')+note('原始回报按代码列示；同一委托可能有多个快照。仅凭代码不能确认策略或原因。')+table(('来源','本地委托标识','状态 / 记录结果','限价','累计成交'),raw)
        self.pages[1].setHtml(layers); self.money(); self.logs(0)

    def money(self):
        from .model import strategy_returns
        out=heading('策略已实现收益'); summaries=strategy_returns(self.rows)
        for summary in summaries:
            feed,acc,day,strat=summary['scope']; paper=summary['paper']
            out+=heading(f'{strat} · {day}')+note(f'{feed} · {acc}')
            items=[(r['tier'],r['remaining'],r['gross'],r['fee'],r['net'],', '.join(f'#{n}' for n in r['refs'])) for r in summary['tiers']]
            items.append(('策略合计','—',summary['gross'],summary['fee'],summary['net'],'—'))
            out+=table(('层级','剩余手数','已实现毛收益','模拟手续费' if paper else '实收手续费','模拟净收益' if paper else '结算净收益','流水'),items)
            if summary['sources']: out+=note('费用来源：'+'；'.join(summary['sources']))
            if summary['budgets']:
                out+=table(('止盈层级','委托流水','下单时预计费用（不入账）','估算来源'),summary['budgets'])
        if not summaries: out+=note('没有可归属的策略成交记录，收益未知。')
        out+=note('手续费仅列已平手数分摊额。纸面费用按模拟成交和费率计算，不是实收。止盈委托的预计费用不入账。缺少逐笔费用凭据时净收益未知；无可退出报价时浮动收益未知。')+heading('账户资金快照')
        accounts=[]
        for f in self.snap.feeds:
            conflicts={(r.account,r.trading_day,r.identity) for r in f.events if r.kind=='account_snapshot' and r.outcome==Outcome.CONFLICT}
            for r in reversed(f.events):
                if r.kind!='account_snapshot' or r.outcome!=Outcome.NEW: continue
                problem=(r.account,r.trading_day,r.identity) in conflicts; p=r.payload
                detail = p.get('accounts'); detail = detail if isinstance(detail,list) and detail else [p]
                for a in detail:
                    a = a if isinstance(a,dict) else {}
                    accounts.append((f'{r.account}\n{r.trading_day}',None if problem else number(a.get('available')),None if problem else number(a.get('balance')),a.get('currency'),f'{f.name} #{r.sequence}\n'+('冲突，需核对' if problem else '历史快照')+'\n'+text(p.get('observed_at'))))
        out+=table(('账户 / 交易日','历史可用','历史权益','币种','来源'),accounts) if accounts else note('可用资金、权益均未知：所选流水中没有 account_snapshot。')
        self.pages[2].setHtml(out+note('资金属于账户，不按价差拆分。离线快照已失去实时性，不代表当前余额或报单许可。'))

    def logs(self,delta):
        ss=self.snap; items=[]; sources=[('价差配置',str(ss.sources.config),ss.config_status)]
        for f in ss.feeds:
            sources.append((f.name,str(f.path) if f.path else '未指定',f.status))
            for r in reversed(f.events):
                detail = r.kind
                if r.kind=='shadow_input': detail += ' / '+text(r.payload.get('kind'))
                if r.kind=='layer_event':
                    data = r.payload.get('data'); data = data if isinstance(data, dict) else {}
                    detail += ' / '+text(r.payload.get('type'))+' / '+r.identity[0]+'\n'+', '.join(f'{k}={text(data[k])}' for k in ('order_id','decision_id','direction','lots','filled') if k in data)
                if r.kind in ('order','order_reject','fill'): detail += ' / '+text(r.payload.get('symbol'))
                items.append((f'{f.name} #{r.sequence}',r.trading_day,r.account,detail,r.outcome.value))
        sources.append(('登录凭证',str(ss.sources.credentials) if ss.sources.credentials else '未指定',ss.credential_status))
        sources.append(('报单许可','交易进程的一次性凭证','不可核实 · 本工作台不签发、不消费'))
        size=50; last=max(0,(len(items)-1)//size); self.page=max(0,min(last,self.page+delta)); self.prev.setEnabled(self.page>0); self.next.setEnabled(self.page<last)
        out=heading('本次读取')+table(('来源','文件','状态'),sources)+heading('流水日志')+note(f"账户：{ss.sources.account or '全部'}　交易日：{ss.sources.day or '全部'}　按来源分列，各库序号倒序。第 {self.page+1} / {last+1} 页。")
        out+=table(('来源序号','交易日','账户','事件','结果'),items[self.page*size:(self.page+1)*size]) if items else note('无可显示的记录。未指定、读取失败或筛选为空，均不能证明未发生交易。')
        if self.follow:
            recent = [(gen, source + ' #' + str(r.sequence), r.kind, r.outcome.value, origin)
                      for gen, source, r, origin in reversed(self.follow.tail)]
            prefix = heading('纸面更新日志') + note(self.follow.status + '。按接收批次显示最近 100 条；各库序号独立，不代表跨库业务时序。')
            out = prefix + (table(('版本','来源序号','事件','结果','读取方式'), recent) if recent else note('尚未收到可核对的纸面事件。')) + out
        self.pages[3].setHtml(out)
