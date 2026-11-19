"""完整柜台回包→只读快照→首仓证据→模拟发送；不创建交易连接。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.object import OrderRequest
from vnpy_ctp.gateway.ctp_gateway import THOST_FTDC_PC_Combination, symbol_contract_map
from gateway.ctp_spread_gateway import SpreadCtpTdApi
from trading_core.ctp_facts import account_key
from trading_core.flat_check import FirstEntryLimits
from trading_core.intents import OrderIntent
from trading_core.journal import Journal
from trading_core.preflight import Preflight, GateDenied
from trading_core.query_round import QueryCollector


class GatewayRoundTests(TestCase):
    def setUp(self):
        tmp=TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        a=self.a=SpreadCtpTdApi(Mock(gateway_name='CTP'))
        self.addCleanup(a.close)
        a.journal=Journal(Path(tmp.name)/'facts.sqlite'); a.brokerid='b'; a.userid='a'
        self.enterContext(patch.dict(symbol_contract_map,clear=True))
        with patch.object(a,'reqSettlementInfoConfirm',return_value=0):
            a.onRspUserLogin(dict(TradingDay='20261119',FrontID=1,SessionID=2),{'ErrorID':0},1,True)
        self.sym='SP a2701&a2703'
        a.onRspQryInstrument(dict(ProductClass=THOST_FTDC_PC_Combination, InstrumentID=self.sym,
                ExchangeID='DCE', InstrumentName='combo', VolumeMultiple=0, PriceTick=1,
                MinLimitOrderVolume=1, MaxLimitOrderVolume=10),{},2,True)
        self.t=0.; a.preflight=Preflight(ttl_seconds=15,clock=lambda:self.t)
        a.query_collector=QueryCollector(clock=lambda:self.t)
        self.intent=OrderIntent(account_key('b','a'),'20261119','strategy','d1','L1','open',
                self.sym,'buy',1,-5.,'v1',{'z':-2})
        self.lim=FirstEntryLimits(1,300.,True,True,True)

    def collect(self,pos=0):
        a=self.a; a.begin_order_check(self.intent)
        common=dict(BrokerID='b',TradingDay='20261119')
        for i,(req,callback,data) in enumerate([
                ('Order','Order',{}),('Trade','Trade',{}),
                ('TradingAccount','TradingAccount',dict(common,AccountID='a',CurrencyID='CNY',Available=9000)),
                ('InvestorPosition','InvestorPosition',dict(common,InvestorID='a',InstrumentID='a2701',PosiDirection='2',Position=pos))]):
            self.t+=1
            with patch.object(a,'reqQry'+req,return_value=0) as send:
                a.advance_order_check(); send.assert_called_once()
                reqid=send.call_args.args[1]
                getattr(a,'onRspQry'+callback)(data,{},reqid,True)

    def test_first_entry_real_callback_chain(self):
        self.collect(); ev=self.a.finish_first_entry_check(self.intent,self.lim)
        req=OrderRequest(symbol=self.sym,exchange=Exchange.DCE,direction=Direction.LONG,
                         offset=Offset.OPEN,type=OrderType.LIMIT,volume=1,price=-5,reference='CrossPeriod_strategy_open')
        req.extra={'order_intent':self.intent,'preflight_evidence':ev}
        with patch.object(self.a,'reqOrderInsert',return_value=0) as send:
            self.assertTrue(self.a.send_order(req)); self.assertFalse(self.a.send_order(req)); send.assert_called_once()

    def test_complete_snapshot_retains_unknown_equity_for_ui(self):
        self.collect(pos=1)
        snapshot=self.a.take_order_snapshot()
        self.assertIsNotNone(snapshot)
        row=list(self.a.journal.replay())[-1]
        self.assertEqual((row.kind,row.payload['available'],row.payload['balance']),('account_snapshot',9000,None))
        self.assertEqual(row.payload['positions'][0]['lots'],1)
        from trading_core.snapshot_archive import load_batch
        restored=load_batch(self.a.journal,account=self.intent.account,day=self.intent.day,token=snapshot.round.token)
        self.assertEqual(restored,snapshot)
        self.assertFalse(self.a.take_order_snapshot())

    def test_held_add_full_gateway_round_keeps_top_tp(self):
        from dataclasses import replace
        from trading_core.held_check import HeldLimits
        from trading_core.send_binding import reserve_and_bind
        a=self.a; opening=self.intent
        tp=replace(opening,purpose='take_profit',side='sell',decision_id='tp',price=-3.)
        candidate=replace(opening,purpose='add',tier_id='L2',decision_id='add',price=-8.)
        common=dict(BrokerID='b',InvestorID='a',TradingDay='20261119',ExchangeID='DCE')
        orders=[]
        for i,intent in enumerate((opening,tp),1):
            reserve_and_bind(a.journal,intent,('8','9',str(i)),'DCE')
            orders.append(dict(common,InstrumentID=self.sym,FrontID=8,SessionID=9,OrderRef=str(i),
                OrderSysID=f'sys{i}',Direction=str(i-1),CombOffsetFlag=str(i-1),LimitPrice=intent.price,
                VolumeTotalOriginal=1,VolumeTraded=int(i==1),OrderStatus='0' if i==1 else '3',
                OrderSubmitStatus='3',OrderPriceType='2',TimeCondition='3',VolumeCondition='1'))
        positions=[]; trades=[]
        for i,s in enumerate(('a2701','a2703')):
            trades.append(dict(common,InstrumentID=s,OrderRef='1',OrderSysID='sys1',Volume=1,
                TradeID='exchange-fill',OffsetFlag='0',Direction=str(i),Price=100+5*i))
            positions.append(dict(common,InstrumentID=s,Position=1,TodayPosition=1,YdPosition=0,
                LongFrozen=i,ShortFrozen=1-i,PosiDirection=str(i+2),HedgeFlag='1'))
        a.begin_order_check(candidate)
        for suffix,rows in [('Order',orders),('Trade',trades),('TradingAccount',[dict(common,
                AccountID='a',CurrencyID='CNY',Available=9000)]),('InvestorPosition',positions)]:
            self.t+=1
            with patch.object(a,'reqQry'+suffix,return_value=0) as query:
                a.advance_order_check(); reqid=query.call_args.args[1]
                for n,row in enumerate(rows):
                    getattr(a,'onRspQry'+suffix)(row,{},reqid,n==len(rows)-1)
        limits=HeldLimits('a2701','a2703','DCE',3,300.,True,True,True)
        ev=a.finish_order_check(candidate,limits)
        req=OrderRequest(symbol=self.sym,exchange=Exchange.DCE,direction=Direction.LONG,offset=Offset.OPEN,
            type=OrderType.LIMIT,volume=1,price=-8,reference='CrossPeriod_strategy_add')
        req.extra={'order_intent':candidate,'preflight_evidence':ev}
        with patch.object(a,'reqOrderInsert',return_value=0) as send, patch.object(a,'reqOrderAction') as cancel:
            self.assertTrue(a.send_order(req)); send.assert_called_once(); cancel.assert_not_called()

    def test_single_leg_position_not_empty_account(self):
        self.collect(pos=1)
        with self.assertRaises(GateDenied): self.a.finish_first_entry_check(self.intent,self.lim)

    def test_state_changed_after_last_prevents_seal(self):
        self.collect(); self.a.preflight.invalidate()
        with self.assertRaises(RuntimeError): self.a.finish_first_entry_check(self.intent,self.lim)
