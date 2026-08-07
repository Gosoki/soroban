"""汇率源标识改成具体源名：fallback → erapi

汇率从「主源 + 备用」两档，改成**可配置的优先级链**（默认 中行 → 谷歌 → 通用汇率 API），
所以 `source` 列不该再存「fallback」这种**相对**概念——同一个源，用户把它调到第一位就不是
备用了。改存具体源名（`boc` / `google` / `erapi`），「是不是备用」由「是不是链上第一个」
在读取时算出来（见 routers/fx._read）。

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-08-07 16:10:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(sa.text("UPDATE fxrate SET source = 'erapi' WHERE source = 'fallback'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("UPDATE fxrate SET source = 'fallback' WHERE source = 'erapi'"))
