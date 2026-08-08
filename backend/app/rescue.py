"""离线自救：把「当前数据后端」改回本地 SQLite。

放在 `app/` 而不是 `tools/` 是有原因的：`app.*` 天然在 PyInstaller 的导入图里
（run.py → app.main），打包版拿得到；而 `tools/` 既不在导入图里、也没被 spec 的 datas 收，
exe 里根本不存在。想靠 `hiddenimports=["tools.use_local_db"]` 把它拽进包，正是 soroban.spec
头部注释警告过的漂移陷阱——改路径/改名后静默失效，而用户只会在最需要它的时候发现。

`tools/use_local_db.py` 现在是本模块的薄壳，两个入口共用同一份实现。
"""
from __future__ import annotations

import sys
from typing import Optional


def use_local_db(assume_yes: bool = False, stream=None) -> int:
    """把当前后端改回本地 SQLite。返回进程退出码（0=已切换或本就是本地，1=用户取消）。

    只改「当前用哪个后端」这一个标记，**不动任何数据**：MySQL 上的账本原样留着。
    """
    from .database import control_engine, current_backend, switch_to_local
    from .db import control

    out = stream or sys.stdout

    def say(msg: str) -> None:
        print(msg, file=out)

    cfg = control.read_config(control_engine())
    if cfg["backend"] != "mysql":
        say(f"当前后端已经是本地 SQLite（backend={cfg['backend']}），无需切换。")
        return 0

    # 只显示不含密码的标识，别把 DSN 原样打到终端/日志里
    where = "（连接串已加密，无法显示）"
    if cfg["mysql_url"]:
        from sqlalchemy.engine import make_url
        u = make_url(cfg["mysql_url"])
        where = f"{u.username}@{u.host}:{u.port}/{u.database}"
    say(f"当前后端：MySQL {where}")
    say("将把「当前后端」改回本地 SQLite。MySQL 上的数据原样保留，不会被删或被改。")
    say("注意：切回后你看到的是**本地那份**账本——它停在当初迁移到 MySQL 的那一刻。")

    if not assume_yes:
        try:
            if input("确认切回本地 SQLite？输入 yes 继续：").strip().lower() != "yes":
                say("已取消。")
                return 1
        except (EOFError, KeyboardInterrupt):
            say("\n已取消。")
            return 1

    switch_to_local()
    say(f"已切回本地 SQLite（当前后端：{current_backend()}）。现在可以正常启动 soroban 了。")
    say("MySQL 恢复后，在应用内「数据库」页点「切换」即可切回去。")
    return 0
