"""从运行中的事实流水制作只读工作台快照；不复制凭据、不连接交易网关。"""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile


def export_snapshot(source: Path, target: Path) -> Path:
    """用 SQLite backup 纳入 WAL 已提交页，再原子发布；不覆盖现有文件。"""
    source=Path(source).resolve(strict=True)
    target=Path(target).absolute()
    if target.exists():
        raise FileExistsError(target)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as reader:
        reader.execute('PRAGMA trusted_schema=OFF')
        if reader.execute("SELECT type FROM sqlite_schema WHERE name='facts'").fetchone()!=('table',):
            raise ValueError('来源不是事实库')
        # 输出同目录暂存确保同卷。发布前不暴露半份快照；硬链接不会替换已有目标。
        with NamedTemporaryFile(dir=target.parent,prefix='.facts-export-',delete=False) as f:
            temp=Path(f.name)
        try:
            with closing(sqlite3.connect(temp)) as dest:
                reader.backup(dest)
                dest.execute('PRAGMA journal_mode=DELETE')
                if dest.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                    raise ValueError('导出快照完整性校验失败')
            os.link(temp,target)
        finally:
            temp.unlink(missing_ok=True)
    return target


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path); parser.add_argument('target',type=Path)
    args=parser.parse_args(); print(export_snapshot(args.source,args.target))
