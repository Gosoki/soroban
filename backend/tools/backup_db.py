"""账本备份的命令行入口（源码运行用）。实现在 `app/backup.py`——打包版也要用同一份。

用法（在 backend/ 下）：
    .venv/bin/python -m tools.backup_db                    # 备份到 backend/backups/
    .venv/bin/python -m tools.backup_db --dir /mnt/nas     # 备份到别处（异地）
    .venv/bin/python -m tools.backup_db --keep 60          # 保留最近 60 份（默认 30）
    .venv/bin/python -m tools.backup_db --restore backups/soroban-20260819-120000.db

挂 cron（每天 03:00）：
    0 3 * * * cd /path/to/soroban/backend && .venv/bin/python -m tools.backup_db >> backups/backup.log 2>&1

**恢复一定要先演练一次再指望它。** 一个天天返回退出码 0、备的却是错东西的 cron，
比明摆着没有备份更危险——那正是原来那个 `backup.sh` 的失败形态
（它检测到 MySQL 就报错退出，而本机连 sqlite3 都没装，两种模式下都跑不起来）。
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.backup import make_backup, restore


def main() -> int:
    argv = sys.argv[1:]

    def opt(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    if "--restore" in argv:
        return restore(Path(opt("--restore")), assume_yes="--yes" in argv)

    out_dir = opt("--dir")
    keep = int(opt("--keep", "30"))
    make_backup(Path(out_dir) if out_dir else None, keep=keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
