"""纸面终端验收：使用真实核验流程与显式纸面成交，不连接行情或交易服务器。"""
import argparse
import json
from pathlib import Path
from trading_core.paper_run import run_paper
from trading_core.replay_runner import json_lines


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    root=Path(__file__).resolve().parent
    p.add_argument('--config',type=Path,default=root/'config/spreads.json');p.add_argument('--spread',default='A_2701_2703')
    p.add_argument('--events',type=Path,default=root/'examples/paper_bean.jsonl');p.add_argument('--output',type=Path,required=True,help='必须是尚不存在的输出目录')
    p.add_argument('--available',type=float,default=100000,help='合成纸面可用资金，非实际账户余额')
    p.add_argument('--pace',type=float,default=0,help='每个纸面事件之间的演示间隔（秒，缺省不等待）')
    p.add_argument('--fee-table',type=Path,help='显式使用费率表估算纸面费用；不作为结算实收')
    args=p.parse_args(argv)
    if not 0<=args.pace<=60: p.error('--pace 必须在 0 至 60 秒之间')
    cfg=json.loads(args.config.read_text(encoding='utf8'))[args.spread]
    def events():
        from time import sleep
        for n,e in enumerate(json_lines(args.events)):
            if n and args.pace: sleep(args.pace)
            yield e
    schedule=None
    if args.fee_table:
        from trading_core.fee_view import fee_schedule_from_table
        near,far=(leg.rsplit('.',1)[0] for leg in cfg['legs'])
        schedule=fee_schedule_from_table(near,far,cfg['exchange'],table_path=args.fee_table)
    r=run_paper(args.config,args.spread,events(),args.output,available=args.available,
                margin_per_lot=cfg['margin_per_lot'],fee_schedule=schedule)
    print(json.dumps(r,ensure_ascii=False,indent=2));return 0


if __name__=='__main__': raise SystemExit(main())
