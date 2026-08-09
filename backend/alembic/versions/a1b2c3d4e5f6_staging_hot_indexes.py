"""暂存表补三根索引：scraped_at / platform_account / platform

暂存表是**插件写得最勤、页面读得最多**的一张表，却只有 `import_status` 和
`imported_order_id` 两根索引。三个热路径全在裸扫：

  1. `GET /api/staging` 按 `scraped_at DESC, id DESC` 排序分页（routers/staging.py）。
     没有索引 → 每翻一页都是「全表扫描 + filesort」。淘宝插件每轮抓取要 `limit=500`
     翻完整张表两遍（去重用），页数随抓取历史线性增长。
  2. `GET /api/tags/{field}` 对 `platform_account` / `platform` 做 `DISTINCT` 扫描
     （routers/tags._data_values）。账本侧的 `Order` 这两根列**都有**索引，
     暂存侧一根都没有——同一个下拉框的两个数据源，一个走索引一个全表扫。
  3. 订单页/暂存页按账号昵称筛选也落在同两根列上。

三根都建成普通索引即可；`platform_account`/`platform` 在暂存表上是 BinStr
（二进制排序规则，见 f2a3b4c5d6e7），索引对它一样有效。

幂等：建之前先问一次 inspector，重复跑不炸。

Revision ID: a1b2c3d4e5f6
Revises: d2e3f4a5b6c7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "orderstaging"
_INDEXES = (
    ("ix_orderstaging_scraped_at", "scraped_at"),
    ("ix_orderstaging_platform_account", "platform_account"),
    ("ix_orderstaging_platform", "platform"),
)


def _existing() -> set[str]:
    insp = sa.inspect(op.get_bind())
    if _TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(_TABLE)}


def upgrade() -> None:
    """Upgrade schema."""
    have = _existing()
    for name, column in _INDEXES:
        if name not in have:
            op.create_index(name, _TABLE, [column])


def downgrade() -> None:
    """Downgrade schema."""
    have = _existing()
    for name, _ in _INDEXES:
        if name in have:
            op.drop_index(name, table_name=_TABLE)
