"""汇率一天可以有多条

原先 `fxrate.date` 是唯一键，一天只留一条、后写的覆盖先写的。两个问题：

  1. **补录会用错汇率**。爬虫抓回几天前买的东西，该按**那一天**折算；而那一天可能
     抓过好几次，只留一条就是强行抹掉当天的变动。
  2. **回看不了**。「那天几点是多少」查不到——出账对不上时无从复核。

改成追加式：每次抓取记一条。取哪一条是**读**侧的事（services/fx.pick_on：
手填优先，其次该日最后一条）。

存储量：6 小时抓一次 = 一天 4 条，一年约 1500 条，可以忽略。

⚠️ SQLite 不能直接删索引以外的约束，但这里的唯一性是**索引**（ix_fxrate_date，
SQLModel 的 `Field(unique=True, index=True)` 建出来的是唯一索引），
所以两个方言都只要 drop + create，不必重建表。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(name: str) -> bool:
    return name in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("fxrate")}


def upgrade() -> None:
    """Upgrade schema."""
    # 唯一 → 普通。先删再建，两方言同路径。
    if _has_index("ix_fxrate_date"):
        op.drop_index("ix_fxrate_date", table_name="fxrate")
    op.create_index("ix_fxrate_date", "fxrate", ["date"], unique=False)
    # 「某日最后一条」是最频繁的读法（建单、补录都走它），复合索引直接覆盖
    if not _has_index("ix_fxrate_date_fetched"):
        op.create_index("ix_fxrate_date_fetched", "fxrate", ["date", "fetched_at"])


def downgrade() -> None:
    """Downgrade schema。

    ⚠️ 回退要恢复唯一约束，而此时同一天很可能已经有多条——直接建唯一索引会失败。
    所以**先按日期去重**（每天只留最后抓到的那条），再建。这一步会丢数据，
    但那正是回退到「一天一条」的含义。
    """
    op.execute("""
        DELETE FROM fxrate WHERE id NOT IN (
            SELECT keep FROM (
                SELECT MAX(id) AS keep FROM fxrate GROUP BY date
            ) t
        )
    """)
    if _has_index("ix_fxrate_date_fetched"):
        op.drop_index("ix_fxrate_date_fetched", table_name="fxrate")
    if _has_index("ix_fxrate_date"):
        op.drop_index("ix_fxrate_date", table_name="fxrate")
    op.create_index("ix_fxrate_date", "fxrate", ["date"], unique=True)
