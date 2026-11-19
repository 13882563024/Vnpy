"""内存纸面柜台：复用真实门禁，不接 CTP 前置，不读取环境凭据，不自动撮合。

仅在独立回放进程使用。每笔成交由带归属的显式输入驱动；不得作为真实券商适配器。
"""
from collections import defaultdict
from types import SimpleNamespace
from vnpy.trader.constant import Product, Exchange
from vnpy_ctp.gateway.ctp_gateway import symbol_contract_map
from gateway.ctp_spread_gateway import SpreadCtpTdApi
from trading_core.journal import Journal
from trading_core.query_round import QueryCollector
from trading_core.preflight import Preflight


class _PaperTd(SpreadCtpTdApi):
    _paper_only=True
    def connect(self,*args,**kwargs): raise RuntimeError('纸面柜台不能连接网络')
    def reqQryOrder(self,payload,reqid): return self.gateway.answer('Order',list(self.gateway.orders.values()),reqid)
    def reqQryTrade(self,payload,reqid): return self.gateway.answer('Trade',self.gateway.trades,reqid)
    def reqQryTradingAccount(self,payload,reqid): return self.gateway.answer('TradingAccount',[self.gateway.cash],reqid)
    def reqQryInvestorPosition(self,payload,reqid): return self.gateway.answer('InvestorPosition',self.gateway.positions(),reqid)
    def reqOrderInsert(self,payload,reqid): return self.gateway.insert(payload)
    def reqOrderAction(self,payload,reqid): return self.gateway.cancel(payload)


class PaperGateway:
    runtime_fixture_mode='paper'
    gateway_name='PAPER'

    def __init__(self,profile,path,account,day,available,clock):
        self.config=profile.config; self.exchange=profile.exchange
        self.common={'BrokerID':'paper','InvestorID':account,'TradingDay':day,'ExchangeID':self.exchange}
        self.cash=dict(self.common, AccountID=account,CurrencyID='CNY',Available=available)
        self.orders={}; self.trades=[]; self.messages=[]; self._positions=defaultdict(int)
        from pathlib import Path
        Path(path).touch(exist_ok=False)  # 纸面柜台不得追加到真实事实库
        self._old={s:symbol_contract_map.get(s) for s in (self.config.near,self.config.far,self.config.symbol)}
        self.td_api=a=_PaperTd(self); a.journal=Journal(path); a.brokerid='paper';a.userid=account
        from trading_core.ctp_facts import account_key
        a.journal.append(account_key('paper',account),day,'paper_session',('PAPER',),{'mode':'paper','synthetic':True})
        a.preflight=Preflight(ttl_seconds=15,clock=clock);a.query_collector=QueryCollector(clock=clock)
        a.frontid=1;a.sessionid=1;a.login_status=True;a.contract_inited=True
        a.cabinet_trading_day=f'{day[:4]}-{day[4:6]}-{day[6:]}'
        a._login_scope=(a.brokerid,a.userid,a.cabinet_trading_day)
        for s in self._old:
            symbol_contract_map[s]=SimpleNamespace(exchange=Exchange(self.exchange),pricetick=self.config.tick,
                                 product=Product.SPREAD if s==self.config.symbol else Product.FUTURES)
        a._native_combos.add((self.config.symbol,self.exchange))

    def close(self):
        self.td_api.close()
        for s,old in self._old.items():
            if old is None: symbol_contract_map.pop(s,None)
            else: symbol_contract_map[s]=old

    def write_log(self,msg): self.messages.append(msg)
    def write_error(self,msg,error): self.messages.append(f'{msg}: {error}')
    def on_order(self,order): pass
    def on_trade(self,trade): pass

    def answer(self,suffix,rows,reqid):
        batch=rows or [{}]
        for i,row in enumerate(batch): getattr(self.td_api,'onRspQry'+suffix)(dict(row),{},reqid,i==len(batch)-1)
        return 0

    def insert(self,payload):
        ref=payload['OrderRef']
        if ref in self.orders: raise ValueError('纸面委托序号冲突')
        self.orders[ref]=dict(payload,**{**self.common,'OrderRef':ref,'OrderSysID':f'PAPER-{ref}',
                'FrontID':1,'SessionID':1,'OrderStatus':'3','OrderSubmitStatus':'3','VolumeTraded':0,
                'InsertDate':self.common['TradingDay'],'InsertTime':'09:00:00','StatusMsg':''})
        self.td_api.onRtnOrder(dict(self.orders[ref]));return 0

    def cancel(self,payload):
        o=self.orders[payload['OrderRef']]
        if o['OrderStatus'] in ('3','1'):
            o['OrderStatus']='5';self.td_api.onRtnOrder(dict(o))
        return 0

    def fill(self,reference,quantity,near_price,far_price):
        """显式配对成交；未有对应活动委托或超量则拒绝。"""
        from math import isfinite
        o=self.orders[reference];side=o['Direction'];opening=o['CombOffsetFlag'][0]=='0'
        if (type(quantity) is not int or quantity<=0 or not all(type(x) in (int,float) and isfinite(x) and x>0 for x in (near_price,far_price))
                or o['OrderStatus'] not in ('1','3') or quantity+o['VolumeTraded']>o['VolumeTotalOriginal']):
            raise ValueError('无效的纸面成交或已终结委托')
        spread=near_price-far_price
        if (side=='0' and spread>o['LimitPrice']) or (side=='1' and spread<o['LimitPrice']):
            raise ValueError('纸面成交价违反委托限价')
        legs=[(self.config.near,near_price,side),(self.config.far,far_price,'1' if side=='0' else '0')]
        keys=[(s,'2' if (d=='0')==opening else '3') for s,_,d in legs]
        if not opening and any(self._positions[k]<quantity for k in keys): raise ValueError('纸面可平仓不足')
        for (s,px,d),k in zip(legs,keys):
            self._positions[k]+=quantity if opening else -quantity
            t=dict(self.common,InstrumentID=s,TradeID=f'{reference}/{o["VolumeTraded"]}',OrderRef=reference,
                   OrderSysID=o['OrderSysID'],Direction=d,OffsetFlag=o['CombOffsetFlag'][0],Volume=quantity,Price=px,
                   TradeDate=self.common['TradingDay'],TradeTime='09:00:00')
            self.trades.append(t);self.td_api.onRtnTrade(t)
        o['VolumeTraded']+=quantity;o['OrderStatus']='0' if o['VolumeTraded']==o['VolumeTotalOriginal'] else '1'
        self.td_api.onRtnOrder(dict(o))

    def positions(self):
        rows=[]
        for (symbol,d),qty in self._positions.items():
            frozen=0
            for o in self.orders.values():
                if o['OrderStatus'] not in ('1','3') or o['CombOffsetFlag'][0]=='0': continue
                buy=(o['Direction']=='0') == (symbol==self.config.near)
                if buy==(d=='3'): frozen+=o['VolumeTotalOriginal']-o['VolumeTraded']
            rows.append(dict(self.common,InstrumentID=symbol,PosiDirection=d,Position=qty,TodayPosition=qty,
                      YdPosition=0,HedgeFlag='1',LongFrozen=frozen if d=='3' else 0,ShortFrozen=frozen if d=='2' else 0))
        return rows


from vnpy_riskmanager.engine import RiskEngine


class _PaperRisk(RiskEngine):
    """沿用 RiskEngine.send_order/check_allowed；不加载用户规则、文件或连接。"""
    def __init__(self, engine, rules):
        self.main_engine=engine
        self.rules={str(n):rule for n,rule in enumerate(rules)}
        self.patch_functions()


class PaperMainEngine:
    """独立无网络路由。刻意不支持注册网关、connect 或隐式 fallback。"""
    def __init__(self,gateway,*,rules=()):
        if type(gateway) is not PaperGateway: raise TypeError('仅接受本地纸面柜台')
        self.gateway=gateway
        limit=SimpleNamespace(active=True,check_allowed=lambda req,name: name=='PAPER' and
                              0<req.volume<=gateway.config.max_lots and req.symbol==gateway.config.symbol)
        self.risk=_PaperRisk(self,[limit,*rules])

    def get_gateway(self,name): return self.gateway if name=='PAPER' else None

    def send_order(self,req,gateway_name):
        if gateway_name!='PAPER': raise ValueError('纸面模式不能路由其它柜台')
        return self.gateway.td_api.send_order(req)
