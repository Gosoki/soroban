"""列布局（每个表的列顺序+宽度，存后端，所有人一致）。"""

import datetime as dt

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from ...db.dialect import UtcDateTime
from ..base import utcnow


class ColumnLayout(SQLModel, table=True):
    table_name: str = Field(primary_key=True, max_length=32)  # orders / shipment / misc / staging
    columns_json: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # 有序 [{"key":..,"width":..}, ...]
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
