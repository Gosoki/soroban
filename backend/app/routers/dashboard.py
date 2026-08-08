"""看板聚合：对 jpy_settled 求和；排除软删与不计入状态（取消/退款）（P4）。

⚠️ 本文件聚合的三张表**全是支出**，`total_jpy` 是总支出。将来接入收入表（卖出/利润）
必须先决定符号——不能只给它实现一个 `ledger_exclusions()` 就 `_sum` 进来，
那会把收入当支出加进合计。`tests/test_tags_dashboard.py` 钉着「恰好这三张表」。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import is_mysql
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
    conds = [model.is_delete.is_(False)]
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

    return DashboardRead(
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
