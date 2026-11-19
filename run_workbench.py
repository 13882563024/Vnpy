"""Launch the standalone offline spread workbench, never the trading application."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from workbench import Sources, load_snapshot
from workbench.model import DEFAULT_CONFIG


def main(argv=None):
    parser = argparse.ArgumentParser(description='价差核对工作台（离线、只读）。仅接受已关闭的流水快照。')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='价差配置 JSON')
    parser.add_argument('--facts', type=Path, help='已有事实流水快照 SQLite')
    parser.add_argument('--shadow', type=Path, help='已有影子流水快照 SQLite（与事实分开）')
    parser.add_argument('--credentials', type=Path, help='只检查指定凭证文件是否存在；不读取内容')
    parser.add_argument('--account', help='完整账本账户键，严格匹配')
    parser.add_argument('--day', help='交易日，严格匹配；不会自动补入以前交易日')
    parser.add_argument('--follow', type=Path, metavar='纸面目录', help='每秒跟随纸面运行发布的关闭副本；不打开活动库')
    args = parser.parse_args(argv)
    directory = args.follow
    del args.follow
    if directory and any((args.facts, args.shadow, args.credentials)):
        parser.error('--follow 不可与 --facts、--shadow 或 --credentials 同用')
    sources = Sources(**vars(args))
    from PySide6.QtWidgets import QApplication
    from workbench.window import Window
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if directory:
        from workbench.follow import PaperFollow
        follow = PaperFollow(directory, sources)
        window = Window(follow.snapshot, follow=follow)
    else:
        window = Window(load_snapshot(sources))
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
