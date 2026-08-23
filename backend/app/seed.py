"""Create the dev admin account. Run: python -m app.seed

不开放注册；开发期用这个脚本建一个 admin。可用环境变量覆盖默认账号密码：
  SOROBAN_ADMIN_USER (默认 admin)
  SOROBAN_ADMIN_PASS (默认 admin123)
"""

import os

from sqlmodel import Session, select

from .auth import hash_password
from .database import migrate_to_latest, get_engine
from .models import User


def main() -> None:
    """命令行入口：建表 + 确保有 admin。"""
    migrate_to_latest()
    ensure_admin()


def ensure_admin() -> None:
    """确保有一个管理员账号。**不建表**——调用方负责。

    与 `main()` 分开是因为进程启动时这一步必须跑在**单进程闸之后**：
    原先 `run.py` 在 `uvicorn.run()` 之前调 `main()`，而闸是在 lifespan 里拿的，
    于是 `migrate_to_latest()`（完整 alembic upgrade）跑在闸**之前** ——
    改端口开第二个实例时，新进程会对**正在被老进程使用的库**跑完迁移，
    然后才在 lifespan 里被闸拒绝。而 `main.py` 的 lifespan 特意把
    `single_process.acquire()` 排在建表之前，理由原话是「不要几个进程同时 ALTER 同一个库」。
    """
    username = os.getenv("SOROBAN_ADMIN_USER", "admin")
    password = os.getenv("SOROBAN_ADMIN_PASS", "admin123")

    with Session(get_engine()) as session:
        existing = session.exec(select(User).where(User.username == username)).first()
        if existing:
            print(f"用户 {username!r} 已存在，跳过。")
            return
        user = User(
            username=username,
            password_hash=hash_password(password),
            display_name="管理员",
        )
        session.add(user)
        session.commit()
        # **不打印口令。** 打包版首启是双击运行的，这一行会白纸黑字出现在控制台上——
        # 旁边有人、或用户截图发群里求助时，口令一起进去。
        # 守卫原先只 grep `run.py`（`test_security.py`），而真正 print 的是这里：
        # 把这一行删掉、或加回来，都不会有任何测试变红 —— 典型的「防线钉在实现位置、
        # 而不是行为上」。守卫已改成全仓扫描。
        print(f"已创建管理员 {username}；初始口令见 README / .env 的 SOROBAN_ADMIN_PASS。"
              "请尽快在设置页改掉。")


if __name__ == "__main__":
    main()
