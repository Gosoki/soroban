"""淘宝抓取暂存（机器人只写这里，人手动导入才进正表）+ 暂存物品行。"""

import datetime as dt
from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, Relationship, SQLModel

from ...db.dialect import BinStr, UtcDateTime
from ..base import ImportStatus, guard_cny, items_carry_no_price, price_from_items, utcnow


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
    # BinStr 的理由见 Order 同名列（唯一约束 / 批量精确匹配，必须逐字节）。
    order_no: Optional[str] = Field(default=None, max_length=64, sa_type=BinStr(64))  # 可空：手动新建空行后再填
    # 这两根列 index=True 与账本侧 `Order` 对齐：`GET /api/tags/{field}` 对它们做
    # DISTINCT 扫描（同一个下拉框的两个数据源，不能一个走索引一个全表扫），
    # 订单页/暂存页按账号昵称筛选也落在这里。见迁移 a1b2c3d4e5f6。
    platform_account: Optional[str] = Field(default=None, max_length=64, index=True, sa_type=BinStr(64))
    platform: Optional[str] = Field(default=None, max_length=32, index=True, sa_type=BinStr(32))  # 来源平台（淘宝/闲鱼/京东）；淘宝插件抓取即「淘宝」，导入时随单迁移到账本
    title: Optional[str] = Field(default=None, sa_type=Text)   # 商品标题；Text 的理由见 Order.title
    price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    postage_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # 邮费（元）；空=包邮。价 = Σ(单价×数量) + 邮费
    fx_rate: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=4)  # 新建/抓取时记当天汇率，导入一同迁移
    order_date: Optional[dt.date] = None
    express_no: Optional[str] = Field(default=None, max_length=64)
    # 与账本 `Order.express_company` 对齐。快递单号与快递公司是同一件事的两半，
    # 账本两列都有；暂存少这一列时，插件从同一个响应里解析出来的公司名
    # 在跨表那一步被静默丢掉——不是被拒绝，是根本没有地方放。
    express_company: Optional[str] = Field(default=None, max_length=64)
    # 与账本 `Order.url` 对齐。淘宝插件从列表接口的 orderItemInfo.item.itemUrl 里直接拿得到
    # （零额外请求），此前暂存没有这一列，于是那个值在跨表那一步无处可放。
    # 用 sa_type 而不是 sa_column=Column(Text)：DDL 完全相同，但 sa_column 会让 SQLModel
    # 忽略 Field 层的 nullable/index/default（今天无害，将来有人加 index=True 会静默不生效），
    # 且一个 Column 实例只能绑一张表，照抄 Order 那行更是绑错表。同文件 title 用的就是 sa_type。
    url: Optional[str] = Field(default=None, sa_type=Text)     # 商品链接；Text 的理由同 title
    raw_json: Optional[str] = Field(default=None, sa_column=Column(Text))  # 原始留底
    # 一行上有两个「状态」，务必分清：import_status = 导入工作流（待处理/已导入/已忽略），
    # purchase_status = 淘宝那边的真实交易状态。旧名 status / order_status 看不出区别。
    import_status: str = Field(default=ImportStatus.pending.value, max_length=32, index=True)
    purchase_status: Optional[str] = Field(default=None, max_length=32)  # 淘宝订单真实状态(待发货/待收货/…)；导入后与账本 status 联动
    # index=True 不是可选项：每次订单 PATCH（mirror_to_staging）、每次订单删除、
    # 每次按账号批量改名，都要按这一列反查暂存行。没有索引就是每次写订单都全表扫暂存表。
    imported_order_id: Optional[int] = Field(
        default=None, foreign_key="orders.id", index=True
    )
    version: int = Field(default=1)                         # 乐观锁（人工/爬虫并发编辑同一暂存行）
    # index=True：暂存列表就按它排序分页（`scraped_at DESC, id DESC`），
    # 没索引则每翻一页都是全表扫描 + filesort，而淘宝插件每轮要翻完整张表两遍。
    scraped_at: dt.datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime())
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())

    items: list["StagingItem"] = Relationship(
        back_populates="staging",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def sync_from_items(self, *, derive_price: Optional[bool] = None) -> None:
        """暂存价 = Σ(物品单价×数量) + 邮费（与账本同口径）。改动 items/邮费 后调用。

        卡口与账本侧同源（guard_cny）：账本走 compute_money 顺带卡，暂存没有日元派生、
        不调 compute_money，所以必须在这里显式卡一次——否则「暂存能存、导入成账本才报错」，
        脏行会一直躺在暂存表里，还会让整个暂存列表被 response_model 打成 422。

        **派生不出来时什么都不做**（判据见 `items_carry_no_price`，账本侧同源）。
        它管两种情形：一条物品都没有，**以及有物品但单价全是 NULL**——
        后者是 `f6a7b8c9d0e1` 那次只加列留下的历史形态，比前者常见得多。
        「没有物品」的意思是**不知道明细**，
        不是「这单值 0 元」——而按前者算出来的恰好是后者（`0 + 邮费`）。
        0 物品的暂存行是真实存在的历史状态：`f6a7b8c9d0e1` 那次只加了列，
        既有行的回填留给 `tools/backfill_item_price.py`，没跑过的库里就有一堆。
        今天的 API 造不出这种行（`build_items` 对空列表也会补一条占位），
        所以空 items **只会**是那种老行，保住它的价永远是对的。

        不这么改的话，同一个伤害要在三条路上各打一次补丁（审计报告 §154 / §168）：
        订单 PATCH 的镜像、暂存 PATCH 的已导入分支、暂存 PATCH 的未导入分支——
        而第三条上连「账本价」都没有可镜像的东西。

        `derive_price` 与账本侧 `Order.sync_from_items` **同一套三态语义**（见那里的说明）：
        `None`=按 items 推断（安全默认，保住存量 NULL）、`True`=确知知道、`False`=确知不知道。
        暂存侧原先只有推断这一条路，而推断读的是**刚被 `build_items` 替换过**的 items——
        于是一张 ¥320、物品单价全 NULL 的历史暂存行，**改一次物品名**就变 ¥0.00
        （HTTP 200、零提示）：`build_items` 把 NULL 写成 0.00，判据当场失效。
        那个伪造已经拔掉了（见 `build_items` 的返回），这里补上显式结论的入口，
        好让「用户主动清空全部单价 → 归零」仍然表达得出来。
        """
        if derive_price is None:                       # 没依据 → 退回按 items 推断
            derive_price = not items_carry_no_price(self.items)
        if not derive_price:
            return
        self.price_cny = guard_cny(price_from_items(self.items) + (self.postage_cny or 0))


class StagingItem(SQLModel, table=True):
    """暂存订单的物品行（一单多物），结构对齐 OrderItem（含单价 price_cny / auto）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    staging_id: int = Field(foreign_key="orderstaging.id", index=True)
    name: str = Field(max_length=255)
    quantity: int = Field(default=1)
    unit_price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)  # **单价**（元）；与订单的 price_cny(总价) 区分
    auto: bool = Field(default=False)                       # True=系统自动生成/自动定价（前端灰显）

    staging: Optional[OrderStaging] = Relationship(back_populates="items")
