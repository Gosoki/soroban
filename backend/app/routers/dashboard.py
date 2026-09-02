"""看板聚合：对 jpy_settled 求和；排除软删与不计入状态（取消/退款）（P4）。

⚠️ 本文件聚合的三张表**全是支出**，`total_jpy` 是总支出。将来接入收入表（卖出/利润）
必须先决定符号——不能只给它实现一个 `ledger_exclusions()` 就 `_sum` 进来，
那会把收入当支出加进合计。`tests/test_tags_dashboard.py` 钉着「恰好这三张表」。
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import is_mysql
from ..models.base import not_deleted, unconverted_clause
from ..models import ShipmentOrder, MiscExpense, Order
from ..schemas import DashboardRead, MonthTotal
from ..services.fx import current_rate

router = APIRouter(
    prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_user)]
)

def _valid_conds(model):
    """未软删 + 每一条 ledger_exclusions() 都不命中。

    排除规则由**模型自己**声明（见 LedgerBase.ledger_exclusions）。原先这里写的是
    `model.status`——鸭子类型，靠「两张表状态列恰好同名」成立，列一改名就是运行期
    AttributeError、看板整页 500。"""
    conds = [not_deleted(model)]
    for col, excluded in model.ledger_exclusions():
        conds.append(col.notin_(tuple(excluded)))   # 取消/退款不计入
    return conds


def _sum(session: Session, model) -> int:
    conds = _valid_conds(model)
    return int(
        session.exec(
            select(func.coalesce(func.sum(model.jpy_settled), 0)).where(*conds)
        ).one()
    )


def _count(session: Session, model) -> int:
    conds = _valid_conds(model)
    return int(session.exec(select(func.count()).select_from(model).where(*conds)).one())


def _uncounted(session: Session, model) -> tuple[int, Decimal]:
    """有钱、却没能算出日元的行：(笔数, 人民币合计)。

    **为什么要单独数出来**：`_sum` 求的是 `SUM(jpy_settled)`，NULL 被 SQL 直接跳过——
    于是「有货款但缺汇率」的行，**金额被吞、笔数照数**。看板上合计变小、单数不变，
    没有任何一处显示异常。命中条件很实在：全新部署且汇率插件一次都没成功跑过
    （断网 / 源挂了 / 打包版根本跑不起来，见 T9），而用户已经开始录单。

    行级其实有信号（汇率、结算两列空白），但看板侧完全静默——而看板正是用来
    「一眼看总数」的地方。这里不改写入（缺汇率照样能记账，断网时不该记不了账），
    只把被吞掉的部分摆到台面上。

    判据用 `price_cny` 真值而不是 `is not None`：显式填 0 的行（预付/包邮）
    没有任何金额会被吞，报出来只是噪音。
    """
    # 判据走 `models.base.unconverted_clause`——与列表页脚、集运到岸同一份规则。
    # 这三处历史上分叉过两次，每次都是漏抄 `!= 0`。
    conds = [*_valid_conds(model), unconverted_clause(model)]
    n, cny = session.exec(
        select(func.count(), func.coalesce(func.sum(model.price_cny), 0)).where(*conds)
    ).one()
    return int(n), Decimal(str(cny or 0))


def _month_expr(session: Session, date_col):
    """按月分组的 '%Y-%m' 表达式，跨方言：MySQL 用 DATE_FORMAT，SQLite 用 strftime。"""
    if is_mysql(session.get_bind()):
        return func.date_format(date_col, "%Y-%m")
    return func.strftime("%Y-%m", date_col)


def _by_month_cat(session: Session, model) -> dict:
    """某类目按月的 {月份: (结算日元合计, 单数)}。"""
    conds = _valid_conds(model)
    month = _month_expr(session, model.date)
    rows = session.exec(
        select(month, func.coalesce(func.sum(model.jpy_settled), 0), func.count())
        .where(*conds)
        .group_by(month)
    ).all()
    return {m: (int(j), int(c)) for m, j, c in rows if m is not None}


@router.get("", response_model=DashboardRead)
def dashboard(session: Session = Depends(get_session)):
    order_jpy = _sum(session, Order)
    shipment_jpy = _sum(session, ShipmentOrder)
    misc_jpy = _sum(session, MiscExpense)

    tb = _by_month_cat(session, Order)
    sp = _by_month_cat(session, ShipmentOrder)
    mc = _by_month_cat(session, MiscExpense)
    by_month = []
    for m in sorted(set(tb) | set(sp) | set(mc), reverse=True):   # 最新月在前（卡片自上而下）
        t_j, t_c = tb.get(m, (0, 0))
        s_j, s_c = sp.get(m, (0, 0))
        x_j, x_c = mc.get(m, (0, 0))
        by_month.append(MonthTotal(
            month=m, jpy=t_j + s_j + x_j,
            order_jpy=t_j, shipment_jpy=s_j, misc_jpy=x_j,
            order_count=t_c, shipment_count=s_c, misc_count=x_c,
        ))

    # 被吞掉的部分：三张表各数一遍再合并。分表数没有额外价值——用户要知道的是
    # 「合计里少了多少」，而补救动作（把汇率填上）也只有一个。
    unc = [_uncounted(session, m) for m in (Order, ShipmentOrder, MiscExpense)]
    unc_n = sum(n for n, _ in unc)
    unc_cny = sum((c for _, c in unc), Decimal("0"))

    return DashboardRead(
        uncounted_count=unc_n,
        uncounted_cny=unc_cny,
        total_jpy=order_jpy + shipment_jpy + misc_jpy,
        order_jpy=order_jpy,
        shipment_jpy=shipment_jpy,
        misc_jpy=misc_jpy,
        order_count=_count(session, Order),
        shipment_count=_count(session, ShipmentOrder),
        misc_count=_count(session, MiscExpense),
        by_month=by_month,
        fx_rate=current_rate(session),
    )
