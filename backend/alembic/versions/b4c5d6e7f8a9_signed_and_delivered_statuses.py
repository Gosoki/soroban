"""订单「已入仓」→「已签收」，集运「已签收」→「已送达」

按用户口径重新划分两段的状态词：

- **订单**（`orders.status` / `orderstaging.trade_status`）记的是**国内段**：
  快递签收就叫「已签收」——淘宝/闲鱼页面上的「交易成功」就是这一刻。
  上一版为避开与集运单「已签收」的同名冲突，把它改叫过「已入仓」；现在集运侧改叫
  「已送达」，冲突消失，用回用户的说法。
- **集运单**（`shipmentorder.status`）记的是**国际段**：包裹送到本人手上叫「已送达」。

**存量数据全部源自「交易成功」**，所以直接归位到「已签收」：升级前账本里 1 条、暂存里 14 条
`已入仓`，无一例外都是爬虫/OCR 从「交易成功」映射来的，没有任何一条是人工标记的真·入仓
（`已入仓` 只存在了一个版本）。集运侧升级前没有任何一条 `已签收`，改名是零数据风险。

两张表互不干扰：orders 侧是 已入仓→已签收，shipmentorder 侧是 已签收→已送达，
不存在「先改 A 再改 B 会撞车」的顺序问题。

⚠️ `已入仓` 这个概念本身没有消失，只是**暂不作为独立状态**。后续计划是：订单一旦挂上集运单，
展示层就跟随集运单的状态，国内/国际两段各自只有一个真相来源（详见 docs/README.md 第五十一版）。

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-06 14:20:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 列, 旧值, 新值)
_RENAMES = [
    ("orders", "status", "已入仓", "已签收"),
    ("orderstaging", "trade_status", "已入仓", "已签收"),
    ("shipmentorder", "status", "已签收", "已送达"),
]


def _rewrite(pairs) -> None:
    for table, column, old, new in pairs:
        op.execute(
            sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old")
            .bindparams(new=new, old=old)
        )


def upgrade() -> None:
    """Upgrade schema."""
    _rewrite(_RENAMES)


def downgrade() -> None:
    """Downgrade schema."""
    _rewrite([(t, c, new, old) for t, c, old, new in _RENAMES])
