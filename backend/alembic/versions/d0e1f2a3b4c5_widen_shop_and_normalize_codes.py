"""商品标题列放宽为 TEXT + 单号类列统一大写去空格

两件事，都是为了消除「同一份数据在 SQLite 与 MySQL 上表现不同」：

1. `orders.shop` / `orderstaging.shop` VARCHAR(255) → TEXT
   这两列存的是**商品标题**。一单多件时爬虫会把各件标题拼成「A / B / C」，轻易超过 255。
   定长列逼得入口层二选一：截断（悄悄丢字）或 422（打回整批同步）。两列都不参与索引/唯一
   约束，改 TEXT 无代价。**SQLite 侧不做 DDL**——SQLite 不强制 VARCHAR 长度，其列本来就
   等价于无限长 TEXT，跑 batch ALTER 反而要重建表、有搞坏部分唯一索引的风险。

2. 快递单号/国际单号统一 `UPPER(TRIM(...))`
   OCR 提取时就 `.upper()` 了，用户手输可能是小写或带首尾空格；而精确匹配在 **SQLite 上区分
   大小写、MySQL 默认不区分**——同一份数据两种后端行为不同。入口层已改为写入即归一，
   这里把历史数据一起补齐，让集运「内含快递」自动挂靠稳定命中。
   只动**没有唯一约束**的列（express_no / intl_tracking_no）：order_no、shipment_no 上有
   唯一索引，批量改写有撞约束导致升级失败的风险，且它们实际都是纯数字/数字加连字符，
   大小写本就不是问题——那两列只在入口层去首尾空格，不改历史数据。

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-05 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import is_mysql

# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (表, 列)：存商品标题的自由文本列
_TITLE_COLUMNS = (("orders", "shop"), ("orderstaging", "shop"))
# (表, 列)：单号类列，写入即 UPPER+TRIM。仅限**无唯一约束**的列，理由见模块文档。
_CODE_COLUMNS = (
    ("orders", "express_no"),
    ("orderstaging", "express_no"),
    ("shipmentorder", "intl_tracking_no"),
)


def _normalize_codes() -> None:
    """把历史单号统一成 UPPER(TRIM(...))；空串一并归成 NULL（否则 IS NOT NULL 过滤会放行空值）。
    UPPER/TRIM 是 SQLite 与 MySQL 都有的标准函数，无需分方言。"""
    for table, column in _CODE_COLUMNS:
        op.execute(sa.text(
            f"UPDATE {table} SET {column} = UPPER(TRIM({column})) WHERE {column} IS NOT NULL"))
        op.execute(sa.text(f"UPDATE {table} SET {column} = NULL WHERE {column} = ''"))


def upgrade() -> None:
    """Upgrade schema."""
    if is_mysql(op.get_bind()):
        for table, column in _TITLE_COLUMNS:
            op.alter_column(table, column, type_=sa.Text(), existing_nullable=True)
    _normalize_codes()


def downgrade() -> None:
    """Downgrade schema.

    注意：单号的大小写/空格无法还原（信息已丢失），也没必要——归一后的值在旧代码里同样可用。
    标题列收回 VARCHAR(255) 时，**超过 255 字的标题会被 MySQL 截断**。"""
    if is_mysql(op.get_bind()):
        for table, column in _TITLE_COLUMNS:
            op.alter_column(table, column, type_=sa.String(length=255), existing_nullable=True)
