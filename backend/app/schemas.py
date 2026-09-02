"""Request/response schemas. Keep money as Decimal/int (P1). Validate fx range (P6)."""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from pydantic import ConfigDict, field_validator, model_validator
from sqlmodel import Field, SQLModel

from .config import FX_MAX, FX_MIN, FX_QUANTUM
from .models import (
    ImportStatus,
    MiscExpense,
    Order,
    OrderItem,
    OrderStaging,
    PurchaseStatus,
    ShipmentOrder,
    ShipmentStatus,
    TagOption,
)

_FX_Q = FX_QUANTUM           # 汇率量化到 4 位（唯一真相见 config.FX_QUANTUM）
_CNY_Q = Decimal("0.01")     # 人民币量化到分
_CNY_MAX = Decimal("9999999999.99")   # Numeric(12,2) 上限：防 DB 溢出 + 防 quantize 越精度抛 InvalidOperation
_JPY_MAX = 2_147_483_647              # 有符号 INT 上限（MySQL）：防溢出报 500


def _len(model, column: str) -> int:
    """该列建表时声明的 VARCHAR 长度。**以模型为唯一真相**，改列长不必再改校验。

    为什么必须校验：所有 str 列都是定长 VARCHAR。超长时 SQLite 静默照存、MySQL 直接
    「Data too long」→ OperationalError → 500。不卡这一刀就是双引擎发散（与 TagIn 已有的
    max_length 同一个理由，这里把它补齐到全部输入列）。"""
    return model.__table__.columns[column].type.length


# 显式传 null 清空 NOT NULL 列的中文列名。key 是列名，值是用户在界面上看到的叫法。
# 只用于错误文案；缺项时回落到列名本身，不影响校验是否生效。
_REQUIRED_LABELS = {
    "date": "日期",
    "name": "名称",
    "purchase_status": "交易状态",
    "shipment_status": "集运状态",
    "order_date": "下单日期",
}


def _reject_null_on_required(model, *fields: str):
    """生成一个 model_validator：显式传 `null` 清空 NOT NULL 列时 422，而不是让它撞到数据库。

    为什么需要：写模型用 `Optional[...] = None` + `exclude_unset` 来区分「没传」和「传了 null」，
    但「传了 null」这条路此前没有任何校验——值原样 setattr 到 NOT NULL 列，直到 commit 才被
    数据库拦下，经全局 IntegrityError 处理器变成 409「数据完整性冲突」。而前端把 409 当乐观锁
    冲突处理，弹「数据已变，已刷新」并整表重拉：用户既没看懂错在哪，也不知道自己那一步本就不允许。
    （最容易撞上的是日期格——el-date-picker 默认 clearable，点 ✕ 就发 date=null。）

    **可空性从 `Model.__table__` 反查，不手抄清单**：本仓有前科——`_overlay` 手抄的共享字段
    清单与 `_SHARED_TO_ORDER` 漂掉过一个 `platform`。列改名或改可空性时，这里自动跟着走。
    """
    def _check_required(self):
        bad = []
        for f in fields:
            if f in self.model_fields_set and getattr(self, f, None) is None:
                col = model.__table__.columns.get(f)
                if col is not None and not col.nullable:
                    bad.append(_REQUIRED_LABELS.get(f, f))
        if bad:
            raise ValueError("「" + "」「".join(bad) + "」不能清空")
        return self

    return model_validator(mode="after")(_check_required)


def _clip(v: Optional[str], limit: int) -> Optional[str]:
    """短文本列：超长**截断**而不是拒绝。

    只用于「物品名 / 杂项名」这类**显示标签**：255 字远超实际所需，超长多半是误粘贴，
    截尾比 422 打断整批同步划算，且与 routers/common.build_items 里既有的 [:255] 同口径。
    真正会天然超长的商品标题（`title`）已经放宽成 TEXT 列、不再截断（见迁移 d0e1f2a3b4c5）。
    标识类列（订单号/快递号/账号…）不走这里——那种超长是脏输入，宁可 422。"""
    return v if v is None else v[:limit]


def norm_code(v: Optional[str]) -> Optional[str]:
    """单号类列归一：去首尾空格 + 转大写；空串归 NULL。

    为什么必须归一：这些值是**匹配键**（集运「内含快递」截图靠 express_no 精确匹配商品订单）。
    OCR 提取时就 `.upper()` 了，用户手输可能小写或带粘贴带来的首尾空格；而字符串精确比较在
    **SQLite 上区分大小写、MySQL 默认不区分**——不归一就是「同一份数据两种后端行为不同」。
    历史数据由迁移 d0e1f2a3b4c5 一并补齐。"""
    if v is None:
        return None
    return v.strip().upper() or None


def norm_id(v: Optional[str]) -> Optional[str]:
    """有唯一约束的单号（订单号/集运单号）：**只**去首尾空格，不改大小写。

    不转大写是因为这两列上有唯一索引：批量改写历史数据可能撞约束、让升级直接失败；
    而它们实际都是纯数字或「数字-数字」，大小写本就不构成问题。去空格则纯赚——
    粘贴带来的首尾空格会让唯一约束形同虚设（" 123" 与 "123" 被当成两张单）。"""
    if v is None:
        return None
    return v.strip() or None


def _q_decimal(v: Optional[Decimal], max_value: Decimal, quantum: Decimal,
               label: str) -> Optional[Decimal]:
    """定点小数列的通用入口：非有限/超上限 → 422，否则量化到该列精度，最后拒负数。

    顺序很讲究：**先**卡有限性与量级、**再** quantize——否则超大/NaN 输入会让
    Decimal.quantize 抛 InvalidOperation（ArithmeticError，不是 ValueError），
    Pydantic 不会转 422 → 直接 500。
    上限对应 DB 里的 Numeric(p,s)：超了 MySQL 报 Out of range → 500，SQLite 静默存 → 双引擎发散。"""
    if v is None:
        return None
    v = Decimal(v)
    # `copy_abs()` 而不是 `abs()`：后者是走 context 的算术运算，指数超过 Emax（默认 999999）
    # 抛 `decimal.Overflow`——它继承 ArithmeticError **不是** ValueError，
    # 于是 pydantic 不转 422、main.py 的 ValueError 兜底也接不住，一路裸 500。
    # 而这一行本身就是那道「防止极端量级」的闸。copy_abs 是拷贝操作，永不抛。
    if not v.is_finite() or v.copy_abs() > max_value:
        raise ValueError(f"{label}数值超出可接受范围（上限 {max_value}）")
    v = v.quantize(quantum, rounding=ROUND_HALF_UP)
    if v < 0:
        raise ValueError(f"{label}不能为负数（退款/取消请用状态标记，自动不计入合计）")
    return v


def _q_money(v: Optional[Decimal], label: str = "金额") -> Optional[Decimal]:
    """人民币金额：量化到分 + 非负 + 有限性/上限校验（Numeric(12,2)）。"""
    return _q_decimal(v, _CNY_MAX, _CNY_Q, label)


def _q_fx(v: Optional[Decimal]) -> Optional[Decimal]:
    """汇率：有限性 + 合理区间 + 量化到 4 位。越界或非有限一律 422。"""
    if v is None:
        return None
    v = Decimal(v)
    if not v.is_finite() or v.copy_abs() > FX_MAX:      # copy_abs 理由同上
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


_WEIGHT_MAX = Decimal("999999.99")    # ShipmentOrder.weight = Numeric(8,2) 的上限

_IMPORT_STATUS = {s.value for s in ImportStatus}
_PURCHASE_STATUS = {s.value for s in PurchaseStatus}
_SHIPMENT_STATUS = {s.value for s in ShipmentStatus}


# --- 写模型一律拒绝未知字段 -------------------------------------------------
#
# 默认的 `extra="ignore"` 在本项目是个**静默失败**装置：客户端把字段名写错（改名漏改、
# 前端和后端不同步、爬虫发旧名），pydantic 会默默丢掉它 → `model_dump(exclude_unset=True)`
# 得到空 dict → setattr 循环空转 → 而 `guarded_bump` 已经把 version 自增了。
# 结果是「200 OK + 什么都没改 + 零日志」，还会把别的客户端顶成 409。
#
# 已核实所有写入方发的都是窄 body（前端表格 `{version, [col.key]: v}`、编辑面板同款、
# 新建走列配置、爬虫走 _PUSH_FIELDS），forbid 是安全的。
# 这一条对**未来**任何错名同样生效（比如把 sale_status 发到 /api/orders），不会过期。
_FORBID = ConfigDict(extra="forbid")


# --- 通用金额输入校验 mixin ---------------------------------------------------

class MoneyIn(SQLModel):
    price_cny: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    jpy_override: Optional[int] = None
    # override_note 来自 LedgerBase，三张账本表列长相同；LedgerBase 不是表、没有 __table__，
    # 所以拿 Order 当代表取长度。
    override_note: Optional[str] = Field(default=None, max_length=_len(Order, "override_note"))

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
    """物品行输入（订单/暂存共用）。**unit_price_cny=单价**（元）；订单价由 Σ(单价×数量) 派生。
    auto 由客户端回传：未改动的「系统自动」项保持 True（前端灰显），用户一编辑即传 False。

    forbid 不能只加在顶层写模型上：`items[]` 是整个请求体里唯一装钱的地方，单价键名写错
    （比如沿用旧名 `price_cny`）会被 pydantic 静默丢弃 → build_items 记 0.00 + auto=True
    → 订单价派生成 ¥0.00，接口还返回 200。同一个错名放顶层是 422，放这里却是静默丢钱。"""
    model_config = _FORBID

    name: str
    quantity: int = Field(default=1, ge=1, le=1_000_000)   # ≥1 防负/零算出负订单价；≤1e6 防离谱数量把总价撑爆列上限
    unit_price_cny: Optional[Decimal] = None               # **单价**；订单的 price_cny 是总价
    auto: bool = False

    @field_validator("unit_price_cny")
    @classmethod
    def _q_item_price(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "物品单价")

    @field_validator("name")
    @classmethod
    def _clip_name(cls, v: str) -> str:
        return _clip(v, _len(OrderItem, "name"))


class OrderItemIn(ItemInBase):
    pass


class OrderItemRead(SQLModel):
    id: int
    name: str
    quantity: int
    unit_price_cny: Optional[Decimal] = None
    auto: bool = False


class ItemListRead(SQLModel):
    """物品列表页：一行=一个 OrderItem + 其父订单只读上下文。amount_cny=单价×数量。"""
    id: int
    name: str
    quantity: int
    unit_price_cny: Optional[Decimal] = None   # 单价
    amount_cny: Optional[Decimal] = None       # 单价 × 数量
    auto: bool = False
    order_id: int
    date: dt.date
    order_no: Optional[str] = None
    title: Optional[str] = None
    platform_account: Optional[str] = None
    platform: Optional[str] = None
    purchase_status: str                                # 订单自己的**国内段**状态
    # 界面该显示的状态：订单挂了集运单就跟随那张单，否则等于 purchase_status。
    # 与商品订单页同一口径（见 models/order/order.py 的 Order.fulfillment_status）。
    fulfillment_status: str = ""
    shipment_order_id: Optional[int] = None    # 有值 = 已挂靠 → 该格锁定
    express_no: Optional[str] = None


class OrderFieldsIn(SQLModel):
    """订单可写字段的长度/归一约束（Create/Update 共用；两边字段一致，别再各写一份而漂移）。
    标识类列超长 → 422；单号类列写入即归一（见 norm_code / norm_id）；
    商品标题 title 是 Text 列，不限长也不截断。"""
    order_no: Optional[str] = Field(default=None, max_length=_len(Order, "order_no"))
    title: Optional[str] = None                     # Text 列，无长度上限
    url: Optional[str] = None                      # Text 列，无长度上限
    category: Optional[str] = Field(default=None, max_length=_len(Order, "category"))
    platform: Optional[str] = Field(default=None, max_length=_len(Order, "platform"))
    express_no: Optional[str] = Field(default=None, max_length=_len(Order, "express_no"))
    express_company: Optional[str] = Field(default=None, max_length=_len(Order, "express_company"))
    platform_account: Optional[str] = Field(default=None, max_length=_len(Order, "platform_account"))
    shipment_order_id: Optional[int] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None                     # Text 列，无长度上限

    @field_validator("express_no")
    @classmethod
    def _norm_express(cls, v: Optional[str]) -> Optional[str]:
        return norm_code(v)

    @field_validator("order_no")
    @classmethod
    def _norm_order_no(cls, v: Optional[str]) -> Optional[str]:
        return norm_id(v)


class OrderBase(MoneyIn, PostageIn, OrderFieldsIn):
    date: dt.date
    purchase_status: str = PurchaseStatus.paid.value

    @field_validator("purchase_status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _check(v, _PURCHASE_STATUS, "淘宝状态")


def _check_postage_within_total(price_cny, postage_cny, items) -> None:
    """**种子价路径**上：邮费是订单总价的一部分，不能超过总价。否则货款被夹到 0、
    总价被悄悄抬成 = 邮费，与用户填的总价不符 → 明确 422 拒绝。

    判据是「有没有物品**带单价**」，不是「有没有物品」。`build_items` 只在
    `not any_priced` 时才拿 price_cny 当种子（见其 any_priced 分支），也就是说
    「传了 3 条没单价的物品」和「一条物品都没传」走的是**同一条**种子路径、
    有**同样**的夹零后果。原先只判 `not items`，于是同一个非法输入
    （邮费 > 总价）在前者是 422、在后者是 200 + 静默把总价改成邮费。
    物品自带单价时 price_cny 根本不参与计算（订单价由物品派生），无从判起，也就不检查。"""
    seeded = not any(getattr(it, "unit_price_cny", None) is not None for it in items)
    if seeded and price_cny is not None and postage_cny is not None and postage_cny > price_cny:
        raise ValueError("邮费不能大于订单总价（订单价 = 商品单价×数量 + 邮费）")


class OrderCreate(OrderBase):
    model_config = _FORBID
    items: list[OrderItemIn] = []

    @model_validator(mode="after")
    def _postage_le_total(self):
        _check_postage_within_total(self.price_cny, self.postage_cny, self.items)
        return self


class OrderUpdate(MoneyIn, PostageIn, OrderFieldsIn):
    model_config = _FORBID
    version: int                                   # 乐观锁必填
    date: Optional[dt.date] = None
    purchase_status: Optional[str] = None
    items: Optional[list[OrderItemIn]] = None      # 给了就整体替换

    _no_null_required = _reject_null_on_required(Order, "date", "purchase_status")

    @field_validator("purchase_status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _PURCHASE_STATUS, "淘宝状态")


class OrderRead(MoneyOut):
    id: int
    date: dt.date
    postage_cny: Optional[Decimal] = None
    order_no: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    purchase_status: str                     # 订单自己的**国内段**状态（挂靠期间也原样保留）
    # 界面该显示的状态：挂着集运单就是那张单的状态，否则等于 purchase_status。
    # 由后端算而不是前端拼：筛选、排序、导出都要用同一个口径，算两遍必然对不上。
    fulfillment_status: str
    platform: Optional[str] = None
    express_no: Optional[str] = None
    express_company: Optional[str] = None
    platform_account: Optional[str] = None
    shipment_order_id: Optional[int] = None
    # 挂靠集运单的单号。下拉选项有条数上限，显示不能靠「下拉里恰好有这张」——
    # 显示的真相在订单行上，下拉只负责挑选。与 fulfillment_status 同一条预加载，不多查一次。
    shipment_no: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    created_via: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    items: list[OrderItemRead] = []


# --- 集运订单 ---------------------------------------------------------------

class ShipmentFieldsIn(SQLModel):
    """集运可写字段的长度/取值约束（Create/Update 共用）。"""
    shipment_no: Optional[str] = Field(default=None, max_length=_len(ShipmentOrder, "shipment_no"))
    weight: Optional[Decimal] = None
    intl_tracking_no: Optional[str] = Field(
        default=None, max_length=_len(ShipmentOrder, "intl_tracking_no"))
    special_fee_jpy: Optional[int] = None
    recipient: Optional[str] = Field(default=None, max_length=_len(ShipmentOrder, "recipient"))
    payer_id: Optional[int] = None
    note: Optional[str] = None                     # Text 列，无长度上限

    @field_validator("special_fee_jpy")
    @classmethod
    def _nonneg_fee(cls, v: Optional[int]) -> Optional[int]:
        return _bounded_jpy(v, "特殊费")

    @field_validator("weight")
    @classmethod
    def _q_weight(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        # weight 是 Numeric(8,2)：不卡上限的话 MySQL 会 Out of range → 500，SQLite 静默存
        return _q_decimal(v, _WEIGHT_MAX, Decimal("0.01"), "重量")

    @field_validator("intl_tracking_no")
    @classmethod
    def _norm_intl(cls, v: Optional[str]) -> Optional[str]:
        return norm_code(v)

    @field_validator("shipment_no")
    @classmethod
    def _norm_shipment_no(cls, v: Optional[str]) -> Optional[str]:
        return norm_id(v)


class ShipmentBase(MoneyIn, ShipmentFieldsIn):
    date: dt.date
    shipment_status: str = ShipmentStatus.packing.value

    @field_validator("shipment_status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _check(v, _SHIPMENT_STATUS, "集运状态")


class ShipmentCreate(ShipmentBase):
    model_config = _FORBID
    pass


class ShipmentUpdate(MoneyIn, ShipmentFieldsIn):
    model_config = _FORBID
    version: int
    date: Optional[dt.date] = None
    shipment_status: Optional[str] = None

    _no_null_required = _reject_null_on_required(ShipmentOrder, "date", "shipment_status")

    @field_validator("shipment_status")
    @classmethod
    def _status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _SHIPMENT_STATUS, "集运状态")


class OrderBrief(SQLModel):
    id: int
    order_no: Optional[str] = None
    date: dt.date
    title: Optional[str] = None
    purchase_status: str    # 订单自己的采购段状态（本对象是**订单**摘要，不是集运单）
    jpy_settled: Optional[int] = None
    items: list[OrderItemRead] = []
    # 这一单算不算进集运单的货款合计（`ShipmentRead.orders_jpy`）。
    # 由**后端**按 `Order.ledger_exclusions()` 判定并发出来——前端不许自己抄一份
    # 「哪些状态不计入」的清单：抄了就是两份，迟早对不上，而对账单是要发给别人的。
    counted: bool = True


class ShipmentRead(MoneyOut):
    id: int
    date: dt.date
    shipment_no: Optional[str] = None
    weight: Optional[Decimal] = None
    intl_tracking_no: Optional[str] = None
    shipment_status: str
    special_fee_jpy: Optional[int] = None
    recipient: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    created_via: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime
    orders: list[OrderBrief] = []

    # --- 到岸成本（派生，**不是本表的列**）------------------------------------
    # 一张集运单真正花了多少钱 = 里面那些商品单的货款 + 这张单自己的国际运费。
    # 三个都可能是 None，含义是「这次没算」（`brief=True` 不展开子订单），
    # 与「算出来是 0」是两回事——0 会被读成「这单不要钱」。
    orders_jpy: Optional[int] = None      # 子订单货款合计（已排除退款/关闭等不计入的单）
    landed_jpy: Optional[int] = None      # 到岸合计 = orders_jpy + 本单 jpy_settled
    unconverted: Optional[int] = None     # 有钱、却缺汇率没折算成日元的行数（含本单自己）


class ShipmentOcrAttachResult(SQLModel):
    """「内含快递」截图识别 + 联动挂靠的结果。分三类回报，前端据此拼提示。"""
    shipment: ShipmentRead
    attached: list[OrderBrief] = []      # 本次挂上（或已在本单）的商品订单
    skipped: list[OrderBrief] = []       # 已挂在别的集运单 → 跳过不强改
    unmatched: list[str] = []            # 截图里有、但商品订单里找不到的快递号
    express_nos: list[str] = []          # 截图识别出的全部快递号（供人工核对）
    # 截图里看得见「快递单号」这个标签、却没能取出号的行数（断行、糊字、与上一行重号）。
    # 没有它的时候「少挂了一单」这件事**在响应里完全不存在**：前端只看到 attached 的数量，
    # 于是照样弹绿色成功提示。三类回报之外必须有第四类——「我知道我漏了，但不知道漏了什么」。
    unreadable: int = 0


# --- 杂项 -------------------------------------------------------------------

class MiscFieldsIn(SQLModel):
    """杂项可写字段的长度约束（Create/Update 共用）。name 是自由文本 → 截断，见 _clip。"""
    category: Optional[str] = Field(default=None, max_length=_len(MiscExpense, "category"))
    payer_id: Optional[int] = None
    note: Optional[str] = None                     # Text 列，无长度上限

    @field_validator("name", check_fields=False)
    @classmethod
    def _clip_name(cls, v: Optional[str]) -> Optional[str]:
        return _clip(v, _len(MiscExpense, "name"))


class MiscBase(MoneyIn, MiscFieldsIn):
    date: dt.date
    name: str


class MiscCreate(MiscBase):
    model_config = _FORBID
    pass


class MiscUpdate(MoneyIn, MiscFieldsIn):
    model_config = _FORBID
    version: int
    date: Optional[dt.date] = None
    name: Optional[str] = None

    _no_null_required = _reject_null_on_required(MiscExpense, "date", "name")


class MiscRead(MoneyOut):
    id: int
    date: dt.date
    name: str
    category: Optional[str] = None
    payer_id: Optional[int] = None
    note: Optional[str] = None
    created_via: str
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
    # 有货款、却因为缺汇率算不出日元的行。SUM(jpy_settled) 会把 NULL 直接跳过，
    # 于是这些行**金额被吞、笔数照数**——合计变小而单数不变，看板上没有任何异常信号。
    # 把它单列出来，让「被吞掉」变成看得见的东西。0 = 没有这种行。
    uncounted_count: int = 0
    uncounted_cny: Decimal = Decimal("0")


# --- 汇率 -------------------------------------------------------------------

class FxRead(SQLModel):
    base: str = "CNY"
    quote: str = "JPY"
    rate: Optional[Decimal] = None
    date: Optional[dt.date] = None
    # 「这条汇率不是今天的」——**日粒度**。刻意不叫 stale：
    # `is_expired()`（超过 fx.stale_hours）在英文里是同义词，在这里却是另一回事，
    # 而两者会同时出现在同一个响应里，字段名撞义就只能靠猜。
    # 名字撞义的代价是读代码的人得每次回来确认一遍哪个是哪个。
    not_today: bool = False
    source: str = ""   # 源标识由插件自定，核心只存不解释；"manual"=用户手填
    source_label: str = ""                  # 上面那个的中文名，供前端提示显示
    # not_today 是**日粒度**（不是今天的），1 天前和 3 个月前长得一样；
    # 下面两个才说得清「还能不能信」。
    # 这条汇率有多旧——按它**是哪一天的**算，不是按什么时候取到的。
    # （按后者算的话，补填一条历史汇率会把过期告警静默关掉；见 services/fx.rate_age_hours）
    age_hours: Optional[float] = None
    expired: bool = False                   # 超过 fx.stale_hours → 界面明确标出、建单时记警告
    # 现在有没有插件能自动提供汇率（名字，没有则空）。界面据此说实话：
    # 插件被删掉之后还写着「自动获取由插件负责」，那句话就是假的——点过去什么都没有。
    auto_provider: str = ""      # 真的会自动更新时，写供给它的插件名
    # 装了汇率插件、但它跑不起来（停用 / 没授权 / 缺环境）时的原因。
    # 三态是必要的：「由 X 负责」和「没有插件」之间还有「装了但不会跑」，
    # 而后者原先被显示成前者——那句话是假的，且汇率停更时账本会继续用兜底值建单。
    auto_blocked: str = ""


# --- 淘宝抓取暂存（全部淘宝订单 → 确认导入）---------------------------------

class StagingItemIn(ItemInBase):
    pass


class StagingItemRead(SQLModel):
    id: int
    name: str
    quantity: int
    unit_price_cny: Optional[Decimal] = None
    auto: bool = False


class StagingBase(PostageIn):
    # 长度上限对齐 OrderStaging 建表定义（导入时这些值会原样搬进账本，两边列长一致）
    order_no: Optional[str] = Field(default=None, max_length=_len(OrderStaging, "order_no"))
    platform_account: Optional[str] = Field(
        default=None, max_length=_len(OrderStaging, "platform_account"))
    platform: Optional[str] = Field(default=None, max_length=_len(OrderStaging, "platform"))
    title: Optional[str] = None               # 商品标题，Text 列，无长度上限
    price_cny: Optional[Decimal] = None
    fx_rate: Optional[Decimal] = None
    order_date: Optional[dt.date] = None
    express_no: Optional[str] = Field(default=None, max_length=_len(OrderStaging, "express_no"))
    express_company: Optional[str] = Field(
        default=None, max_length=_len(OrderStaging, "express_company"))
    # 商品链接，Text 列，无长度上限——**不要**写 max_length=_len(...)：`_len` 取的是
    # `.type.length`，在 TEXT 上是 None，写了等于给 Field 传 max_length=None，白费还误导。
    url: Optional[str] = None
    purchase_status: Optional[str] = None    # 采购段交易状态（待发货/待收货/…），导入后与账本联动

    @field_validator("price_cny")
    @classmethod
    def _q_cny(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_money(v, "金额")

    @field_validator("fx_rate")
    @classmethod
    def _fx_range(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _q_fx(v)

    @field_validator("express_no")
    @classmethod
    def _norm_express(cls, v: Optional[str]) -> Optional[str]:
        return norm_code(v)

    @field_validator("order_no")
    @classmethod
    def _norm_order_no(cls, v: Optional[str]) -> Optional[str]:
        return norm_id(v)

    @field_validator("purchase_status")
    @classmethod
    def _purchase_status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _PURCHASE_STATUS, "订单状态")


class StagingCreate(StagingBase):
    model_config = _FORBID
    import_status: str = ImportStatus.pending.value
    items: list[StagingItemIn] = []

    @field_validator("import_status")
    @classmethod
    def _import_status(cls, v: str) -> str:
        """新建的暂存行**只能是「待导入」**。

        这个字段原先在 create 侧一点校验都没有（update 侧有白名单），三个后果：
          · 任意长字符串落进 VARCHAR(32) 列 —— SQLite 静默收下，MySQL 抛 1406 DataError，
            而 `main.py` 只挂了 IntegrityError/ValueError/OperationalError ⇒ **裸 500**；
          · 非枚举值原样入库并回读，还会被标签同步当成「在用」登记；
          · **最要紧的一条**：`{"import_status": "已导入"}` 能建出一条
            `import_status=已导入` 而 `imported_order_id=NULL` 的行 —— 按状态筛选时它算已导入
            （用户以为账已经记了），而 `POST /{id}/import` 照样能再导一次，同一笔货进账本两遍。
        而这正是 `staging.update_staging` 用十行注释堵死的那个洞 —— 只堵在了 PATCH 上。
        同一个前缀、同一份 `staging:write` 权限、同一个字段，POST 放行、PATCH 拒绝。

        口径与 PATCH 一致：导入状态只由「导入账本」/「忽略」/删除来推进。
        """
        v = _check(v, _IMPORT_STATUS, "导入状态")
        if v != ImportStatus.pending.value:
            raise ValueError(
                f"新建的暂存行只能是「{ImportStatus.pending.value}」："
                "导入状态请用「导入账本」或「忽略」来推进")
        return v

    @model_validator(mode="after")
    def _postage_le_total(self):
        _check_postage_within_total(self.price_cny, self.postage_cny, self.items)
        return self


class StagingUpdate(StagingBase):
    model_config = _FORBID
    version: int                                       # 乐观锁必填
    import_status: Optional[str] = None
    items: Optional[list[StagingItemIn]] = None       # 给了就整体替换

    @field_validator("import_status")
    @classmethod
    def _import_status(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _check(v, _IMPORT_STATUS, "导入状态")


class StagingRead(StagingBase):
    id: int
    version: int
    import_status: str
    imported_order_id: Optional[int] = None
    scraped_at: dt.datetime
    items: list[StagingItemRead] = []


# --- 列布局 -----------------------------------------------------------------

# 列布局落库时是 `json.dumps` 进 `ColumnLayout.columns_json`（**Text 列**）。
# `/api/layout` 曾是全仓唯一一条**无上限**写入 Text 列的路径：实测 3000 个列定义
# → 200 OK、落库 94890 字节。SQLite 静默收下，MySQL 的 TEXT 上限是 65535 **字节**
# → 1406 Data too long → `DataError`，而 `main.py` 只挂了 IntegrityError / ValueError /
# OperationalError ⇒ **裸 500**。同一份数据库导出，两个后端两种结局。
# 同样往 Text 列写 JSON 的 `ingest/kinds/plugin_record.py` 早就把上限钉成 65535 并
# 写了整段说明，这条推理没有被应用到这里。
#
# 三档边界都按「真实列数的量级」定，而不是按 Text 容量倒推——留足余量比卡死更好用：
#   · key   —— 列名，最长的现有列名不到 20 字符；
#   · width —— 像素宽，负数与荒谬的大数都不该落库（原先负数照单全收，见下方测试）；
#   · 条数  —— 最宽的表也就十几列，200 足够，且 200×(64+数字) 远小于 65535。
class LayoutColumn(SQLModel):
    model_config = _FORBID
    key: str = Field(max_length=64)
    width: int = Field(ge=0, le=4000)


class LayoutRead(SQLModel):
    table_name: str
    columns: list[LayoutColumn] = []


class LayoutUpdate(SQLModel):
    model_config = _FORBID
    columns: list[LayoutColumn] = Field(max_length=200)


# --- 标签选项（列头可管理的下拉集）------------------------------------------

class TagIn(SQLModel):
    value: str = Field(max_length=_len(TagOption, "value"))   # 长度以 TagOption.value 列为准


# --- 爬虫插件配置 -----------------------------------------------------------

class PluginConfigIn(SQLModel):
    # **`extra="forbid"`，且刻意不再有 `params`。**
    # 原先这里声明了 `params: dict = {}`（注释还写着该放什么），而 `save_config`
    # 从头到尾没读过它——全仓 `payload.params` 零次出现。真正的参数入口是另一个端点
    # `PUT /{plugin_id}/params`。于是任何带 `params` 的调用都是
    # 「200 OK + 什么都没改 + 零日志」，正是本仓头号敌人。
    # 删掉字段还不够（没有 forbid 时多余键照样被静默忽略），所以一起把 forbid 补上：
    # 拼错的键、发错端点的参数，现在都会当场 422 说清楚。
    model_config = _FORBID
    enabled: bool = False
    schedule_minutes: int = 0         # 定时抓取间隔（分钟），0=不定时


class TagOut(SQLModel):
    value: str
    color: int                      # 调色盘序号（0..N-1），前端映射到 TAG_PALETTE
    in_use: bool = False            # 是否被数据（订单/暂存/集运）使用中——使用中不可删除


# --- 运行期设置 ------------------------------------------------------------------

class SettingsUpdate(SQLModel):
    """只带要改的键（部分更新）。取值校验在 services/prefs 里做，那里是唯一真相。"""

    values: dict
