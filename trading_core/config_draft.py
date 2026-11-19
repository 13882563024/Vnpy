"""策略参数草案：明确基线、生成变更预览，仅导出新配置，绝不热改实盘文件。"""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import os
from .config_adapter import profile


# 不开放账户、柜台、交易权限、腿映射和凭据的隐式变更。
EDITABLE=frozenset({'entry_k','layer_step','max_positions','layer_volume','mean_window_minutes',
    'sd_window_minutes','min_exit_pnl_override','allow_stop_loss','max_hold_days','close_only','exit_preplace',
    'width_ticks','min_depth','sync_seconds','vr_max','roll_entry_cutoff_days','roll_exit_days'})


@dataclass(frozen=True)
class Draft:
    before_hash:str
    after_hash:str
    spread:str
    changes:tuple
    document:str


def preview(source:Path,spread:str,updates:dict)->Draft:
    raw=Path(source).read_bytes(); data=json.loads(raw.decode('utf8')); params=data[spread]['strategy']
    if not updates or not set(updates)<=EDITABLE: raise ValueError('只允许显式修改白名单内策略参数')
    changes=[]
    for k,v in sorted(updates.items()):
        old=params[k]
        if type(old) is bool and type(v) is not bool: raise ValueError(f'{k} 需要布尔值')
        if type(old) is not bool and type(v) not in (int,float): raise ValueError(f'{k} 需要数值')
        if v!=old:
            changes.append((k,old,v));params[k]=v
    if not changes: raise ValueError('没有参数变化')
    doc=json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    return Draft(sha256(raw).hexdigest(),sha256(doc.encode()).hexdigest(),spread,tuple(changes),doc)


def export(source:Path,target:Path,draft:Draft):
    """只发布经二次验证、与预览完全相同的文件。绝不替换源、已有草案或账户文件。"""
    source,target=Path(source),Path(target)
    checked=preview(source,draft.spread,{key:new for key,_,new in draft.changes})
    if checked!=draft: raise ValueError('配置基线或草案内容已变化，请重新预览')
    if target.exists(): raise FileExistsError(target)
    with NamedTemporaryFile(dir=target.parent,prefix='.draft-',delete=False) as f:
        tmp=Path(f.name)
    try:
        tmp.write_bytes(draft.document.encode())
        profile(tmp,draft.spread)
        if sha256(source.read_bytes()).hexdigest()!=draft.before_hash:
            raise ValueError('配置在验证期间变化')
        os.link(tmp,target)
    finally:
        tmp.unlink(missing_ok=True)

