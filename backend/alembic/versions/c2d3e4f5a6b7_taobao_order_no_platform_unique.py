"""taobaoorder: (order_no, platform) 唯一，替换原单列 order_no 唯一索引

保证「订单号 + 来源」唯一；id 仍为自增主键（不改主键）。仅约束 order_no 非空且未软删的行，
手动空行草稿（order_no 为空）照旧可多条并存。

方言翻译（见 app/db/dialect.py）：
- SQLite：部分唯一索引 ON (order_no, COALESCE(platform,'')) WHERE 未删且非空。
- MySQL：生成列 = 活跃行时 CONCAT(order_no, 分隔符, COALESCE(platform,''))，否则 NULL；
  再对生成列建唯一键。分隔符用 CHAR(31)（单元分隔符，不会出现在订单号/平台名中），
  避免 ('12','3X') 与 ('123','X') 拼接后相等的边界碰撞。

Revision ID: c2d3e4f5a6b7
Revises: b1f2a3c4d5e6
Create Date: 2026-07-15 22:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import drop_active_unique, emit_active_unique


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, Sequence[str], None] = 'b1f2a3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MYSQL_COMPOSITE = (
    "CASE WHEN order_no IS NOT NULL AND deleted_at IS NULL "
    "THEN CONCAT(order_no, CHAR(31 USING utf8mb4), COALESCE(platform, '')) END"
)


def upgrade() -> None:
    """Upgrade schema."""
    # 撤销 baseline 建的单列活跃唯一（含 MySQL 侧生成列）
    drop_active_unique(op, table='taobaoorder', index_name='ix_taobaoorder_order_no_active',
                       gen_col='order_no_active_key')
    # 建 (order_no, platform) 复合活跃唯一
    emit_active_unique(
        op,
        table='taobaoorder',
        index_name='ix_taobaoorder_order_no_platform_active',
        gen_col='order_no_platform_active_key',
        mysql_expr=_MYSQL_COMPOSITE,
        sqlite_columns="order_no, COALESCE(platform, '')",
        sqlite_where='order_no IS NOT NULL AND deleted_at IS NULL',
    )


def downgrade() -> None:
    """Downgrade schema.

    **先查数据，再动 schema。** 本条 upgrade 的全部意义是「不同来源下允许同号
    （如闲鱼/淘宝各一条）」——降级要装回的是 `order_no` 单列唯一，
    而那正是 upgrade 之后**合法存在**的数据所违反的约束。

    不先查的话：`drop_active_unique` 已经把索引和生成列删掉了，
    随后 `emit_active_unique` 在建唯一索引时撞 1062 / UNIQUE constraint failed。
    **MySQL 的 DDL 是隐式提交的**，于是库停在「新索引没了、旧索引也没建上」的半降级态——
    此后连唯一性都没有人守了，而用户只拿到一句原始报错。

    所以在门口查一次并**如实说出是哪几个号**：用户要做的是先合并/改掉那些重号，
    而不是对着一句 IntegrityError 猜。
    """
    conn = op.get_bind()
    if conn is not None:                       # 离线 --sql 模式下查不了，跳过（那条路本来也执行不了数据步骤）
        dupes = conn.execute(sa.text(
            "SELECT order_no, COUNT(*) c FROM taobaoorder "
            "WHERE order_no IS NOT NULL AND deleted_at IS NULL "
            "GROUP BY order_no HAVING c > 1 LIMIT 5"
        )).fetchall()
        if dupes:
            listed = "、".join(f"{r[0]}（{r[1]} 条）" for r in dupes)
            raise RuntimeError(
                "降级会把唯一约束装回「只按订单号」，但库里有**不同来源的同号订单**——"
                f"那正是本条迁移放开的东西：{listed}。"
                "请先在应用里合并或改掉这些重号，再降级。"
                "（一个字节都还没改，库仍是完好的。）")
    drop_active_unique(op, table='taobaoorder', index_name='ix_taobaoorder_order_no_platform_active',
                       gen_col='order_no_platform_active_key')
    emit_active_unique(
        op,
        table='taobaoorder',
        index_name='ix_taobaoorder_order_no_active',
        gen_col='order_no_active_key',
        mysql_expr="CASE WHEN order_no IS NOT NULL AND deleted_at IS NULL THEN order_no END",
        sqlite_columns='order_no',
        sqlite_where='order_no IS NOT NULL AND deleted_at IS NULL',
    )
