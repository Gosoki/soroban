"""orderstaging.url —— 与账本 orders.url 对齐

**为什么补这一列**：`Order.url`（商品链接）此前是一根**两头都空的列**——账本上有，
但没有任何生产者（插件不抓）、任何一页也不显示。核对暂存与账本的差集时它被标成
【待定】，理由写的是「今天没有生产者」。

这轮把生产者补上了：淘宝的列表接口响应里，`orderItemInfo_*.fields.item.itemUrl`
就躺在插件已经打开的那个 `fields["item"]` 里（32/32 覆盖，与 `item.itemId` 逐单一致），
拿它**不需要多发一次请求**，也就不触犯「要多爬一次就不做」那条约束。
暂存少这一列的话，插件解析出来的链接在跨表那一步无处可放——而且比丢一格更疼：
`StagingCreate` 带 `model_config = _FORBID`，插件推一个未声明的键会让**整条订单 422**，
落进 failed 桶，整批同步全灭。所以这条迁移必须先于插件那侧上线。

用 Text 不用 String(n)：淘宝的 itemUrl 原样是 100+ 字符（含 mi_id 归因串），
插件侧会白名单清洗到只剩 `?id=`，但清洗规则属于插件、可能随淘宝改版放宽，
库这一侧不该替它设上限。与 `orders.url` 同型。

方言无关：`add_column` 在 SQLite 与 MySQL 上都能直接执行。
**刻意不给 server_default**：MySQL 上 TEXT 列带 DEFAULT 建不出来（错误 1101），
`tests/test_lengths.py::test_no_text_column_has_a_server_default` 守着这条。
可空、无默认 ⇒ 存量行取 NULL，与「还没抓到链接」语义一致，无需数据回填。

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('orderstaging', sa.Column('url', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('orderstaging', 'url')
