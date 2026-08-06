"""商品订单（正式账本）+ 订单行。"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, Relationship

from ..base import LedgerBase, OrderStatus, price_from_items


class Order(LedgerBase, table=True):
    # 类名 Order → 表名显式钉为 "orders"（默认小写类名 order 是 SQL 保留字）。
    __tablename__ = "orders"

    # P3: (订单号, 来源) 唯一但要兼容软删 → 部分唯一索引（仅未删且订单号非空的行）。
    # 用 COALESCE(platform,'') 参与索引：来源未填(NULL)时仍把同一订单号视为重复，
    # 堵住「无来源时重复导入同一单」的漏洞；不同来源下允许同号（如闲鱼/淘宝各一条）。
    # id 仍是自增主键（本约束不改主键，见 README「唯一性」）。
    #
    # 注意：sqlite_where 部分索引仅 SQLite 生效（供 create_all/autogenerate 参考）。
    # 运行期建表走 Alembic；MySQL 侧此约束由迁移用「生成列 + 唯一键」等价实现
    # （见 app/db/dialect.py），故 MySQL 请勿对本表跑 autogenerate。
    __table_args__ = (
        Index(
            "ix_orders_order_no_platform_active",
            "order_no",
            text("COALESCE(platform, '')"),
            unique=True,
            sqlite_where=text("order_no IS NOT NULL AND is_delete = 0"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    order_no: Optional[str] = Field(default=None, max_length=64)   # 淘宝订单号
    # 商品标题。用 Text 而非 VARCHAR(255)：一单多件时爬虫会把各件标题拼成「A / B / C」，
    # 轻易超过 255；定长列会逼得入口层截断（悄悄丢字）或 422（打回整批同步）。
    # 本列不参与索引/唯一约束，Text 无代价。
    title: Optional[str] = Field(default=None, sa_type=Text)
    url: Optional[str] = Field(default=None, sa_column=Column(Text))  # 商品链接（可能很长）
    category: Optional[str] = Field(default=None, max_length=64)   # 分类
    status: str = Field(default=OrderStatus.paid.value, max_length=32, index=True)
    platform: Optional[str] = Field(default=None, max_length=32, index=True)      # 来源平台（闲鱼/淘宝/京东）
    express_no: Optional[str] = Field(default=None, max_length=64, index=True)    # 快递号（归组用）
    express_company: Optional[str] = Field(default=None, max_length=64)           # 快递公司
    platform_account: Optional[str] = Field(default=None, max_length=64, index=True)  # 平台账号（各平台的登录号，如淘宝2个）
    shipment_order_id: Optional[int] = Field(
        default=None, foreign_key="shipmentorder.id", index=True
    )  # 可空 = 已买未集运
    postage_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # 邮费（元）；空=包邮(0)。订单价 = Σ(单价×数量) + 邮费

    shipment_order: Optional["ShipmentOrder"] = Relationship(back_populates="orders")  # noqa: F821
    items: list["OrderItem"] = Relationship(  # noqa: F821
        back_populates="order",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def sync_from_items(self) -> None:
        """订单价 = Σ(物品单价×数量) + 邮费，再重算日元。改动 items/邮费 后必须调用。"""
        self.price_cny = price_from_items(self.items) + (self.postage_cny or 0)
        self.compute_money()
