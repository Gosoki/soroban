"""商品订单行（一单多物）——系统对接的最小单位。"""

from decimal import Decimal
from typing import Optional

from sqlmodel import Field, Relationship, SQLModel


class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    name: str = Field(max_length=255)                       # 物品名
    quantity: int = Field(default=1)                        # 数量（小件数，对接口径）
    unit_price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # **单价**（元）；订单价 = Σ(单价×数量) + 邮费
    auto: bool = Field(default=False)                       # True=系统自动生成/自动定价（前端灰显、提示可编辑覆盖）

    order: Optional["Order"] = Relationship(back_populates="items")  # noqa: F821
