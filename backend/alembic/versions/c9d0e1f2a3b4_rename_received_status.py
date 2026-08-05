"""rename order status 交易成功 → 已签收（为集运中/已到达腾出生命周期尾段）

订单状态在 DB 里存中文字符串（非 native enum），故「加值」无需迁移，「改名」必须回填。
新增的「集运中」「已到达」不需要任何 DDL/数据动作，只在枚举与前端常量里出现。

两处需要回填：
    orders.status              账本订单状态
    orderstaging.order_status  暂存行快照的订单状态（与账本联动，见 routers/staging.py）
方言无关（纯 UPDATE），SQLite/MySQL 通用。

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 列) — 两张表存的是同一套 OrderStatus 值
_TARGETS = (("orders", "status"), ("orderstaging", "order_status"))


def _rename(old: str, new: str) -> None:
    for table, column in _TARGETS:
        op.execute(
            sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old")
            .bindparams(new=new, old=old)
        )


def upgrade() -> None:
    """Upgrade schema."""
    _rename("交易成功", "已签收")


def downgrade() -> None:
    """Downgrade schema."""
    # 「集运中」「已到达」在旧枚举里无对应值 —— 回退时归到最接近的旧终态「交易成功」，
    # 否则这些行会带着非法状态回到旧代码，被 schema 白名单拒掉。
    for value in ("已签收", "集运中", "已到达"):
        _rename(value, "交易成功")
