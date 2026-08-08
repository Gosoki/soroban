"""汇率缓存（P7：持久化，抓取失败回退最近一条）。"""

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from ...db.dialect import UtcDateTime
from ..base import utcnow


class FxRate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # 该条汇率对应的**日期**。刻意**不再唯一**：一天可以有多条（每次抓取一条），
    # 因为「爬虫补录前几天买的东西」要按**那一天**折算，而那一天可能抓过好几次；
    # 只留一条就等于强行抹掉当天的变动，也没法回看「那天几点是多少」。
    date: dt.date = Field(index=True)
    rate: Decimal = Field(max_digits=10, decimal_places=4)  # 1 CNY = rate JPY
    fetched_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
    # 这条汇率的来源标识。"manual" = 用户在设置页手填；其余由汇率插件自报（"boc"/"google"/…），
    # 核心只存不解释。展示名在前端 constants，核心侧只认识 "manual"。
    source: str = Field(default="unknown", max_length=16, index=True)

    __table_args__ = (
        # 取「某日最后一条」是最频繁的读法（建单、补录都走它），复合索引直接覆盖。
        Index("ix_fxrate_date_fetched", "date", "fetched_at"),
    )
