"""配置表（预留 key-value；敏感凭证勿明文入库）。"""

import datetime as dt
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from ...db.dialect import UtcDateTime
from ..base import utcnow


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True, max_length=128)
    value: Optional[str] = Field(default=None, sa_column=Column(Text))
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
