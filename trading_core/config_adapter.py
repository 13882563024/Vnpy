"""从价差注册表生成不可变的影子参数；不读取 CTP 凭据或允许实盘。"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from .shadow_strategy import ShadowConfig


@dataclass(frozen=True)
class Profile:
    config: ShadowConfig
    close_only: bool
    exit_preplace: bool
    exchange: str


def profile(path: Path, name: str) -> Profile:
    item=json.loads(Path(path).read_text(encoding='utf-8'))[name]
    if item['executor']!='exchange_spread' or item['price_formula'].replace(' ','')!='near-far':
        raise ValueError('仅支持近减远的原生组合')
    legs=item['legs']; exchange=item['exchange']
    if len(legs)!=2 or any(not leg.endswith('.'+exchange) for leg in legs):
        raise ValueError('组合交易所或腿定义不匹配')
    near,far=(leg.rsplit('.',1)[0] for leg in legs)
    s=item['strategy']; layers=s['max_positions']; size=s['layer_volume']
    for x in (layers,size,s['min_depth']):
        if type(x) not in (int,float) or not float(x).is_integer() or x<=0: raise ValueError('层数/手数/深度必须为正整数')
    for k in ('close_only','exit_preplace','allow_stop_loss'):
        if type(s[k]) is not bool: raise ValueError(f'{k} 必须显式为布尔值')
    version=sha256(json.dumps(item,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    config=ShadowConfig(version,near,far,item['exchange_spread'],item['pricetick'],item['size'],
        mean_seconds=60*s['mean_window_minutes'], sd_seconds=60*s['sd_window_minutes'],
        min_samples=s.get('warmup_min_samples',30), min_span=60*s.get('warmup_min_minutes',5),
        entry_k=s['entry_k'],layer_step=s['layer_step'],max_layers=int(layers),max_lots=int(size*layers),
        layer_lots=int(size),width_ticks=s['width_ticks'],min_depth=int(s['min_depth']),sync_seconds=s['sync_seconds'],
        volume_ratio_max=s['vr_max'],roll_cutoff=s['roll_entry_cutoff_days'],min_exit_pnl_override=s['min_exit_pnl_override'],
        fee_buffer=s.get('fee_buffer',1),roll_exit_days=s['roll_exit_days'],stop_loss=item.get('stop_loss',s['stop_loss']),
        allow_stop_loss=s['allow_stop_loss'],max_hold_days=s['max_hold_days'])
    return Profile(config,s['close_only'],s['exit_preplace'],exchange)
