"""插件运行结果三列

插件是 fire-and-forget：起了子进程就返回，结果由后台线程收割写日志。
于是界面上「跑完了吗、成没成、抓了几条」一个字都看不到——用户唯一能做的是去翻 soroban 日志。
本迁移把结果落到 PluginConfig 上，插件卡片直接显示。

`last_run_at`（已有）是「上次**触发**时间」，供定时判断用；这三列是「上次**结束**的结果」。
两者语义不同，故不合并。

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import UtcDateTime

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# VARCHAR 而不是 TEXT：带 DEFAULT 的 TEXT 列在 MySQL 上建不出来（错误 1101），
# 而 SQLite 照单全收 —— 那是本项目最常见的一类双引擎发散，上一版刚栽过。
_COLS = [
    ("last_outcome", sa.String(length=16), "''"),
    ("last_summary", sa.String(length=512), "''"),
]


def upgrade() -> None:
    """Upgrade schema."""
    for name, typ, default in _COLS:
        op.add_column("pluginconfig",
                      sa.Column(name, typ, nullable=False, server_default=sa.text(default)))
    op.add_column("pluginconfig", sa.Column("last_finished_at", UtcDateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pluginconfig", "last_finished_at")
    for name, _, _ in reversed(_COLS):
        op.drop_column("pluginconfig", name)
