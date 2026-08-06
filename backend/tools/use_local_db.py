"""离线自救：把「当前数据后端」改回本地 SQLite。

什么时候需要它：数据已切到 MySQL，而 MySQL 连不上（关机/换网/容器没起）。此时 soroban
**启动就会失败**——它刻意不自动降级，因为本地 SQLite 里留着的是切换那天的旧账本，
悄悄退回去等于让你对着陈旧数据继续记，MySQL 一回来就两边各有一半。
但服务起不来，网页上的「数据库」页也就点不到了，于是需要这条命令行出口。

用法（在 backend/ 下）：
    .venv/bin/python -m tools.use_local_db          # 交互确认后切回本地
    .venv/bin/python -m tools.use_local_db --yes    # 跳过确认（脚本里用）

只改「当前用哪个后端」这一个标记，**不动任何数据**：MySQL 上的账本原样留着，
之后 MySQL 恢复了，在「数据库」页点「切换」就能回去。
"""
from __future__ import annotations

import sys

from app.database import control_engine, current_backend, switch_to_local
from app.db import control


def main() -> int:
    cfg = control.read_config(control_engine())
    if cfg["backend"] != "mysql":
        print(f"当前后端已经是本地 SQLite（backend={cfg['backend']}），无需切换。")
        return 0

    # 只显示不含密码的标识，别把 DSN 原样打到终端/日志里
    where = "（连接串已加密，无法显示）"
    if cfg["mysql_url"]:
        from sqlalchemy.engine import make_url
        u = make_url(cfg["mysql_url"])
        where = f"{u.username}@{u.host}:{u.port}/{u.database}"
    print(f"当前后端：MySQL {where}")
    print("将把「当前后端」改回本地 SQLite。MySQL 上的数据原样保留，不会被删或被改。")
    print("注意：切回后你看到的是**本地那份**账本——它停在当初迁移到 MySQL 的那一刻。")

    if "--yes" not in sys.argv:
        try:
            if input("确认切回本地 SQLite？输入 yes 继续：").strip().lower() != "yes":
                print("已取消。")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 1

    switch_to_local()
    print(f"已切回本地 SQLite（当前后端：{current_backend()}）。现在可以正常启动 soroban 了。")
    print("MySQL 恢复后，在应用内「数据库」页点「切换」即可切回去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
