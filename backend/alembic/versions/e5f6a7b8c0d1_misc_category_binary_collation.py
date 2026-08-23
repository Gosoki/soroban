"""miscexpense.category 改二进制排序规则——它已经变成一根「键列」了

**为什么现在才轮到它**：`f2a3b4c5d6e7`（把键列改成二进制排序规则）当时把判据写得很清楚——
只改两类列：① 参与唯一约束的；② 被 `WHERE col = value` 批量精确匹配的（tags 的改名/删除）。
并且明确说过「别的列（备注、标题、**商品分类**…）按 ci 比较对用户反而更自然」。

那句话在当时是对的：`category` 是一根纯文本列，没有任何按值精确匹配的批量操作。
2026-08-22 把杂项分类接进了标签体系（`tags._FIELD_SOURCES` 加了
`(MiscExpense, MiscExpense.category)`），它于是落进了判据 ②——而排序规则没跟着改。

**不改的后果**（MySQL 上，SQLite 不受影响）：`category` 停在表默认的
`utf8mb4_0900_ai_ci`，大小写与重音都不敏感。于是

- `GET /api/tags/category` 的 `SELECT DISTINCT category` 会把 'EMS' 与 'ems' **折成一个**，
  用户在下拉里根本看不到还有另一个值；
- `POST /api/tags/category/rename?old=EMS` 发的是 `UPDATE ... WHERE category='EMS'`，
  会**连 'ems' 那些行一起改掉**，并推进它们的乐观锁版本——一笔无关的支出被悄悄改了分类；
- `tag_value_in_use()` 同样会误判，导致合法的大小写变体改名被 409 拒掉。

这正是 `f2a3b4c5d6e7` 文档里逐条列过的那一类事故，只是换了一列。

**只动 MySQL**：SQLite 无 COLLATE 即 BINARY，本来就是对的，不需要任何 DDL。
`category` 不参与任何唯一约束，也不是生成列的组成部分，所以不必像那条迁移一样
先拆再建——直接 `alter_column` 即可。

Revision ID: e5f6a7b8c0d1
Revises: d4e5f6a7b8c0
Create Date: 2026-08-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import mysql

from app.db.dialect import bin_collation, is_mysql

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c0d1'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE, _COLUMN, _LEN = "miscexpense", "category", 64


def upgrade() -> None:
    if not is_mysql(op.get_bind()):
        return                                  # SQLite 无 COLLATE 即 BINARY
    op.alter_column(
        _TABLE, _COLUMN,
        existing_type=mysql.VARCHAR(_LEN),
        type_=mysql.VARCHAR(_LEN, collation=bin_collation(op.get_bind())),
        existing_nullable=True,
    )


def downgrade() -> None:
    if not is_mysql(op.get_bind()):
        return
    op.alter_column(
        _TABLE, _COLUMN,
        existing_type=mysql.VARCHAR(_LEN),
        type_=mysql.VARCHAR(_LEN),              # 回落到表默认（ai_ci）
        existing_nullable=True,
    )
