"""展示策略参数变更，或导出新草案；绝不修改源配置。"""
import argparse
import json
from pathlib import Path
from trading_core.config_draft import preview, export


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1]/'config'/'spreads.json')
    p.add_argument('--spread',required=True); p.add_argument('--set',action='append',required=True,metavar='KEY=JSON')
    p.add_argument('--export',type=Path,help='显式指定新文件，缺省只显示预览')
    args=p.parse_args(argv); updates={}
    for item in args.set:
        k,sep,value=item.partition('=')
        if not sep or k in updates: p.error('参数须用不重复的 KEY=JSON')
        try: updates[k]=json.loads(value)
        except ValueError: p.error('参数值须为 JSON 数值或 true/false')
    draft=preview(args.source,args.spread,updates)
    print(json.dumps(dict(spread=draft.spread,base=draft.before_hash,draft=draft.after_hash,changes=draft.changes),ensure_ascii=False,indent=2))
    if args.export: export(args.source,args.export,draft)
    return 0


if __name__=='__main__': raise SystemExit(main())
