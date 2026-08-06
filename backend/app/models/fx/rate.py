"""汇率缓存（P7：持久化，抓取失败回退最近一条）。"""

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlmodel import Field, SQLModel

from ...db.dialect import UtcDateTime
from ..base import utcnow


class FxRate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    date: dt.date = Field(unique=True, index=True)          # 该日汇率
    rate: Decimal = Field(max_digits=10, decimal_places=4)  # 1 CNY = rate JPY
    fetched_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
