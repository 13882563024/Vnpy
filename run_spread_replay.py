"""离线重放合约配置和历史行情/归属明确的成交，不连接交易接口。"""
import argparse
import json
from pathlib import Path
from trading_core.replay_runner import json_lines, run_events


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path(__file__).resolve().parent/'config'/'spreads.json')
    p.add_argument('--spread',required=True); p.add_argument('--account',required=True,help='回放用的账户键，不是凭据')
    p.add_argument('--events',required=True,type=Path,help='离线 JSONL 事件文件')
    p.add_argument('--output',required=True,type=Path,help='新的影子 SQLite 流水，不覆盖已有文件')
    args=p.parse_args(argv)
    if not args.events.is_file(): p.error('输入事件文件不存在')
    result=run_events(config_path=args.config,spread=args.spread, account=args.account,output=args.output, events=json_lines(args.events))
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


if __name__=='__main__':
    raise SystemExit(main())
