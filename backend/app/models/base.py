"""共通基类与枚举（所有页面共享）。

金额规则（见 docs/README.md）：
- price_cny / fx_rate / jpy_override 是「输入」列。
- jpy_auto / jpy_settled 是「派生」列，但落库为普通 int 列，写入时由
  compute_money() 用 Decimal + ROUND_HALF_UP 精确算出（不用 float，不用生成列）。
- 结算优先级：jpy_override 有值就用它，否则用 jpy_auto。

字符串列长度：为兼容 MySQL（索引/非索引 VARCHAR 均需长度），所有 str 列都显式给
max_length；长文本（备注/JSON 留底）用 sa_column=Column(Text)。SQLite 不强制长度，
两方言共用同一份定义。
"""

import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from sqlalchemy import Text
from sqlmodel import Field, SQLModel

from ..config import CNY_MAX, JPY_MAX
from ..db.dialect import UtcDateTime


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- 枚举（作为「允许值」的唯一真相；DB 里存字符串值，schema 里做校验）----------

# 名字必须与列名 created_via 一致：叫 Source 时会被读成「平台=淘宝」，
# 而它其实是「由淘宝爬虫写入」——恰好又和 Order.platform == "淘宝" 是两件事。
class CreatedVia(str, Enum):
    manual = "manual"
    imported = "imported"       # 从暂存表导入
    taobao_bot = "taobao_bot"
    shipment_bot = "shipment_bot"


class PurchaseStatus(str, Enum):
    """**只记国内段**：从下单到国内快递签收。

    国际段（集运中/送达）不在这里——它的唯一真相是所挂靠集运单的 `ShipmentStatus`。
    订单一旦挂上集运单，展示的状态就跟随那张单（见 `Order.fulfillment_status`）；
    释放出来则回落到本列。这样同一件事只有一处记录，不会像以前那样
    「订单说集运中、它挂的集运单说已发出」。
    """

    unpaid = "待付款"       # 等待买家付款
    paid = "待发货"         # 买家已付款、待卖家发货
    shipped = "待收货"      # 卖家已发货
    # 「已签收」= **国内**快递签收（淘宝/闲鱼页面上的「交易成功」就是这一刻）。
    # 曾短暂叫过「已入仓」，因为集运单那边也有个「已签收」、同字面量不同义容易出错；
    # 现在集运侧已改叫「已送达」，冲突消失，故用回用户口径的「已签收」。
    signed = "已签收"       # 国内快递签收（淘宝/闲鱼「交易成功」）—— 国内段终态
    refunded = "退款"
    cancelled = "交易关闭"


# 国内段生命周期序：只准前进不回退（OCR 合并 / 爬虫回灌时据此判定）。
# 退款/交易关闭是旁支终态，不参与推进 → 用 purchase_status_rank() 取 -1。
# 国际段不在序里——它由集运单自己的状态表达，订单不复制。
PURCHASE_STATUS_RANK = {
    PurchaseStatus.unpaid.value: 0,
    PurchaseStatus.paid.value: 1,
    PurchaseStatus.shipped.value: 2,
    PurchaseStatus.signed.value: 3,
}


# 旁支**终态**：走到这里就结束了，不再沿生命周期前进。
# 它们不在 PURCHASE_STATUS_RANK 里，所以 purchase_status_rank() 返回 -1——
# ⚠️ 而 -1 的实际效果是「**任何**推进态都 > 它，于是谁都能盖掉它」，与「不参与推进」
# 这个本意正好相反。曾因此出过事：一张已标「退款」的单被 OCR 再识别一次，
# 兜底判成「待发货」（rank 1 > -1）就把退款抹掉了，而退款单本来不计入看板合计
# （见 EXCLUDED_STATUSES）→ **看板金额凭空变大**。
# 所以「能不能覆盖」必须走 can_advance_purchase()，不能只比 rank。
PURCHASE_TERMINAL_STATUSES = frozenset({PurchaseStatus.refunded.value, PurchaseStatus.cancelled.value})


def purchase_status_rank(status: Optional[str]) -> int:
    """状态在国内段生命周期里的序号。终态与未知值取 -1。

    **别拿它单独做「能不能覆盖」的判定**——-1 会让终态被任何推进态盖掉。用 can_advance_purchase()。
    """
    return PURCHASE_STATUS_RANK.get(status or "", -1)


def is_purchase_terminal(status: Optional[str]) -> bool:
    """是不是旁支终态（退款 / 交易关闭）。"""
    return (status or "") in PURCHASE_TERMINAL_STATUSES


def can_advance_purchase(current: Optional[str], incoming: Optional[str]) -> bool:
    """自动化写入（OCR 识别、爬虫回灌）能不能把 current 改成 incoming。

    规则，按优先级：
      1. incoming 为空 / 与 current 相同 → 不写。
      2. **current 已是终态 → 一律不写**。退款、交易关闭是人（或平台）明确下的结论，
         自动识别不该推翻它。这一条是本函数存在的理由。
      3. incoming 是终态 → 允许写。平台把单关掉/退款了，账本该跟上。
      4. 其余按 rank 只前进不回退。

    ⚠️ 只约束**自动化**写入。人在界面上手动改状态不走这里——用户说了算。
    """
    if not incoming or incoming == current:
        return False
    if is_purchase_terminal(current):
        return False
    if is_purchase_terminal(incoming):
        return True
    return purchase_status_rank(incoming) > purchase_status_rank(current)


class ShipmentStatus(str, Enum):
    packing = "打包中"
    shipped = "已发出"
    # 「已送达」= 国际包裹送到我手上（= 签收）。刻意不叫「已签收」——那是**订单**侧
    # 国内快递签收的说法，同字面量不同义曾出过事（EXCLUDED_STATUSES 这类集合跨两张表用）。
    delivered = "已送达"
    cancelled = "已取消"


class ImportStatus(str, Enum):
    pending = "待处理"
    imported = "已导入"
    ignored = "已忽略"


# 看板合计要排除的状态：未付款/退款/交易关闭都不计入（金额与物品仍照常显示，只是不加总）。
# 不再用「负数行」冲抵退款——打上退款/关闭标记即自动不计入。
#
# **按段拆开**：原先是一个大集合，被同一个 `notin_` 同时喂给订单表和集运表，
# 「订单表不会误伤集运的值」全靠「两个枚举值域恰好不相交」这条巧合撑着。
# 那是把一个巧合当成了契约：加第三个枚举（卖出段）时它会绷断，而绷断的表现是
# **看板金额悄悄变了**，不是报错。现在每张表只排除自己那一段的值。
PURCHASE_EXCLUDED = frozenset({
    PurchaseStatus.unpaid.value,          # 待付款：还没花钱，不计入
    PurchaseStatus.refunded.value,        # 退款
    PurchaseStatus.cancelled.value,       # 交易关闭
})
SHIPMENT_EXCLUDED = frozenset({ShipmentStatus.cancelled.value})   # 集运已取消

# 两段的并集。现在各表只用自己那半，本常量只剩「全部不计入的状态」这层文档语义。
EXCLUDED_STATUSES = PURCHASE_EXCLUDED | SHIPMENT_EXCLUDED


def guard_cny(p: Decimal) -> Decimal:
    """人民币金额的上限卡口，越界抛 ValueError（main 里统一转 422）。

    为什么派生金额也要卡：schemas 的单字段校验只管**直填**的列，而订单/暂存的 price_cny 是
    Σ(物品单价×数量)+邮费 派生来的，单项都合法、乘出来/加起来照样能越界
    （数量 100 万 × 单价 1 万，或光靠一个顶格的邮费）。
    不卡的后果是双引擎发散：SQLite 的 NUMERIC(12,2) 只是亲和性、照单全收，脏行落库后
    连 GET 列表都会被 response_model 的校验打成 422（整页打不开）；MySQL 的 DECIMAL(12,2)
    则在 commit 时抛 1264 Out of range（实测 MySQL 9.7，默认 sql_mode 含 STRICT_TRANS_TABLES），
    经 DataError 冲到 ASGI 层变成裸 500。两边都难看，且都在写入之后才暴露。"""
    if p is None:
        return p
    d = Decimal(p)
    if not d.is_finite() or abs(d) > CNY_MAX:
        raise ValueError(f"货款金额超出可接受范围（上限 {CNY_MAX}）")
    return d


def price_from_items(items) -> Decimal:
    """订单/暂存的人民币总价 = Σ(物品单价 × 数量)，量化到分。空价按 0。

    系统的最小单位是「物品(OrderItem/StagingItem)」：物品带单价(price_cny)与数量(quantity)，
    订单价由此派生、不再直接编辑（见各 model 的 sync_from_items 与 README「物品为最小单位」）。"""
    total = sum(
        (Decimal(it.unit_price_cny or 0) * (it.quantity or 1) for it in items),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- 共通基类：日期/备注/来源/付款人/乐观锁/软删/时间戳 + 金额输入与派生 --------

class LedgerBase(SQLModel):
    @classmethod
    def ledger_exclusions(cls):
        """看板合计要跳过哪些行：`[(状态列, 不计入的值集合), ...]`，每条都不能命中。

        空列表 = 这张表没有「计入/不计入」的状态维度（杂项支出就是）。

        **为什么是列表而不是单条**：将来加卖出/退货这类并行的状态轴时，是往列表里
        追一项，而不是改看板的函数签名。

        **为什么列从 `cls` 上取**：看板原先写的是 `model.status`——鸭子类型，
        唯一依据是「两张表的状态列恰好同名」。改成把列**传进去**同样不行：
        `_sum(session, Order, ShipmentOrder.status)` 这种手滑不报错，SQLAlchemy 会把
        对侧表自动加进 FROM 生成隐式交叉连接，`SUM` 乘以对侧行数——**看板金额静默变大**。
        从 `cls` 上取，引用到别的表在语法上就写不出来。
        """
        return []

    date: dt.date = Field(index=True)                       # 记录日期
    note: Optional[str] = Field(default=None, sa_type=Text)  # sa_type（非 sa_column）→ 每个子表各建一份，避免共享 Column 报错
    # created_via = 这行**怎么进来的**（手填/从暂存导入/机器人）。刻意不叫 source——
    # 那会和 platform（UI 标签就叫「来源」，值是 淘宝/闲鱼/京东）撞义。
    created_via: str = Field(default=CreatedVia.manual.value, max_length=32, index=True)
    payer_id: Optional[int] = Field(default=None, foreign_key="user.id")
    version: int = Field(default=1)                          # 乐观锁
    is_delete: bool = Field(default=False, index=True)       # 软删标记（True=已删，查询默认过滤）
    # UtcDateTime：MySQL 侧显式 DATETIME(6)。不写 fsp 建出来是 DATETIME(0)，
    # 超精度的值会被**四舍五入**（不是截断、也不报 warning），迁移过去时间戳就变了。
    created_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())

    # 金额输入列
    price_cny: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    fx_rate: Optional[Decimal] = Field(default=None, max_digits=10, decimal_places=4)
    jpy_override: Optional[int] = Field(default=None)
    override_note: Optional[str] = Field(default=None, max_length=255)
    # 金额派生列（落库；写入时算，勿手改）
    jpy_auto: Optional[int] = Field(default=None)
    jpy_settled: Optional[int] = Field(default=None)

    def compute_money(self, extra_jpy: int = 0) -> None:
        """用 Decimal 精确重算 jpy_auto / jpy_settled。extra_jpy 供集运加特殊费。
        先把 cny/rate 量化到入库精度，保证派生日元与最终存储/展示值一致。

        单字段校验（schemas）只卡住直填列；订单/暂存的 price_cny 是 Σ(物品×数量)+邮费
        派生而来、绕过了单字段校验，故这里对**最终**货款与派生日元再卡一次上限：越界抛
        ValueError（main 里统一转 422），防止 Numeric(12,2)/有符号 INT 溢出与双引擎发散。"""
        if self.price_cny is not None:
            guard_cny(self.price_cny)               # 与 OrderStaging.sync_from_items 同一把卡口
        if self.price_cny is not None and self.fx_rate is not None:
            cny = Decimal(self.price_cny).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rate = Decimal(self.fx_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            auto = int((cny * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) + extra_jpy
        elif extra_jpy:
            auto = extra_jpy
        else:
            auto = None
        if auto is not None and abs(auto) > JPY_MAX:
            raise ValueError(f"结算日元超出可接受范围（货款×汇率 超过 {JPY_MAX}），请降低货款或汇率")
        self.jpy_auto = auto
        self.jpy_settled = self.jpy_override if self.jpy_override is not None else auto
