"""单日、专户、原生组合的无网络运行链路；独立输出事实/策略库。"""
from dataclasses import replace
from pathlib import Path
from vnpy.trader.constant import Exchange
from gateway.paper_spread import PaperGateway, PaperMainEngine
from .config_adapter import profile
from .paper_publish import PaperPublisher
from .ctp_facts import account_key
from .decision_view import describe_execution
from .flat_check import FirstEntryLimits
from .held_check import HeldLimits
from .journal import Journal
from .layers import Working, Terminal
from .paired_fills import matched_fills
from .runtime_bridge import RuntimeBridge, RuntimeValidation
from .shadow_strategy import ShadowStrategy, EntryChecks, LegQuote, FeeEstimate
from .trace import trace_decision


def run_paper(config,spread,events,output,*,available=100000,margin_per_lot=4500,fee_schedule=None,publish=True):
    p=profile(config,spread); c=p.config; root=Path(output); root.mkdir(exist_ok=False)
    strategy_db=Journal(root/'strategy.sqlite'); account=account_key('paper','demo')
    s=ShadowStrategy(strategy_db,c,account=account,strategy=spread); gateway=bridge=None
    publisher=PaperPublisher(root,account) if publish else None
    clock=[0.]; aliases={}; refs={}; pending=None; quote=None; responses=[]; day=None; ids=set()
    def revalidate(intent):
        cfg=profile(config,spread).config
        n,f,checks,at=quote
        fresh=all(0<=clock[0]-x.timestamp<=cfg.stale_seconds for x in (n,f))
        opening=intent.purpose in ('open','add')
        # 纸面运行只接受决定生成时盘口仍一致、且当前输入时刻不陈旧的情况。
        # 新报价必须先过策略；同方向也不能用旧阈值/旧价格发送。
        reason=intent.reason
        if opening:
            mid=(n.bid+n.ask-f.bid-f.ask)/2; mu,sd=s.window.mean(),s.window.sd()
            k=cfg.entry_k+cfg.layer_step*len(s.book.layers); z=(mid-mu)/sd if sd and mu is not None else None
            signal=(z is not None and (z < -k if intent.side=='buy' else z > k)
                    and cfg.layer_lots==intent.lots and 0<=n.ask-n.bid<=(cfg.width_ticks+.5)*cfg.tick
                    and 0<=f.ask-f.bid<=(cfg.width_ticks+.5)*cfg.tick
                    and min(n.bid_size,n.ask_size,f.bid_size,f.ask_size)>=cfg.min_depth
                    and abs(n.timestamp-f.timestamp)<=cfg.sync_seconds
                    and intent.price==(n.ask-f.bid if intent.side=='buy' else n.bid-f.ask)
                    and type(checks.days_to_delivery) is int and checks.days_to_delivery>cfg.roll_cutoff
                    and reason.get('rank')==len(s.book.layers)+1 and all(
                    x is True for x in (checks.can_open,checks.listed,checks.funds_ok,checks.quota_ok)))
        else:
            signal=any(l.tier_id==intent.tier_id and l.remaining>=intent.lots for l in s.book.layers)
        params=(cfg.max_lots,margin_per_lot,signal,fresh,checks.in_session)
        limits=FirstEntryLimits(*params) if intent.purpose=='open' and not refs else HeldLimits(c.near,c.far,p.exchange,*params)
        return RuntimeValidation(cfg.version,limits)
    def advance():
        nonlocal pending
        gateway.td_api.advance_order_check()
        result=bridge.poll()
        if pending and result.state!='checking':
            responses.append((pending.decision_id,result.state,result.reason))
            if result.state!='submitted': raise ValueError(f'纸面报单未获准：{result.reason}')
            ref=result.order_id.rsplit('_',1)[1]; refs[pending.decision_id]=ref
            s.on_status(day,'ack:'+ref,Working(pending.decision_id));pending=None
    def handle(result):
        nonlocal pending
        for cancel in result.cancels:
            order=cancel.order_id;ref=refs[order]; o=gateway.orders[ref]
            from vnpy.trader.object import CancelRequest
            req=CancelRequest(orderid=f'1_1_{ref}',symbol=c.symbol,exchange=Exchange(p.exchange))
            gateway.td_api.cancel_order(req)
            if o['OrderStatus']!='5': raise ValueError('撤单结果未确认')
            s.on_status(day,'cancel:'+ref,Terminal(order,o['VolumeTraded']))
        if result.intent:
            if pending: raise ValueError('已有待核验意图，停止场景')
            pending=result.intent
            b=bridge.begin(pending,exchange=Exchange(p.exchange))
            if b.state!='checking': raise ValueError(b.reason)
            return pending.decision_id
    try:
        for e in events:
            key=e['id']; kind=e['type']
            if key in ids: raise ValueError('重复场景输入标识')
            ids.add(key)
            if gateway is None:
                day=e['day']; gateway=PaperGateway(p,root/'facts.sqlite','demo',day,available,lambda:clock[0]); bridge=RuntimeBridge(
                    PaperMainEngine(gateway),gateway_name='PAPER',source_journal=strategy_db,account=account,
                    strategy=spread,mode='paper',revalidate=revalidate,clock=lambda:clock[0])
            if e['day']!=day or e['now']<clock[0]: raise ValueError('交易日变化或时钟回退')
            clock[0]=e['now']
            if kind=='quote':
                checks=EntryChecks(**e['checks']);checks=replace(checks,can_open=False) if p.close_only else checks
                n,f=LegQuote(**e['near']),LegQuote(**e['far']);quote=(n,f,checks,e['now'])
                aliases[key]=handle(s.on_quote(day,n,f,now=e['now'],checks=checks))
            elif kind=='protect':
                if not p.exit_preplace: raise ValueError('止盈未启用')
                aliases[key]=handle(s.protect(day,key,FeeEstimate(**e['fees'])))
            elif kind=='fill':
                order=aliases[e['decision_event']];ref=refs[order]
                gateway.fill(ref,e['lots'],e['near_price'],e['far_price'])
                i=s.intents[order]; tr=trace_decision(gateway.td_api.journal.replay(),account,day,spread,order)
                v=describe_execution(tr,near=c.near,far=c.far)
                if v['issues']: raise ValueError(str(v['issues']))
                if fee_schedule is not None:
                    from .fee_view import record_paper_fees
                    record_paper_fees(gateway.td_api.journal,tr,fee_schedule=fee_schedule,near=c.near,far=c.far,
                                      multiplier=c.multiplier,open_day=s.open_days.get(i.tier_id,day))
                actual=matched_fills(tr.fills,exchange=p.exchange,sys_id='PAPER-'+ref,near=c.near,far=c.far,
                                     combo=c.symbol,buy=i.side=='buy',max_lots=i.lots)
                handle(s.on_matched(day,key,order,actual))
                o=gateway.orders[ref]
                if o['OrderStatus']=='0': s.on_status(day,'done:'+ref,Terminal(order,o['VolumeTraded']))
            elif kind!='timer': raise ValueError('未知纸面事件')
            had_pending=pending is not None
            if pending: advance()
            if publisher:
                publisher.publish(day,'running',changed=kind=='fill' or (had_pending and pending is None))
        if pending: raise ValueError('场景结束但本轮核验尚未完成')
        if not gateway: raise ValueError('空场景')
        actual={r['InstrumentID']:r['Position']*(1 if r['PosiDirection']=='2' else -1)
                for r in gateway.positions() if r['Position']}
        expected=sum(l.direction*l.remaining for l in s.book.layers)
        if actual!={symbol:qty for symbol,qty in [(c.near,expected),(c.far,-expected)] if qty}:
            raise ValueError('场景结束时策略层账与纸面柜台双腿仓位不符')
        if publisher: publisher.publish(day,'finished')
        return dict(mode='offline_paper',account=account,orders=len(refs),trade_rows=len(gateway.trades),
                    active_orders=sum(o['OrderStatus'] in ('1','3') for o in gateway.orders.values()),
                    cancelled=sum(o['OrderStatus']=='5' for o in gateway.orders.values()),
                    broker_leg_positions=actual,remaining=[(l.tier_id,l.remaining) for l in s.book.layers],decisions=responses)
    except Exception:
        if bridge: bridge.close(); bridge=None
        if publisher and gateway: publisher.publish(day,'failed')
        raise
    finally:
        if bridge: bridge.close()
        if gateway: gateway.close()
