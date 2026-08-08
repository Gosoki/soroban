"""离线自救的命令行入口（源码运行用）。实现在 `app/rescue.py`——打包版也要用同一份。

用法（在 backend/ 下）：
    .venv/bin/python -m tools.use_local_db          # 交互确认后切回本地
    .venv/bin/python -m tools.use_local_db --yes    # 跳过确认（脚本里用）

打包版没有 backend/ 也没有 .venv，走的是 `soroban.exe --use-local-db`（见 run.py）。
"""
from __future__ import annotations

import sys

from app.rescue import use_local_db


def main() -> int:
    return use_local_db(assume_yes="--yes" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
