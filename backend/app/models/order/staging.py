"""淘宝抓取暂存（机器人只写这里，人手动导入才进正表）+ 暂存物品行。"""

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, Relationship, SQLModel

from ..base import StagingStatus, price_from_items, utcnow


class OrderStaging(SQLModel, table=True):
    # 淘宝订单号本身全局唯一 → 对非空 order_no 建部分唯一索引（去重键，供 bot upsert）；
    # 允许多条 order_no 为空的手动新建行（SQLite 多 NULL 视为不同，部分索引也不约束 NULL）。
    # MySQL 侧同样由迁移用「生成列 + 唯一键」等价实现（见 app/db/dialect.py）。
    __table_args__ = (
        Index(
            "ix_staging_order_no",
            "order_no",
            unique=True,
            sqlite_where=text("order_no IS NOT NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: Optional[str] = Field(default=None, max_length=64)  # 可空：手动新建空行后再填
    platform_account: Optional[str] = Field(default=None, max_length=64)
    platform: Optional[str] = Field(default=None, max_length=32)  # 来源平台（淘宝/闲鱼/京东）；淘宝插件抓取即「淘宝」，导入时随单迁移到账本
    title: Optional[str] = Field(default=None, sa_type=Text)   # 商品标题；Text 的理由见 Order.title
    price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    postage_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # 邮费（元）；空=包邮。价 = Σ(单价×数量) + 邮费
    fx_rate: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=4)  # 新建/抓取时记当天汇率，导入一同迁移
    order_date: Optional[dt.date] = None
    express_no: Optional[str] = Field(default=None, max_length=64)
    raw_json: Optional[str] = Field(default=None, sa_column=Column(Text))  # 原始留底
    # 一行上有两个「状态」，务必分清：import_status = 导入工作流（待处理/已导入/已忽略），
    # trade_status = 淘宝那边的真实交易状态。旧名 status / order_status 看不出区别。
    import_status: str = Field(default=StagingStatus.pending.value, max_length=32, index=True)
    trade_status: Optional[str] = Field(default=None, max_length=32)  # 淘宝订单真实状态(待发货/待收货/…)；导入后与账本 status 联动
    imported_order_id: Optional[int] = Field(
        default=None, foreign_key="orders.id"
    )
    version: int = Field(default=1)                         # 乐观锁（人工/爬虫并发编辑同一暂存行）
    scraped_at: dt.datetime = Field(default_factory=utcnow)
    updated_at: dt.datetime = Field(default_factory=utcnow)

    items: list["StagingItem"] = Relationship(
        back_populates="staging",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def sync_from_items(self) -> None:
        """暂存价 = Σ(物品单价×数量) + 邮费（与账本同口径）。改动 items/邮费 后调用。"""
        self.price_cny = price_from_items(self.items) + (self.postage_cny or 0)


class StagingItem(SQLModel, table=True):
    """暂存订单的物品行（一单多物），结构对齐 OrderItem（含单价 price_cny / auto）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    staging_id: int = Field(foreign_key="orderstaging.id", index=True)
    name: str = Field(max_length=255)
    quantity: int = Field(default=1)
    unit_price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # **单价**（元）；与订单的 price_cny(总价) 区分
    auto: bool = Field(default=False)                       # True=系统自动生成/自动定价（前端灰显）

    staging: Optional[OrderStaging] = Relationship(back_populates="items")
