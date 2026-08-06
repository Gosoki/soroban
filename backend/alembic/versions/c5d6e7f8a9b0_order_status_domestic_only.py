"""商品订单状态只保留国内段：去掉「集运中」「已到达」

**为什么**：国际段的状态本来就有唯一真相——所挂靠集运单的 `ShipmentStatus`。订单再存一份
就是同一件事两处记录，必然漂移，而且**已经漂了**：升级前 7 条订单标「集运中」，
它们挂的那张集运单标「已发出」。

改法不是「同步两边」，而是**取消订单侧的这两个值**：订单一旦挂上集运单，界面显示的状态就
跟随那张单（`Order.effective_status`）；释放出来则回落到订单自己的国内段状态——
而国内段状态一直原样留在库里、从没被覆盖，所以回落是准的。

**存量归位**（按用户拍板）：两个值统统改成「已签收」。
- 7 条「集运中」：都挂着集运单，国内段确实已签收；改完界面显示继承来的「已发出」，不丢信息。
- 29 条「已到达」：**一张集运单都没挂**，改成「已签收」会丢掉「国际段已送达」这个事实
  （用户已知悉并确认这样处理）。若日后想找回，可补一张集运单把它们挂上去。

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-06 15:10:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GONE = ("集运中", "已到达")
_TARGET = "已签收"
# 两张表都要：暂存的 trade_status 是淘宝那边的交易状态，理论上不会出现这两个值，
# 但历史上人工改过暂存行，一并归位免得留下枚举白名单外的幽灵值（会让整页 422）。
_COLUMNS = (("orders", "status"), ("orderstaging", "trade_status"))


def upgrade() -> None:
    """Upgrade schema."""
    for table, column in _COLUMNS:
        for old in _GONE:
            op.execute(
                sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old")
                .bindparams(new=_TARGET, old=old)
            )


def downgrade() -> None:
    """Downgrade schema.

    **不可逆**：升级把两个值都并进了「已签收」，无从分辨哪些原本是「集运中」、
    哪些是「已到达」。这里只恢复枚举的存在性，不动数据——硬猜着还原只会造出假数据。
    """
