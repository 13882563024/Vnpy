"""按指定两次完整快照复核隔夜仓，只打印差异及处理建议，绝不交易或改账。"""
from argparse import ArgumentParser
from dataclasses import asdict
import json
from pathlib import Path
from trading_core.config_adapter import profile
from trading_core.journal import Journal
from trading_core.recovery import RecoveryScope, build_baseline, audit_rollover
from trading_core.snapshot_archive import load_batch


def main(argv=None):
    p=ArgumentParser(description=__doc__);p.add_argument('facts',type=Path)
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().parents[1]/'config'/'spreads.json')
    for key in ('account','spread','prior-day','current-day','prior-token','current-token','calendar-previous-day'):
        p.add_argument('--'+key,required=True)
    args=p.parse_args(argv); prof=profile(args.config,args.spread); c=prof.config
    journal=Journal.open_readonly(args.facts)
    scope=RecoveryScope(args.account,args.spread,c.version,prof.exchange,c.near,c.far)
    try:
        prior=load_batch(journal,account=args.account,day=args.prior_day,token=args.prior_token)
        current=load_batch(journal,account=args.account,day=args.current_day,token=args.current_token)
        base=build_baseline(scope,prior,journal)
        audit=audit_rollover(base,prior,current,journal,previous_trading_day=args.calendar_previous_day)
    except (ValueError,KeyError) as exc:
        print(json.dumps({'matched':False,'reason':str(exc),'trade_permission':False},ensure_ascii=False));return 2
    print(json.dumps({'matched':audit.matched,'trade_permission':False,'baseline':asdict(base),'audit':asdict(audit)},
                     ensure_ascii=False,indent=2,default=str));return 0 if audit.matched else 2


if __name__=='__main__': raise SystemExit(main())
