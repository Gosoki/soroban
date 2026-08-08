"""暂存表 imported_order_id 加索引

每次订单 PATCH（`common.mirror_to_staging`）、每次订单删除（`orders.delete_order`）、
每次按账号批量改名（`tags.soft_delete_account_orders`）都要 `WHERE imported_order_id = ?`
反查暂存行。这一列此前没有索引 —— 也就是**每写一次订单就全表扫一遍暂存表**，
而暂存表是三张表里唯一随每轮抓取无上限增长的。

只加这一条、不顺手给 `platform` / `platform_account` / `order_date` 也加：
那三列是列表页的**可选**筛选，且总是与已有索引 `ix_orderstaging_import_status` 叠加使用；
在当前数据量（百量级）上加索引只增加写入成本，等真出现慢查询再按实际查询形态建复合索引。
`scraped_at` 排序同理——留给后续用真实数据量决定。

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME = "ix_orderstaging_imported_order_id"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(_NAME, "orderstaging", ["imported_order_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(_NAME, table_name="orderstaging")
