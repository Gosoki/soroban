"""pluginconfig.last_ok_at —— 上一次**成功**跑完是什么时候

**为什么 `last_finished_at` 不够**：那一列记的是「上次跑完」，成功失败都算。
于是一个已经连续失败两周的抓取插件，卡片上照样显示一个很新的时间戳——
「最近跑过」和「最近抓到过东西」是两件事，而用户只会看前者并以为一切正常。

这正是这套系统里最安静的一类故障：爬虫的登录会话过期之后，每次定时都照跑、
照失败、照更新 `last_finished_at`，没有任何一处会变红。等到发现时，
已经有两周的订单没进暂存了。

可空、无默认 ⇒ 存量行取 NULL，语义就是「还没成功过（或者这一列上线前的事不算数）」，
不需要数据回填——回填反而会撒谎：把上一次**失败**的时间当成成功时间写进去。

方言无关：`add_column` 在 SQLite 与 MySQL 上都能直接执行。
时间列用 `UtcDateTime`，与本表另外两根时间列同型（MySQL 侧 DATETIME(6)，保住微秒）。

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b8
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c0'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pluginconfig', sa.Column('last_ok_at', UtcDateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('pluginconfig', 'last_ok_at')
