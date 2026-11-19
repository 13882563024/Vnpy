"""单运行、单线程纸面生产者：一个 manifest 切换同一事件边界的两份已关闭副本。"""
import json
import os
from pathlib import Path
import time
from tempfile import NamedTemporaryFile
from tools.export_fact_snapshot import export_snapshot


class PaperPublisher:
    def __init__(self,root:Path,account:str):
        self.root=root.resolve(strict=True); self.account=account; self.generation=0
        self.last=None; self.previous_files=[]
        if (self.root/'workbench.json').exists(): raise FileExistsError('该目录已有运行发布记录')

    def publish(self,day,state,*,changed=False):
        now=time.time()
        if state not in ('running','finished','failed'): raise ValueError('未知纸面运行状态')
        if state=='running' and self.last is not None and not changed and now-self.last<5: return
        n=self.generation+1
        copies=[]
        try:
            for kind in ('facts','strategy'):
                copies.append(export_snapshot(self.root/f'{kind}.sqlite',self.root/f'{kind}-{n}.sqlite'))
            with NamedTemporaryFile(dir=self.root,prefix='.manifest-',delete=False,mode='w',encoding='utf8') as f:
                temp=Path(f.name)
                try:
                    json.dump(dict(version=1,generation=n,state=state,published_at=now,account=self.account,day=day),f)
                    f.flush();os.fsync(f.fileno())
                except BaseException:
                    f.close();temp.unlink(missing_ok=True);raise
            try: os.replace(temp,self.root/'workbench.json')
            finally: temp.unlink(missing_ok=True)
        except Exception:
            for created in copies: created.unlink(missing_ok=True)
            raise
        self.last=now; self.generation=n; self.previous_files.append(copies)
        # 打开的上一代在 Windows 可能暂时不可删除；仅回收本实例创建的文件。
        remaining=[]
        for old in self.previous_files[:-2]:
            for path in old:
                try: path.unlink(missing_ok=True)
                except PermissionError: remaining.append([path])
        self.previous_files=remaining+self.previous_files[-2:]
