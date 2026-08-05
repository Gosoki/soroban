"""Request/response schemas. Keep money as Decimal/int (P1). Validate fx range (P6)."""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from pydantic import field_validator, model_validator
from sqlmodel import Field, SQLModel

from .config import FX_MAX, FX_MIN, FX_QUANTUM
from .models import ShipmentStatus, Source, StagingStatus, OrderStatus

_FX_Q = FX_QUANTUM           # 汇率量化到 4 位（唯一真相见 config.FX_QUANTUM）
_CNY_Q = Decimal("0.01")     # 人民币量化到分
_CNY_MAX = Decimal("9999999999.99")   # Numeric(12,2) 上限：防 DB 溢出 + 防 quantize 越精度抛 InvalidOperation
_JPY_MAX = 2_147_483_647              # 有符号 INT 上限（MySQL）：防溢出报 500


def _q_money(v: Optional[Decimal], label: str = "金额") -> Optional[Decimal]:
    """人民币金额：量化到分 + 非负 + 有限性/上限校验。
    先卡有限性与量级再 quantize——否则超大/NaN 输入会让 Decimal.quantize 抛 InvalidOperation
    （ArithmeticError，非 ValueError），Pydantic 不转 422 → 直接 500。"""
    if v is None:
        return None
    v = Decimal(v)
    if not v.is_finite() or abs(v) > _CNY_MAX:
        raise ValueError(f"{label}数值超出可接受范围（上限 {_CNY_MAX}）")
    v = v.quantize(_CNY_Q, rounding=ROUND_HALF_UP)
    if v < 0:
        raise ValueError(f"{label}不能为负数（退款/取消请用状态标记，自动不计入合计）")
    return v


def _q_fx(v: Optional[Decimal]) -> Optional[Decimal]:
    """汇率：有限性 + 合理区间 + 量化到 4 位。越界或非有限一律 422。"""
    if v is None:
        return None
    v = Decimal(v)
    if not v.is_finite() or abs(v) > FX_MAX:
        raise ValueError(f"汇率 {v} 不在合理区间 [{FX_MIN}, {FX_MAX}]（1元≈20円）")
    v = v.quantize(_FX_Q, rounding=ROUND_HALF_UP)
    if not (FX_MIN <= v <= FX_MAX):
        raise ValueError(f"汇率 {v} 不在合理区间 [{FX_MIN}, {FX_MAX}]（1元≈20円）")
    return v


def _bounded_jpy(v: Optional[int], label: str = "金额") -> Optional[int]:
    """直填日元(int)：非负 + 上限（防有符号 INT 溢出，MySQL 会报 Out of range → 500）。"""
    if v is None:
        return None
    if v < 0:
        raise ValueError(f"{label}不能为负数（退款/取消请用状态标记）")
    if v > _JPY_MAX:
        raise ValueError(f"{label}过大（上限 {_JPY_MAX}）")
    return v


_STAGING_STATUS = {s.value for s in StagingStatus}
_SOURCES = {s.value for s in Source}
_ORDER_STATUS = {s.value for s in OrderStatus}
_SHIPMENT_STATUS = {s.value for s in ShipmentStatus}


# --- 通用金额输入校验 mixin ---------------------------------------------------

class MoneyIn(SQLModel):
    price_cny: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    jpy_override: Optional[int] = None
    override_note: Optional[str] = None

    @field_validator("price_cny")
    @classmethod
    def _q_cny(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "金额")

    @field_validator("jpy_override")
    @classmethod
    def _nonneg_override(cls, v: Optional[int]) -> Optional[int]:
        return _bounded_jpy(v, "覆盖金额")

    @field_validator("fx_rate")
    @classmethod
    def _fx_range(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_fx(v)


class MoneyOut(SQLModel):
    price_cny: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    jpy_override: Optional[int] = None
    override_note: Optional[str] = None
    jpy_auto: Optional[int] = None
    jpy_settled: Optional[int] = None


class PostageIn(SQLModel):
    """邮费输入（淘宝订单/暂存共用）。空=包邮(0)；订单价 = Σ(单价×数量) + 邮费。可编辑（非派生）。"""
    postage_cny: Optional[Decimal] = None

    @field_validator("postage_cny")
    @classmethod
    def _q_postage(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "邮费")


def _check(value: str, allowed: set[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"非法{label}: {value!r}，允许值 {sorted(allowed)}")
    return value


# --- 认证 -------------------------------------------------------------------

class LoginRequest(SQLModel):
    username: str
    password: str


class ChangePassword(SQLModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _pw_max_bytes(cls, v: str) -> str:
        # bcrypt 只取前 72 字节，超出部分被静默忽略——设密码时就挡住，避免「只有前 72 字节生效」
        # 的意外（按字节判：一个汉字 3 字节，故不能用 max_length 字符数）。登录侧不加此限，交给校验失败。
        if len(v.encode("utf-8")) > 72:
            raise ValueError("新密码过长（上限 72 字节，约 24 个汉字或 72 个英文字符）")
        return v


class UserRead(SQLModel):
    id: int
    username: str
    display_name: Optional[str] = None


class LoginResponse(SQLModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


# --- 淘宝订单 ---------------------------------------------------------------

class ItemInBase(SQLModel):
    """物品行输入（订单/暂存共用）。price_cny=单价（元）；订单价由 Σ(单价×数量) 派生。
    auto 由客户端回传：未改动的「系统自动」项保持 True（前端灰显），用户一编辑即传 False。"""
    name: str
    quantity: int = Field(default=1, ge=1, le=1_000_000)   # ≥1 防负/零算出负订单价；≤1e6 防离谱数量把总价撑爆列上限
    price_cny: Optional[Decimal] = None
    auto: bool = False

    @field_validator("price_cny")
    @classmethod
    def _q_item_price(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "物品单价")


class OrderItemIn(ItemInBase):
    pass


class OrderItemRead(SQLModel):
    id: int
    name: str
    quantity: int
    price_cny: Optional[Decimal] = None
    auto: bool = False


class ItemListRead(SQLModel):
    """物品列表页：一行=一个 OrderItem + 其父订单只读上下文。amount_cny=单价×数量。"""
    id: int
    name: str
    quantity: int
    price_cny: Optional[Decimal] = None
    amount_cny: Optional[Decimal] = None
    auto: bool = False
    order_id: int
    date: dt.date
    order_no: Optional[str] = None
    shop: Optional[str] = None
    platform_account: Optional[str] = None
    platform: Optional[str] = None
    status: str
    express_no: Optional[str] = None


class OrderBase(MoneyIn, PostageIn):
    date: dt.date
    order_no: Optional[str] = None
    shop: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    status: str = OrderStatus.paid.value
    platform: Optional[str] = None
    express_no: Optional[str] = None
    express_company: Optional[str] = None
    platform_account: Optional[str] = None
    shipment_order_id: Optional[int] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _check(v, _ORDER_STATUS, "淘宝状态")


def _check_postage_within_total(price_cny, postage_cny, items) -> None:
    """纯种子价（无物品明细）时：邮费是订单总价的一部分，不能超过总价。否则货款被夹到 0、
    总价被悄悄抬成 = 邮费，与用户填的总价不符 → 明确 422 拒绝（有物品明细时价格另算，不检查）。"""
    if not items and price_cny is not None and postage_cny is not None and postage_cny > price_cny:
        raise ValueError("邮费不能大于订单总价（订单价 = 商品单价×数量 + 邮费）")


class OrderCreate(OrderBase):
    items: list[OrderItemIn] = []

    @model_validator(mode="after")
    def _postage_le_total(self):
        _check_postage_within_total(self.price_cny, self.postage_cny, self.items)
        return self


class OrderUpdate(MoneyIn, PostageIn):
    version: int                                   # 乐观锁必填
    date: Optional[dt.date] = None
    order_no: Optional[str] = None
    shop: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    platform: Optional[str] = None
    express_no: Optional[str] = None
    express_company: Optional[str] = None
    platform_account: Optional[str] = None
    shipment_order_id: Optional[int] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    items: Optional[list[OrderItemIn]] = None      # 给了就整体替换

    @field_validator("status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _ORDER_STATUS, "淘宝状态")


class OrderRead(MoneyOut):
    id: int
    date: dt.date
    postage_cny: Optional[Decimal] = None
    order_no: Optional[str] = None
    shop: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    status: str
    platform: Optional[str] = None
    express_no: Optional[str] = None
    express_company: Optional[str] = None
    platform_account: Optional[str] = None
    shipment_order_id: Optional[int] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    source: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    items: list[OrderItemRead] = []


# --- 集运订单 ---------------------------------------------------------------

class ShipmentBase(MoneyIn):
    date: dt.date
    shipment_no: Optional[str] = None
    weight: Optional[Decimal] = None
    intl_tracking_no: Optional[str] = None
    status: str = ShipmentStatus.packing.value
    special_fee_jpy: Optional[int] = None
    recipient: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _check(v, _SHIPMENT_STATUS, "集运状态")

    @field_validator("special_fee_jpy")
    @classmethod
    def _nonneg_fee(cls, v: Optional[int]) -> Optional[int]:
        return _bounded_jpy(v, "特殊费")


class ShipmentCreate(ShipmentBase):
    pass


class ShipmentUpdate(MoneyIn):
    version: int
    date: Optional[dt.date] = None
    shipment_no: Optional[str] = None
    weight: Optional[Decimal] = None
    intl_tracking_no: Optional[str] = None
    status: Optional[str] = None
    special_fee_jpy: Optional[int] = None
    recipient: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _SHIPMENT_STATUS, "集运状态")

    @field_validator("special_fee_jpy")
    @classmethod
    def _nonneg_fee(cls, v: Optional[int]) -> Optional[int]:
        return _bounded_jpy(v, "特殊费")


class OrderBrief(SQLModel):
    id: int
    order_no: Optional[str] = None
    date: dt.date
    shop: Optional[str] = None
    status: str
    jpy_settled: Optional[int] = None
    items: list[OrderItemRead] = []


class ShipmentRead(MoneyOut):
    id: int
    date: dt.date
    shipment_no: Optional[str] = None
    weight: Optional[Decimal] = None
    intl_tracking_no: Optional[str] = None
    status: str
    special_fee_jpy: Optional[int] = None
    recipient: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    source: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    orders: list[OrderBrief] = []


class ShipmentOcrAttachResult(SQLModel):
    """「内含快递」截图识别 + 联动挂靠的结果。分三类回报，前端据此拼提示。"""
    shipment: ShipmentRead
    attached: list[OrderBrief] = []      # 本次挂上（或已在本单）的商品订单
    skipped: list[OrderBrief] = []       # 已挂在别的集运单 → 跳过不强改
    unmatched: list[str] = []            # 截图里有、但商品订单里找不到的快递号
    express_nos: list[str] = []          # 截图识别出的全部快递号（供人工核对）


# --- 杂项 -------------------------------------------------------------------

class MiscBase(MoneyIn):
    date: dt.date
    name: str
    category: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None


class MiscCreate(MiscBase):
    pass


class MiscUpdate(MoneyIn):
    version: int
    date: Optional[dt.date] = None
    name: Optional[str] = None
    category: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None


class MiscRead(MoneyOut):
    id: int
    date: dt.date
    name: str
    category: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    source: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


# --- 看板 -------------------------------------------------------------------

class MonthTotal(SQLModel):
    month: str          # YYYY-MM
    jpy: int            # 当月合计（结算日元）
    order_jpy: int = 0
    shipment_jpy: int = 0
    misc_jpy: int = 0
    order_count: int = 0
    shipment_count: int = 0
    misc_count: int = 0


class DashboardRead(SQLModel):
    total_jpy: int
    order_jpy: int
    shipment_jpy: int
    misc_jpy: int
    order_count: int
    shipment_count: int
    misc_count: int
    by_month: list[MonthTotal] = []
    fx_rate: Optional[Decimal] = None       # 当前 CNY→JPY（兜底值）


# --- 汇率 -------------------------------------------------------------------

class FxRead(SQLModel):
    base: str = "CNY"
    quote: str = "JPY"
    rate: Optional[Decimal] = None
    date: Optional[dt.date] = None
    stale: bool = False                     # True = 用的是历史兜底值


# --- 淘宝抓取暂存（全部淘宝订单 → 确认导入）---------------------------------

class StagingItemIn(ItemInBase):
    pass


class StagingItemRead(SQLModel):
    id: int
    name: str
    quantity: int
    price_cny: Optional[Decimal] = None
    auto: bool = False


class StagingBase(PostageIn):
    order_no: Optional[str] = None
    platform_account: Optional[str] = None
    platform: Optional[str] = None           # 来源平台（淘宝/闲鱼/京东）；导入时随单迁移到账本
    shop: Optional[str] = None
    price_cny: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    order_date: Optional[dt.date] = None
    express_no: Optional[str] = None
    order_status: Optional[str] = None       # 淘宝订单真实状态（已付/已发/…），导入后与账本联动

    @field_validator("price_cny")
    @classmethod
    def _q_cny(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "金额")

    @field_validator("fx_rate")
    @classmethod
    def _fx_range(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_fx(v)

    @field_validator("order_status")
    @classmethod
    def _order_status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _ORDER_STATUS, "订单状态")


class StagingCreate(StagingBase):
    status: str = StagingStatus.pending.value
    items: list[StagingItemIn] = []

    @model_validator(mode="after")
    def _postage_le_total(self):
        _check_postage_within_total(self.price_cny, self.postage_cny, self.items)
        return self


class StagingUpdate(StagingBase):
    version: int                                       # 乐观锁必填
    status: Optional[str] = None
    items: Optional[list[StagingItemIn]] = None       # 给了就整体替换

    @field_validator("status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _STAGING_STATUS, "暂存状态")


class StagingRead(StagingBase):
    id: int
    version: int
    status: str
    imported_order_id: Optional[int] = None
    scraped_at: dt.datetime
    items: list[StagingItemRead] = []


# --- 列布局 -----------------------------------------------------------------

class LayoutColumn(SQLModel):
    key: str
    width: int


class LayoutRead(SQLModel):
    table_name: str
    columns: list[LayoutColumn] = []


class LayoutUpdate(SQLModel):
    columns: list[LayoutColumn]


# --- 标签选项（列头可管理的下拉集）------------------------------------------

class TagIn(SQLModel):
    value: str = Field(max_length=128)   # 与 TagOption.value 列一致，防超长在 MySQL 报 500、SQLite 静默存


# --- 爬虫插件配置 -----------------------------------------------------------

class PluginConfigIn(SQLModel):
    enabled: bool = False
    params: dict = {}                 # 用户填的参数（如 {"accounts": "acctA,acctB"}）
    schedule_minutes: int = 0         # 定时抓取间隔（分钟），0=不定时


class TagOut(SQLModel):
    value: str
    color: int                      # 调色盘序号（0..N-1），前端映射到 TAG_PALETTE
    in_use: bool = False            # 是否被数据（订单/暂存/集运）使用中——使用中不可删除
