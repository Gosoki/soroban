"""物品列表（对接的最小单位）：把所有 OrderItem 拉平成一张表，附父订单只读上下文。

只读列表：筛选/搜索/分页与淘宝订单页一致。物品编辑仍在淘宝订单页的展开面板里做
（那里改物品会重算订单价并镜像暂存）。已软删订单的物品不出现。"""
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import ci_contains
from ..models import OrderItem, Order, ShipmentOrder
from ..schemas import ItemListRead

router = APIRouter(
    prefix="/api/items", tags=["items"], dependencies=[Depends(get_current_user)]
)

_Q = Decimal("0.01")


@router.get("")
def list_items(
    session: Session = Depends(get_session),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    fulfillment_status: Optional[str] = None,
    purchase_status: Optional[str] = Query(None, description="只按订单**自身**的采购段状态筛（不看挂靠的集运单）"),
    platform_account: Optional[str] = None,
    platform: Optional[str] = None,
    q: Optional[str] = Query(None, description="按物品名/订单号/商品搜索"),
    legacy_status: Optional[str] = Query(
        None, alias="status", include_in_schema=False,
        description="已废弃：查询参数 status 已按业务段改名"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    # FastAPI 对未知 query 参数**默认忽略**：改名后漏改一处调用方，
    # 表现就是「筛选点了没反应、返回全量、HTTP 200、零日志」。响亮拒掉比静默返回错数据强。
    if legacy_status:
        raise HTTPException(status_code=400,
                            detail="查询参数 status 已改名为 fulfillment_status（显示口径）或 purchase_status（订单自身）")
    conds = [Order.is_delete.is_(False)]   # 只列未软删订单的物品
    if date_from:
        conds.append(Order.date >= date_from)
    if date_to:
        conds.append(Order.date <= date_to)
    if fulfillment_status:
        # 与**显示**口径一致（列表显示的是继承来的集运状态）。只筛 Order.status 会出现
        # 「界面上一排『已发出』，筛『已发出』却一条都搜不到」——orders.py:85 专门防过这个坑。
        # 用相关子查询而非 JOIN：不改变结果行形状，下面那句 func.count() 与分页才不变形。
        ship_status = (
            select(ShipmentOrder.shipment_status)
            .where(ShipmentOrder.id == Order.shipment_order_id,
                   ShipmentOrder.is_delete.is_(False))
            .scalar_subquery()
        )
        conds.append(func.coalesce(ship_status, Order.purchase_status) == fulfillment_status)
    if purchase_status:
        conds.append(Order.purchase_status == purchase_status)
    if platform_account:
        conds.append(Order.platform_account == platform_account)
    if platform:
        conds.append(Order.platform == platform)
    if q:   # 统一模糊搜：物品名 / 商品标题 / 订单号 / 快递号
        conds.append(
            OrderItem.name.contains(q, autoescape=True)
            | ci_contains(Order.order_no, q, session)
            | Order.title.contains(q, autoescape=True)
            | Order.express_no.contains(q, autoescape=True)
        )

    join = (OrderItem, Order.id == OrderItem.order_id)
    total = session.exec(
        select(func.count()).select_from(Order).join(*join).where(*conds)
    ).one()
    rows = session.exec(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        # fulfillment_status 会触碰 shipment_order 关系；不预加载就是整页逐行发 SQL（N+1）
        .options(selectinload(Order.shipment_order))
        .where(*conds)
        .order_by(Order.date.desc(), Order.id.desc(), OrderItem.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()

    items = []
    for it, o in rows:
        amount = None
        if it.unit_price_cny is not None:
            amount = (Decimal(it.unit_price_cny) * (it.quantity or 1)).quantize(_Q, rounding=ROUND_HALF_UP)
        items.append(ItemListRead(
            id=it.id, name=it.name, quantity=it.quantity, unit_price_cny=it.unit_price_cny,
            amount_cny=amount, auto=it.auto,
            order_id=o.id, date=o.date, order_no=o.order_no, title=o.title,
            platform_account=o.platform_account, platform=o.platform, purchase_status=o.purchase_status,
            fulfillment_status=o.fulfillment_status, shipment_order_id=o.shipment_order_id,
            express_no=o.express_no,
        ))
    return {"items": items, "total": total}
