"""orderstaging.express_company —— 与账本 orders.express_company 对齐

**为什么补这一列**：`express_no`（快递单号）与 `express_company`（快递公司）是同一件事
的两半，账本两列都有，而暂存只有前者。于是淘宝插件从列表接口里同时解析出来的
「申通快递 / 773435263240616」，只有单号能落地，公司名在跨表那一步被丢掉——
不是被拒绝，是**根本没有地方放**，链路上一个字节的报错都不会有。

暂存与账本不是两张完全相同的表（暂存有导入工作流列、账本有集运关联列），
但**共享的业务列必须逐列对齐**：那份清单就是 `routers/staging.py::_SHARED_TO_ORDER`，
`_overlay`（读时用账本值覆盖）与 `mirror_to_staging`（写回镜像）都从它派生。
少一列的表现是「导入时丢一格、而且丢得很安静」。

方言无关：`add_column` 在 SQLite 与 MySQL 上都能直接执行。
可空、无默认值 ⇒ 存量行取 NULL，与「还没抓到快递公司」语义一致，无需数据回填。
列宽 64 与 `orders.express_company` 一致；不是键列，故不用 BinStr。

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orderstaging',
                  sa.Column('express_company', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orderstaging', 'express_company')
