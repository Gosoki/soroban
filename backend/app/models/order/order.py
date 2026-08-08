"""商品订单（正式账本）+ 订单行。"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, Relationship

from ...db.dialect import BinStr
from ..base import PURCHASE_EXCLUDED, LedgerBase, PurchaseStatus, price_from_items


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
    # 逐字节比较（BinStr）的列：要么参与唯一约束，要么被 routers/tags 的批量改名/删除
    # 用 `WHERE col = value` 精确命中。MySQL 默认的 _ci 会让前者误撞 1062、后者误伤别的行。
    order_no: Optional[str] = Field(default=None, max_length=64, sa_type=BinStr(64))   # 淘宝订单号；与 platform 共同构成活跃行唯一键
    # 商品标题。用 Text 而非 VARCHAR(255)：一单多件时爬虫会把各件标题拼成「A / B / C」，
    # 轻易超过 255；定长列会逼得入口层截断（悄悄丢字）或 422（打回整批同步）。
    # 本列不参与索引/唯一约束，Text 无代价。
    title: Optional[str] = Field(default=None, sa_type=Text)
    url: Optional[str] = Field(default=None, sa_column=Column(Text))  # 商品链接（可能很长）
    category: Optional[str] = Field(default=None, max_length=64)   # 分类
    purchase_status: str = Field(default=PurchaseStatus.paid.value, max_length=32, index=True)
    platform: Optional[str] = Field(default=None, max_length=32, index=True, sa_type=BinStr(32))   # 来源平台（闲鱼/淘宝/京东）
    express_no: Optional[str] = Field(default=None, max_length=64, index=True)    # 快递号（归组用）
    express_company: Optional[str] = Field(default=None, max_length=64)           # 快递公司
    platform_account: Optional[str] = Field(default=None, max_length=64, index=True, sa_type=BinStr(64))  # 平台账号（各平台的登录号，如淘宝2个）
    shipment_order_id: Optional[int] = Field(
        default=None, foreign_key="shipmentorder.id", index=True
    )  # 可空 = 已买未集运
    postage_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # 邮费（元）；空=包邮(0)。订单价 = Σ(单价×数量) + 邮费

    shipment_order: Optional["ShipmentOrder"] = Relationship(back_populates="orders")  # noqa: F821
    items: list["OrderItem"] = Relationship(  # noqa: F821
        back_populates="order",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    @classmethod
    def ledger_exclusions(cls):
        """看板不计入：待付款（还没花钱）/ 退款 / 交易关闭。只排采购段自己的值。"""
        return [(cls.purchase_status, PURCHASE_EXCLUDED)]

    @property
    def fulfillment_status(self) -> str:
        """界面上该显示的状态：**挂着集运单就跟随集运单，否则用自己的**。

        `status` 只记国内段（下单→国内快递签收），国际段的唯一真相是集运单的 `status`。
        订单不复制它——复制就会漂移，本项目就漂过：7 条订单标「集运中」，它们挂的那张
        集运单标「已发出」，同一件事两处记录对不上。

        释放（解除挂靠）后自动回落到 `status`，因为国内段状态一直原样留在库里、从没被覆盖。
        正因如此，**挂靠时绝不能把集运状态写进 `status`**（旧的 OCR 自动挂靠干过这事，已改）。

        集运单被软删时也回落到自身状态：那张单在界面上已经看不见了，
        再拿它的状态显示会是个查无此处的幽灵值。

        ⚠️ 读它会触碰 `shipment_order` 关系。列表接口必须 `selectinload(Order.shipment_order)`，
        否则整页逐行懒加载 = N+1（tests/test_queries.py 钉着这条）。
        """
        ship = self.shipment_order
        if ship is not None and not ship.is_delete:
            return ship.shipment_status
        return self.purchase_status

    @property
    def shipment_no(self) -> Optional[str]:
        """挂靠集运单的单号，未挂靠/已软删则 None。

        订单行**自带**这个值，是为了让界面显示不依赖「集运单下拉里恰好有这一张」。
        下拉只取前 200 张（否则 DOM 会炸），挂在第 200 名之外的订单曾因此显示成 `#101`
        这种查无此单的内部 id。显示与选择是两件事：显示的真相在订单行上，
        下拉只负责「挑一张出来」。

        与 `effective_status` 共用同一个软删判断，也共用同一条预加载要求
        （`selectinload(Order.shipment_order)`）——因此不产生额外查询。
        """
        ship = self.shipment_order
        if ship is not None and not ship.is_delete:
            return ship.shipment_no
        return None

    def sync_from_items(self) -> None:
        """订单价 = Σ(物品单价×数量) + 邮费，再重算日元。改动 items/邮费 后必须调用。"""
        self.price_cny = price_from_items(self.items) + (self.postage_cny or 0)
        self.compute_money()
