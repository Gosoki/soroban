"""FxRate 加 source 列（汇率来自中行牌价还是备用源）

主源改成中国银行外汇牌价，通用汇率 API 退居备用（见 services/fx.py 的模块文档串）。
前端要在右下角汇率旁显示「备用」，后端要据此判断「主源连续失败了多久」——都靠这一列。

**存量一律标成 `fallback`**：升级前的所有记录都是通用 API 抓的，标 `boc` 就是撒谎，
而且会让「距上次成功取到中行牌价过了多久」的判定从一个假起点开始算——
本该立刻切备用的场景会白等 72 小时。
列默认值仍是 `boc`：今后新写入的行绝大多数来自主源，默认值该对准常态。

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-07 14:40:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "fxrate",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="boc"),
    )
    op.create_index(op.f("ix_fxrate_source"), "fxrate", ["source"])
    # 存量如实归位：它们确实来自备用源
    op.execute(sa.text("UPDATE fxrate SET source = 'fallback'"))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_fxrate_source"), table_name="fxrate")
    op.drop_column("fxrate", "source")
