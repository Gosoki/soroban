"""Alembic 环境：用本项目的 SQLModel.metadata 做 autogenerate，URL 取自 app.config。
render_as_batch 仅 SQLite 开（其「建新表拷贝」批处理是 SQLite ALTER 受限的绕行；
MySQL 支持直接 ALTER，开 batch 反而多余、且会干扰生成列等 MySQL 专属 DDL）。"""

import sys
import logging
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# 让 env.py 能 import app.*（alembic 在 backend/ 下运行）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app import models  # noqa: E402,F401  导入以把所有表注册进 SQLModel.metadata

config = context.config
# 只在 ini 的 url 仍是占位符时才用 settings 覆盖——这样 database.py 程序内传入的真实 url（及 CLI -x）能生效
_ini_url = config.get_main_option("sqlalchemy.url")
if not _ini_url or _ini_url.startswith("driver://"):
    # alembic 的 Config 底层是 ConfigParser，% 会被当成插值语法（MySQL 密码里的 %40 会炸）；
    # 写入时转义 %→%%，get_section 读回时插值会还原成真实 URL。
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))
# **只在没人配过日志时才由 alembic 来配。**
#
# `fileConfig()` 会按 alembic.ini 的 `[handlers]` **重建 root 的 handler 列表**。
# `disable_existing_loggers=False` 只保住了 logger 不被禁用，管不到 handler 被换掉。
# 在别人的进程里跑迁移时，那一句会把宿主已经装好的 handler 掀掉：
#
#   · 应用进程：`app/main.py` 的 `basicConfig` 装的那个 handler 没了
#     （启动时会 run_migrations，此后的日志靠 alembic.ini 里那份配置，而不是应用自己的）；
#   · **pytest**：装在 root 上的 caplog handler 没了 ⇒ 用例内跑过迁移之后
#     `caplog.records` **恒为空**，日志照常打印在终端上，断言却什么都抓不到——
#     于是被读成「这条日志根本没打」。2026-08-22 写备份守卫时当场撞上，
#     排查了半天才发现不是代码没打日志。
#
# 判据是「root 上有没有 handler」：独立跑 `alembic upgrade` 时没有，由 alembic 配；
# 被宿主进程（应用 / pytest）驱动时有，就别插手——alembic 自己的日志会照常
# 沿 logger 树传播到宿主的 handler 上，一条都不会少。
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    # 取「生效后」的 url（上面已把 CLI -x / database.py 程序内传入的真实 url 写进 config），
    # 而非死用 settings.DATABASE_URL——后者恒为 SQLite 控制库，会让 `alembic upgrade --sql`
    # 对 MySQL 目标生成 SQLite 方言 DDL、且指错库。与 run_migrations_online 保持同一 url 来源。
    url = config.get_main_option("sqlalchemy.url") or settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # SQLite 才需「建新表拷贝」批处理；MySQL 直接 ALTER
            compare_type=True,
            # **每条迁移各自一个事务，而不是整条链一个。**
            # MySQL 的 DDL 是隐式提交的，而 `alembic_version` 的 UPDATE 是普通 DML：
            # 整条链包在一个事务里时，链跑到第 N 条抛错，前 N-1 条的 **DDL 已经落地、
            # 版本号却被回滚掉了**。用户按报错提示处理完再重跑，会从 N-1 重来，
            # 撞上一个和原始错误毫无关系的「Duplicate key name」——
            # 而那正是本项目最怕的「半升级态」。
            # 每迁移一个事务之后，抛错点之前的每一条都带着自己的版本号落地，重跑从断点继续。
            # SQLite 侧 DDL 本来可回滚，这个改动对它是等价的。
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
