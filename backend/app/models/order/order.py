"""商品订单（正式账本）+ 订单行。"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, Relationship

from ...db.dialect import BinStr
from ..base import PURCHASE_EXCLUDED, LedgerBase, PurchaseStatus, items_carry_no_price, price_from_items


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

        与 `fulfillment_status` 共用同一个软删判断，也共用同一条预加载要求
        （`selectinload(Order.shipment_order)`）——因此不产生额外查询。
        """
        ship = self.shipment_order
        if ship is not None and not ship.is_delete:
            return ship.shipment_no
        return None

    def sync_from_items(self, *, derive_price: Optional[bool] = None) -> None:
        """订单价 = Σ(物品单价×数量) + 邮费，再重算日元。改动 items/邮费 后必须调用。

        `derive_price` 是**三态**，说的是「调用方对『这次知不知道钱是多少』有没有依据」：
          · `None`（默认）= 没依据 → 按 items 自行推断（`items_carry_no_price`）。
            这是**安全默认**：它保住存量 NULL，挡的是「改个状态下拉就把 ¥300 变 ¥0」。
          · `True`  = 确知这次知道钱是多少 → 派生。建单站点用它（没有存量可保护，
            payload 就是全部真相），以及「用户主动清空了全部单价」那种明确决定。
          · `False` = 确知这次**不**知道 → 一个字都不动货款，只重算日元。

        推断只是**存量保护**，不是判断力：调用方手里有 payload、有替换前的存量状态，
        它知道的严格多于事后看 items。所以显式结论必须能压过推断——
        「存量有价 + 传来全空 = 用户主动清空 → 归零」就只能靠 `True` 表达，
        事后看 items 与「本来就全空」一模一样（见 `test_clearing_all_prices_zeroes_them`）。

        `derive_price=False` = **调用方已经知道这次不该派生货款**，只重算日元。
        它挡的是一个 `items_carry_no_price` 看不见的漏洞：带 items 的 PATCH 会先过
        `build_items`，而那里把「没单价」重新编码成 `(0.00, auto=True)`
        （那段注释自己写着「没给种子就是不知道单价，一律记 0 + auto」）——
        等到这里，NULL 已经没了，闸形同虚设。
        实测：一张 ¥320.00 / 6400 円、物品单价全 NULL 的历史订单，
        在展开面板里**改一个物品名**，保存后变成 **¥0.00 / 0 円**，HTTP 200、零提示。
        而下面 `items_carry_no_price` 的 docstring 承诺的正是「任何一次 PATCH 都不会」。

        **为什么不让判据去认 `(0.00, auto=True)`**：那个编码有歧义——
        `build_items` 对「订单价种子就是 0」（包邮/赠品单）产出的也是它。
        认它的话，一张真的 0 元单会永远派生不出价。
        而在 `update_order` 那一层，「这次 payload 到底带没带价」是**明确的**，
        就在那里判、把结论传下来。

        **派生不出来就不动 `price_cny`**，理由见 `items_carry_no_price`：
        「不知道多少钱」不是「这单值 0 元」，而按后者算出来的恰好是 `0 + 邮费`。
        暂存侧（`OrderStaging.sync_from_items`）用的是同一条判据——
        它先补的是「零物品」那一半，这里补齐的是「有物品、单价全 NULL」那一半，
        而后者才是历史订单的真实形态（`f6a7b8c9d0e1` 只加列、回填靠一个没人跑过的脚本）。

        **但 `compute_money()` 不在那道闸里面。** 闸问的是「能不能**从物品**派生出货款」，
        重算日元问的是「按现有的 `price_cny` / `fx_rate` / `jpy_override`，结算该是多少」
        ——两件事没有关系。早先把它一起圈进去的后果很具体：物品单价全 NULL 的订单
        （正是上面说的那种历史形态，`app/demo.py` 今天仍在造）填「覆盖（円）」永远不生效，
        HTTP 200、响应里 `jpy_override` 是新值而 `jpy_settled` 还是旧值——
        而这两列在订单页是**并排显示**的，用户同时看到「覆盖 3500」和「结算 6000」，
        看板也仍按旧值算。改汇率、`stamp_fx` 自愈存量脏行，同样一并被跳过。
        """
        if derive_price is None:                       # 没依据 → 退回按 items 推断
            derive_price = not items_carry_no_price(self.items)
        if derive_price:
            self.price_cny = price_from_items(self.items) + (self.postage_cny or 0)
        self.compute_money()
