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
    # 这条汇率是从哪来的："boc" = 中国银行牌价（主源）／"fallback" = 通用汇率 API（备用）。
    # 前端据此在右下角汇率旁显示「备用」；后端据此推导「主源连续失败了多久」——
    # 不另存计时器，最近一次 source="boc" 的 fetched_at 就是唯一真相（见 services/fx.py）。
    source: str = Field(default="boc", max_length=16, index=True)
