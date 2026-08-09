"""插件私有存储表 pluginrecord + 插件授权列

插件此前没有「属于自己的可写状态」，于是需要记住点什么（轨迹推过没有、上次轮询时间、
报价快照）时只能塞进账本表或干脆不记。本表是**唯一的通用扩展位**：
新插件要存东西不必再加表、加迁移、加接口。

刻意 schema-free（data 存 JSON 文本）：核心不解释内容，只保证按 plugin_id 隔离命名空间
与单条长度上限。**账本金额绝不依赖它**——无外键、无约束、跨方言 JSON 查询能力弱。

键列用二进制排序规则，与全库键列同口径（否则 SQLite 区分大小写、MySQL 默认不区分，
同一份数据两种后端行为不同）。

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import BinStr, UtcDateTime

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 重跑安全：MySQL 上一次失败的迁移可能已经把表建出来了（DDL 隐式提交，不回滚）。
    # 不加这个判断的话，修好问题再跑会撞「Table already exists」，看起来像另一个错误。
    if "pluginrecord" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("pluginrecord")
    op.create_table(
        "pluginrecord",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("plugin_id", BinStr(64), nullable=False),
        sa.Column("kind", BinStr(64), nullable=False),
        sa.Column("key", BinStr(128), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        # 与全库其余 13 张表一致：引擎与字符集**钉死**，不取「库的默认」。
        # 漏了这一行的话，这张表的默认排序规则会跟着目标库走，而全库其余表不会——
        # 同一个库里两套默认，是最难注意到的那种不一致。
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_pluginrecord_owner", "pluginrecord",
                    ["plugin_id", "kind", "key"], unique=True)
    # 插件被授予了哪些权限。默认 "[]" = **什么都没授权**：
    # 升级后现有插件的令牌立刻什么门都进不去，必须用户去插件页显式勾选。
    # 这是刻意的 fail-closed —— 权限扩大绝不能是升级的副作用。
    # ⚠️ 必须是 VARCHAR 不能是 TEXT：MySQL 的 TEXT/BLOB 列**不允许 DEFAULT**（错误 1101），
    # 而 SQLite 不管——写成 Text 的话本地测试全绿、切到 MySQL 时这次迁移当场失败。
    op.add_column("pluginconfig",
                  sa.Column("granted_scopes", sa.String(length=512), nullable=False,
                            server_default=sa.text("'[]'")))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("pluginconfig", "granted_scopes")
    op.drop_index("ix_pluginrecord_owner", table_name="pluginrecord")
    op.drop_table("pluginrecord")
