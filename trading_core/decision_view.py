"""工作台/CLI 共用的单决策只读视图：区分报单、组合实成、未配对、证据缺口。"""
from .paired_fills import matched_fills
from .trace import DecisionTrace


def describe_execution(trace: DecisionTrace, *, near: str, far: str) -> dict:
    p = trace.intent.payload
    view = {'tier_id':p['tier_id'], 'purpose':p['purpose'], 'reason':p['reason'],
            'limit_price':p['price'], 'requested_lots':p['lots'], 'state':trace.state,
            'paired_lots':None, 'avg_spread':None, 'near_avg':None,'far_avg':None,
            'unpaired_near':None,'unpaired_far':None,'issues':list(trace.issues)}
    if trace.state != 'broker_reported':
        return view
    keys = {(r.payload['exchange'],r.payload['order_sys_id']) for r in trace.orders
            if r.payload.get('exchange') and r.payload.get('order_sys_id')}
    if len(keys) != 1:
        view['issues'].append('缺少唯一交易所委托号，无法归属成交')
        view['state'] = 'needs_review'
        return view
    exchange,sysid = next(iter(keys))
    try:
        actual = matched_fills(trace.fills,exchange=exchange,sys_id=sysid,near=near,far=far,
                               combo=p['symbol'],buy=p['side']=='buy',max_lots=p['lots'])
    except (ValueError, ArithmeticError, KeyError) as exc:
        view['issues'].append(str(exc)); view['state']='needs_review'
        return view
    as_text = lambda v: None if v is None else str(v)
    view.update(paired_lots=actual.lots, avg_spread=as_text(actual.average_spread),
                near_avg=as_text(actual.near_average), far_avg=as_text(actual.far_average),
                unpaired_near=actual.residual_near,unpaired_far=actual.residual_far)
    if actual.residual_near or actual.residual_far:
        view['state']='unpaired_fills'
    cumulative = max((r.payload.get('traded',0) for r in trace.orders),default=0)
    if cumulative > actual.lots:
        view['issues'].append('柜台累计成交大于已匹配明细，等待补证，不可重发')
        view['state']='awaiting_fill_details'
    return view
