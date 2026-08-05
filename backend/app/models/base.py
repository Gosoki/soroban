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


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- 枚举（作为「允许值」的唯一真相；DB 里存字符串值，schema 里做校验）----------

class Source(str, Enum):
    manual = "manual"
    imported = "imported"       # 从暂存表导入
    taobao_bot = "taobao_bot"
    shipment_bot = "shipment_bot"


class OrderStatus(str, Enum):
    unpaid = "待付款"       # 等待买家付款
    paid = "待发货"         # 买家已付款、待卖家发货
    shipped = "待收货"      # 卖家已发货
    received = "已签收"     # 国内快递被集运仓签收（闲鱼页面显示「交易成功」）
    consolidating = "集运中"  # 已挂靠集运单，等待打包/发出
    arrived = "已到达"      # 终态：国际段送达、本人收到货
    refunded = "退款"
    cancelled = "交易关闭"


# 交易状态生命周期序：只准前进不回退（挂靠/OCR 合并时据此判定）。
# 退款/交易关闭是旁支终态，不参与推进 → 用 order_status_rank() 取 -1。
ORDER_STATUS_RANK = {
    OrderStatus.unpaid.value: 0,
    OrderStatus.paid.value: 1,
    OrderStatus.shipped.value: 2,
    OrderStatus.received.value: 3,
    OrderStatus.consolidating.value: 4,
    OrderStatus.arrived.value: 5,
}


def order_status_rank(status: Optional[str]) -> int:
    """状态在生命周期里的序号；退款/交易关闭/未知一律 -1（不参与「只前进」判定）。"""
    return ORDER_STATUS_RANK.get(status or "", -1)


class ShipmentStatus(str, Enum):
    packing = "打包中"
    shipped = "已发出"
    arrived = "已签收"
    cancelled = "已取消"


class StagingStatus(str, Enum):
    pending = "待处理"
    imported = "已导入"
    ignored = "已忽略"


# 看板合计要排除的状态：未付款/退款/交易关闭都不计入（金额与物品仍照常显示，只是不加总）。
# 不再用「负数行」冲抵退款——打上退款/关闭标记即自动不计入。
EXCLUDED_STATUSES = {
    OrderStatus.unpaid.value,          # 待付款：还没花钱，不计入
    OrderStatus.refunded.value,        # 退款
    OrderStatus.cancelled.value,       # 交易关闭
    ShipmentStatus.cancelled.value,     # 集运已取消
}


def price_from_items(items) -> Decimal:
    """订单/暂存的人民币总价 = Σ(物品单价 × 数量)，量化到分。空价按 0。

    系统的最小单位是「物品(OrderItem/StagingItem)」：物品带单价(price_cny)与数量(quantity)，
    订单价由此派生、不再直接编辑（见各 model 的 sync_from_items 与 README「物品为最小单位」）。"""
    total = sum(
        (Decimal(it.price_cny or 0) * (it.quantity or 1) for it in items),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- 共通基类：日期/备注/来源/付款人/乐观锁/软删/时间戳 + 金额输入与派生 --------

class LedgerBase(SQLModel):
    date: dt.date = Field(index=True)                       # 记录日期
    note: Optional[str] = Field(default=None, sa_type=Text)  # sa_type（非 sa_column）→ 每个子表各建一份，避免共享 Column 报错
    source: str = Field(default=Source.manual.value, max_length=32, index=True)
    payer_id: Optional[int] = Field(default=None, foreign_key="user.id")
    version: int = Field(default=1)                          # 乐观锁
    is_delete: bool = Field(default=False, index=True)       # 软删标记（True=已删，查询默认过滤）
    created_at: dt.datetime = Field(default_factory=utcnow)
    updated_at: dt.datetime = Field(default_factory=utcnow)

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
            p = Decimal(self.price_cny)
            if not p.is_finite() or abs(p) > CNY_MAX:
                raise ValueError(f"货款金额超出可接受范围（上限 {CNY_MAX}）")
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
