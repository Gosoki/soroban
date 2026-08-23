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

from app.db.dialect import BinStr, UtcDateTime, require_online

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    require_online("读取现有数据")
    # 重跑安全：MySQL 上一次失败的迁移可能已经把表建出来了（DDL 隐式提交，不回滚）。
    # 不加这个判断的话，修好问题再跑会撞「Table already exists」，看起来像另一个错误。
    #
    # ⚠️ **但只丢空壳，绝不丢数据。** 这是全链 25 个 `upgrade()` 里唯一一条
    # `drop_table`（其余 12 处全在 `downgrade()` 里）。它要收拾的那条路径上，
    # 表必然是空的——建完就炸、没有任何写入者跑过。而代码原先不区分
    # 「空壳残留」和「有数据」：只要库里有一张同名表而 `alembic_version` 还没走到这里，
    # 它就会连同全部插件私有数据一起删掉，没有日志、没有备份、没有计数。
    # `database.py` 的 pre-Alembic 收养逻辑（丢了 `alembic_version` 就 stamp 回 baseline
    # 重跑全链）理论上能走到这里。非空就抬起来报错，让人来决定。
    bind = op.get_bind()
    if "pluginrecord" in sa.inspect(bind).get_table_names():
        n = bind.execute(sa.text("SELECT COUNT(*) FROM pluginrecord")).scalar() or 0
        if n:
            raise RuntimeError(
                f"迁移 b0c1d2e3f4a5 想重建 pluginrecord，但它里面有 {n} 行数据。"
                "这条重建只为收拾「建完就炸的空壳」，不该丢任何东西。"
                "请先把这张表备份/导出，确认无用后手动 DROP，再重跑迁移。")
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
