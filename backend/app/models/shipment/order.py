"""集运订单。"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import Index, text
from sqlmodel import Field, Relationship

from ...db.dialect import BinStr
from ..base import LedgerBase, ShipmentStatus


class ShipmentOrder(LedgerBase, table=True):
    # 集运单号唯一但兼容软删 → 部分唯一索引（仅未删且单号非空）。MySQL 侧由迁移用
    # 「生成列 + 唯一键」等价实现（见 app/db/dialect.py）。
    __table_args__ = (
        Index(
            "ix_shipmentorder_shipment_no_active",
            "shipment_no",
            unique=True,
            sqlite_where=text("shipment_no IS NOT NULL AND is_delete = 0"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    shipment_no: Optional[str] = Field(default=None, max_length=64, sa_type=BinStr(64))   # 集运单号；活跃行唯一键，故逐字节比较
    weight: Optional[Decimal] = Field(default=None, max_digits=8, decimal_places=2)  # 重量kg
    intl_tracking_no: Optional[str] = Field(default=None, max_length=128)  # 国际运单号
    status: str = Field(default=ShipmentStatus.packing.value, max_length=32, index=True)
    special_fee_jpy: Optional[int] = Field(default=None)    # 特殊费（恒日元：关税/消费税等）
    recipient: Optional[str] = Field(default=None, max_length=128, sa_type=BinStr(128))   # 收货人（标签，被标签改名按值精确匹配）

    orders: list["Order"] = Relationship(back_populates="shipment_order")  # noqa: F821

    def compute_money(self, extra_jpy: int = 0) -> None:
        # 集运 jpy_auto = round(运费×汇率) + 特殊费_日元
        super().compute_money(extra_jpy=(self.special_fee_jpy or 0) + extra_jpy)
