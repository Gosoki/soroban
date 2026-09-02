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
import logging
from typing import Sequence, Union

from alembic import op

from app.db.dialect import is_mysql

log = logging.getLogger("alembic.runtime.migration")

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME = "ix_orderstaging_imported_order_id"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(_NAME, "orderstaging", ["imported_order_id"])


def downgrade() -> None:
    """Downgrade schema。**MySQL 上这一步是不可能做到的，所以它只记一条日志。**

    `orderstaging.imported_order_id` 上挂着外键 `orderstaging_ibfk_1 → orders.id`，
    而 InnoDB **要求**外键列上有一根以它打头的索引。全表只有这一根，于是：

        (1553, "Cannot drop index 'ix_orderstaging_imported_order_id':
                needed in a foreign key constraint")

    2026-09-01 在真 MySQL 9.7 上实测过。后果不是「降级失败」这么轻——
    **MySQL 的 DDL 是隐式提交的**，从 head 往回降到这里之前的 8 条全部已经落地且不可回滚：
    `pluginrecord` 整张表被 DROP（插件私有存储全没）、`pluginconfig` 的五列被删、
    `d2e3f4a5b6c7` 的降级还 `DELETE FROM fxrate`。
    库就停在一个既不是旧版也不是新版的半降级态，而 README 写着
    「全部迁移在真 MySQL 上跑通 upgrade→downgrade→upgrade」。

    **为什么是「跳过」而不是「先删外键再删索引再建回外键」**：那样绕过去之后，
    MySQL 会为重建的外键**自动再造一根索引**——净效果与不删完全一样，
    只是多了三条 DDL 和三次隐式提交的风险窗口。
    这根索引在 MySQL 上不是可选的调优，是外键的组成部分；
    「回到没有它的那一版」在 MySQL 上根本不存在这个状态。

    SQLite 没有这条约束，照常删——它那边这根索引确实只是加速。
    """
    if is_mysql(op.get_bind()):
        log.info("MySQL：跳过 drop_index %s —— 它是外键 %s 必需的索引，"
                 "InnoDB 不允许删（1553）。留着它与旧版 schema 等价："
                 "那一版在 MySQL 上本来也会有一根 MySQL 自己建的同款索引。",
                 _NAME, "orderstaging_ibfk_1")
        return
    op.drop_index(_NAME, table_name="orderstaging")
