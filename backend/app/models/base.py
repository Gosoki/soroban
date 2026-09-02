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

from sqlalchemy import Text, false
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


def not_deleted(model):
    """「这一行没被软删」的**查询条件**。所有带 `is_delete` 的查询都该用它。

    **它必须渲染成 `is_delete = 0`，不能是 `IS 0`**——这不是风格问题，是索引问题。

    `orders` 和 `shipmentorder` 上的唯一索引都是**部分索引**
    （`WHERE order_no IS NOT NULL AND is_delete = 0`，见各自的 `__table_args__`；
    部分是必须的：软删之后同一个单号要允许再出现）。而 SQLite 判断
    「这条查询能不能用这个部分索引」靠的是**语法蕴含**，不是语义等价：
    查询里写 `is_delete = 0` 才和索引的 `is_delete = 0` 对得上；
    写 `is_delete IS 0`（SQLAlchemy 的 `.is_(False)` 生成的就是它）对不上，
    于是那个索引**一次都用不上**。

    2026-09-02 实测（20000 行 orders + ANALYZE，按订单号精确查一条）：

    | 写法 | 执行计划 | 耗时 |
    |---|---|---|
    | `.is_(False)` → `is_delete IS 0` | **SCAN orders** | 4.267 ms |
    | `== false()` → `is_delete = 0` | SEARCH ... USING INDEX `ix_orders_order_no_platform_active` | **0.020 ms** |

    其余查询形状（整表计数、按日期翻页、按平台筛）计划与耗时**一模一样**——
    这是纯赢，没有需要权衡的一侧。

    受影响的是所有「按单号精确查一条」的路径：OCR 去重问「这个单号是不是已经有了」、
    暂存导入前的查重、插件回灌时的存在性判断。它们都是**逐条**发起的，
    所以代价随批量线性放大，而全表 SCAN 在 SQLite 上还会和唯一的写者抢锁。

    六种写法逐个实测过（2000 行 + ANALYZE），只有两种会丢索引：

    | 写法 | 渲染成 | 结果 |
    |---|---|---|
    | `.is_(False)` | `is_delete IS 0` | **全表扫** |
    | `!= True` | `is_delete != 1` | **全表扫** |
    | `== False` / `== false()` / `~col` / `not_(col)` | `is_delete = 0` | 走索引 |

    收敛成一个具名函数而不是散着写 `== false()`：
    ① 那个写法本身没有任何东西提示它为什么不能是 `.is_(False)`，
       下一个人「顺手改整齐」就会静默丢掉索引，而且**没有任何报错**——
       查询照样返回正确结果，只是慢两百倍；
    ② `== False` 会被 flake8 的 E712 挑刺，于是更容易被改回去。

    真正的兜底是 `test_the_exact_order_no_lookup_actually_uses_its_index`：
    它问数据库「你打算怎么执行」，不猜写法。
    """
    return model.is_delete == false()


def is_unconverted(price_cny, jpy_settled) -> bool:
    """这一行算不算「有钱、却没折算成日元」。**全仓唯一真相。**

    三个出口都要用它，判据必须逐条一致：看板的 `_uncounted`、列表页脚的 `list_totals`、
    集运到岸成本的 `_landed`。分叉的现象不是报错，而是**两个数字互相打脸**——
    同一件事，页脚说 1 条、看板说 0 条，用户没有任何办法判断该信哪个。

    `!= 0` 那一条是必须的：显式填 0 的行（预付 / 包邮 / 全是赠品的单）折算过去也是 0 円，
    没有任何金额会被 `SUM` 吞掉，报出来只是噪音——而用户按告警去补汇率也消不掉它。

    这三处历史上已经分叉过两次（审计报告 §151.3、§169），每次都是漏抄了 `!= 0`。
    所以把它变成一个函数，而不是一条「记得三处都改」的约定。
    """
    return price_cny is not None and price_cny != 0 and jpy_settled is None


def unconverted_clause(model):
    """同一条判据的 **SQL 形态**：给 `where()` / `case()` 用的过滤条件。

    为什么要有两种形态：三个出口里有两个（看板 `_uncounted`、列表页脚 `list_totals`）
    在数据库里聚合，构造的是 SQLAlchemy 表达式，调不动上面那个 Python 版；
    第三个（集运到岸 `_landed`）子订单已经在内存里，用 Python 版。
    **规则只有一份，形态有两种**——这正是它们历史上分叉两次的地方
    （每次都是漏抄 `!= 0`）。改判据时两个函数必须一起改，就在彼此隔壁。
    """
    return (model.price_cny.isnot(None)) & (model.price_cny != 0) & (model.jpy_settled.is_(None))


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
    # copy_abs 而非 abs：abs(Decimal) 在超大指数上抛 decimal.Overflow（非 ValueError），
    # 会绕过所有兜底变成裸 500——而这一行正是防极端量级的那道闸。
    if not d.is_finite() or d.copy_abs() > CNY_MAX:
        raise ValueError(f"货款金额超出可接受范围（上限 {CNY_MAX}）")
    return d


def items_carry_no_price(items) -> bool:
    """这批物品**派生不出订单价**——一条都没有，或者每一条的单价都是空的。

    两种情形是同一件事：**「不知道多少钱」不是「这单值 0 元」**，
    而 `price_from_items` 对两者算出来的恰好都是后者（`Decimal(x or 0)`，再加邮费）。

    第二种情形是真实的历史状态，不是假想：`f6a7b8c9d0e1` 那次迁移只加了
    `orderitem.unit_price_cny` 这一列（nullable、无回填），docstring 明写着
    「既有行的数据回填由一次性脚本完成（见 `tools/backfill_item_price.py`）」——
    而**启动链和恢复链都只跑 alembic，没有任何一条会去跑那个脚本**。
    于是那之前建的订单，物品有名称有数量、单价是 NULL。

    伤害的触发条件低得离谱：在订单页对这样一张单做**任何一次** PATCH——
    改个状态下拉、补一个快递号、加一句备注都行——`update_order` 会无条件调
    `sync_from_items()`，货款当场从 ¥300 变成 ¥0（邮费还在），日元一起变 0。
    没有报错、没有提示，保存成功。在列表里改状态下拉的人根本看不到金额列。
    看板合计随之静默缩水，而且**再编辑一次也回不来**——0 已经被固化了。

    「一条物品都没有」这一支同样保留（`all()` 对空序列返回 True，两种情形共用一条判据）。
    """
    return all(getattr(it, "unit_price_cny", None) is None for it in items)


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
        if self.price_cny and self.fx_rate is None:
            # **算不出就是算不出**，不许拿 extra_jpy 冒充一个完整的结算额。
            # 集运单的 price_cny 是「运费」、extra_jpy 是「特殊费」：缺汇率时落到下面那支的话，
            # 界面上会显示一个看起来完整的金额（只有特殊费），运费部分永久缺失并被看板加总——
            # 商品订单同场景显示的是「—」，一眼看得出缺口。两者必须同一个口径。
            # 判据用真值不用 `is not None`：运费显式填 0（预付/包邮）+ 特殊费，
            # 那是一笔算得出的账，不该被打成 None 反而丢钱。
            auto = None
        elif self.price_cny is not None and self.fx_rate is not None:
            cny = Decimal(self.price_cny).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            rate = Decimal(self.fx_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            auto = int((cny * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) + extra_jpy
        elif extra_jpy:
            auto = extra_jpy
        else:
            auto = None
        # 这里是 int（日元），Python 的 int 任意精度、abs() 永不抛——**不要**跟着改 copy_abs
        # （int 没有那个方法）。copy_abs 只对 Decimal 有意义，见 schemas._q_decimal。
        if auto is not None and abs(auto) > JPY_MAX:
            raise ValueError(f"结算日元超出可接受范围（货款×汇率 超过 {JPY_MAX}），请降低货款或汇率")
        self.jpy_auto = auto
        self.jpy_settled = self.jpy_override if self.jpy_override is not None else auto
