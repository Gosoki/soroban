"""MySQL 侧时间列改成 DATETIME(6)，保住微秒

**问题**：模型里的 `sa.DateTime()` 在 MySQL 上编译成裸 `DATETIME`，即 fsp=0。
而 MySQL 对超出精度的小数秒是**四舍五入**（不是截断），且不产生 warning。实测 MySQL 9.7：

    INSERT ... '2026-08-05 14:59:59.700000'  →  读回 '2026-08-05 15:00:00'

跨了分钟和小时；落在 UTC 日界附近（14:59:59.5 之后，JST 次日）时还会跨日——暂存页的
「入库日期」列按 JST 显示，迁移前后会差一天。

SQLite 侧把 DATETIME 存成 ISO 字符串、微秒完整保留，所以这是一处纯粹的「迁过去精度就变了」
的双引擎发散。影响面确实不大（迁移指纹是同引擎自比，排序都有 id 兜底，四舍五入是单调不减
映射所以不会让 updated_at 跑到 created_at 前面），但修它是零代价的。

**SQLite 侧无 DDL**：它本来就存全精度，这一版对 SQLite 是纯 no-op。

**已被四舍五入过的历史数据无法还原**——那部分精度在写入 MySQL 时就永久丢了。
这一版只保证今后不再丢。

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-06 07:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import mysql

from app.db.dialect import is_mysql

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 列, 可空) —— MODIFY 会重写整个列定义，可空必须与模型一致，漏写就把 NOT NULL 丢了。
_DATETIME_COLUMNS = [
    ('columnlayout',  'updated_at',  False),
    ('fxrate',        'fetched_at',  False),
    ('pluginconfig',  'last_run_at', True),
    ('pluginconfig',  'updated_at',  False),
    ('setting',       'updated_at',  False),
    ('user',          'created_at',  False),
    ('miscexpense',   'created_at',  False),
    ('miscexpense',   'updated_at',  False),
    ('shipmentorder', 'created_at',  False),
    ('shipmentorder', 'updated_at',  False),
    ('orders',        'created_at',  False),
    ('orders',        'updated_at',  False),
    ('orderstaging',  'scraped_at',  False),
    ('orderstaging',  'updated_at',  False),
]


def _set_fsp(fsp: int) -> None:
    """MySQL 的 MODIFY 会整条重写列定义，所以 existing_type 只是 alembic 渲染时的占位，
    真正决定结果的是 type_ 与 existing_nullable。"""
    for table, column, nullable in _DATETIME_COLUMNS:
        op.alter_column(
            table, column,
            existing_type=mysql.DATETIME(),
            type_=mysql.DATETIME(fsp=fsp) if fsp else mysql.DATETIME(),
            existing_nullable=nullable,
        )


def upgrade() -> None:
    """Upgrade schema."""
    if is_mysql(op.get_bind()):
        _set_fsp(6)


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 这一步**有损**：回到 fsp=0 会把所有时间戳四舍五入到整秒，且不可还原。
    """
    if is_mysql(op.get_bind()):
        _set_fsp(0)
