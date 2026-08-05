"""集运订单 CRUD。金额 = 运费(CNY→JPY) + 特殊费_日元。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, or_
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..models import OrderStatus, ShipmentOrder, Order, order_status_rank, utcnow
from ..schemas import (
    ShipmentCreate, ShipmentOcrAttachResult, ShipmentRead, ShipmentUpdate, OrderItemRead, OrderBrief,
)
from .common import conflict, guarded_bump, mirror_to_staging, not_found, run_ocr, soft_delete

router = APIRouter(
    prefix="/api/shipment", tags=["shipment"], dependencies=[Depends(get_current_user)]
)


def _brief(order: Order) -> OrderBrief:
    return OrderBrief(
        id=order.id, order_no=order.order_no, date=order.date, shop=order.shop,
        status=order.status, jpy_settled=order.jpy_settled,
        items=[OrderItemRead(id=it.id, name=it.name, quantity=it.quantity) for it in order.items],
    )


def _read(session: Session, order: ShipmentOrder) -> ShipmentRead:
    """构造响应，其中 orders 只含未软删的关联淘宝订单（不泄露已删数据）。"""
    r = ShipmentRead.model_validate(order)
    children = session.exec(
        select(Order)
        .where(
            Order.shipment_order_id == order.id,
            Order.is_delete.is_(False),
        )
        .options(selectinload(Order.items))   # 批量载入子订单物品，避免 N+1
    ).all()
    r.orders = [_brief(c) for c in children]
    return r


@router.get("")
def list_orders(
    session: Session = Depends(get_session),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    status: Optional[str] = None,
    q: Optional[str] = Query(None, description="按集运单号搜索"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conds = [ShipmentOrder.is_delete.is_(False)]
    if date_from:
        conds.append(ShipmentOrder.date >= date_from)
    if date_to:
        conds.append(ShipmentOrder.date <= date_to)
    if status:
        conds.append(ShipmentOrder.status == status)
    if q:
        conds.append(ShipmentOrder.shipment_no.contains(q, autoescape=True))

    total = session.exec(select(func.count()).select_from(ShipmentOrder).where(*conds)).one()
    rows = session.exec(
        select(ShipmentOrder)
        .where(*conds)
        .order_by(ShipmentOrder.date.desc(), ShipmentOrder.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [_read(session, r) for r in rows], "total": total}


@router.post("/ocr")
async def ocr_shipment(file: UploadFile = File(...)):
    """识别集运「支付详情」截图。成品包裹页 → 集运单号/国际单号/订单时间/渠道；
    内含快递页 → 快递单号列表（要联动挂靠请改用 /{id}/ocr-express）。不落库。"""
    from ..services.ocr import recognize_shipment

    return await run_ocr(file, recognize_shipment)


@router.post("/{shipment_id}/ocr-express", response_model=ShipmentOcrAttachResult)
async def ocr_attach_express(
    shipment_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """识别「内含快递」截图，把截图里的快递单号对应的商品订单挂到本集运单并置「集运中」。

    单号匹配不上商品订单 → 只在 unmatched 里回报（不建占位单）；已挂在别的集运单 → 跳过
    不强改（沿用 attach_order 的防误抢语义）。重复上传同一张截图是幂等的。
    路由为 async（run_ocr 要 await 读文件）；DB 用同步 Session，SQLite 建连时已
    check_same_thread=False，本地库单次查询亚毫秒级，不构成事件循环阻塞。"""
    from ..services.ocr import recognize_shipment

    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        not_found("集运订单")

    fields = await run_ocr(file, recognize_shipment)
    express_nos = fields.get("express_nos") or []

    consolidating = OrderStatus.consolidating.value
    rank_consolidating = order_status_rank(consolidating)
    attached: list[Order] = []
    skipped: list[Order] = []
    unmatched: list[str] = []

    for no in express_nos:
        matches = session.exec(
            select(Order).where(Order.express_no == no, Order.is_delete.is_(False))
        ).all()
        if not matches:
            unmatched.append(no)
            continue
        for od in matches:
            if od.shipment_order_id is not None and od.shipment_order_id != shipment_id:
                skipped.append(od)                       # 已挂别的集运单：交给用户手动处理
                continue
            values = {"shipment_order_id": shipment_id,
                      "version": Order.version + 1, "updated_at": utcnow()}
            # 状态只前进不回退：已是「已到达」的单不该被拉回「集运中」
            if order_status_rank(od.status) < rank_consolidating:
                values["status"] = consolidating
            # 原子挂靠，守卫与 attach_order 同款：仍未挂靠（或已挂本单，幂等）、未软删、
            # 且集运单在极小竞态窗内没被并发软删。靠 rowcount 判定，避免「读-判断-写」双挂。
            res = session.execute(
                sa_update(Order)
                .where(
                    Order.id == od.id,
                    Order.is_delete.is_(False),
                    or_(Order.shipment_order_id.is_(None),
                        Order.shipment_order_id == shipment_id),
                    select(ShipmentOrder.id)
                    .where(ShipmentOrder.id == shipment_id, ShipmentOrder.is_delete.is_(False))
                    .exists(),
                )
                .values(**values)
            )
            if res.rowcount != 1:                        # 并发被抢走/集运单被并发删除
                skipped.append(od)
                continue
            session.refresh(od)                          # 裸 UPDATE 绕过身份映射，重读拿新状态
            mirror_to_staging(session, od, None)         # 若由暂存导入：把新状态镜像回暂存行
            attached.append(od)

    session.commit()
    return ShipmentOcrAttachResult(
        shipment=_read(session, shipment),
        attached=[_brief(o) for o in attached],
        skipped=[_brief(o) for o in skipped],
        unmatched=unmatched,
        express_nos=express_nos,
    )


@router.post("", response_model=ShipmentRead)
def create_order(payload: ShipmentCreate, session: Session = Depends(get_session)):
    from ..services.fx import current_rate  # 局部导入避免循环

    order = ShipmentOrder(**payload.model_dump())
    if order.fx_rate is None:                 # 新建时写入当天汇率
        order.fx_rate = current_rate(session)
    order.compute_money()
    session.add(order)
    session.commit()
    session.refresh(order)
    return _read(session, order)


@router.get("/{order_id}", response_model=ShipmentRead)
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(ShipmentOrder, order_id)
    if not order or order.is_delete:
        not_found("集运订单")
    return _read(session, order)


@router.patch("/{order_id}", response_model=ShipmentRead)
def update_order(order_id: int, payload: ShipmentUpdate, session: Session = Depends(get_session)):
    order = session.get(ShipmentOrder, order_id)
    if not order or order.is_delete:
        not_found("集运订单")
    if not guarded_bump(session, ShipmentOrder, order_id, payload.version):
        conflict()

    data = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in data.items():
        setattr(order, key, value)
    order.compute_money()

    session.add(order)
    session.commit()
    session.refresh(order)
    return _read(session, order)


@router.post("/{shipment_id}/order/{order_id}", response_model=ShipmentRead)
def attach_order(shipment_id: int, order_id: int, session: Session = Depends(get_session)):
    """把一个商品订单挂到本集运单（点选添加）。同一个外键 shipment_order_id，与商品页共用。
    仅允许「未挂靠」的商品单：已挂在别的集运单 → 422（先移除再加，防误抢）。"""
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        not_found("集运订单")
    od = session.get(Order, order_id)
    if not od or od.is_delete:
        not_found("商品订单")
    if od.shipment_order_id == shipment_id:
        return _read(session, shipment)               # 已挂本单，幂等
    # 原子挂载：仅当商品单在 DB 里仍未挂靠（且未软删）、且集运单当前仍存活时才成功，靠 rowcount 判定。
    # 避免「读-判断-写」在并发下双挂/误抢；EXISTS 子查询防极小竞态窗内集运单被并发软删导致挂到已删单；
    # version 在 DB 层自增，不丢失并发的自增（与 guarded_bump 同风格）。
    res = session.execute(
        sa_update(Order)
        .where(
            Order.id == order_id,
            Order.shipment_order_id.is_(None),
            Order.is_delete.is_(False),
            select(ShipmentOrder.id)
            .where(ShipmentOrder.id == shipment_id, ShipmentOrder.is_delete.is_(False))
            .exists(),
        )
        .values(shipment_order_id=shipment_id, version=Order.version + 1, updated_at=utcnow())
    )
    if res.rowcount != 1:                             # 已被并发挂到别的集运单，或集运单已被并发删除
        raise HTTPException(status_code=422, detail="该商品订单已挂靠其他集运单，请先移除")
    session.commit()
    return _read(session, shipment)


@router.delete("/{shipment_id}/order/{order_id}", response_model=ShipmentRead)
def detach_order(shipment_id: int, order_id: int, session: Session = Depends(get_session)):
    """从本集运单移除一个商品订单（解除外键）。仅当它确实挂在本单才动（幂等）。"""
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        not_found("集运订单")
    od = session.get(Order, order_id)
    if not od or od.is_delete:
        not_found("商品订单")
    # 原子解除：仅当它确实挂在本单才动（幂等）；version 在 DB 层自增，不丢并发自增。
    session.execute(
        sa_update(Order)
        .where(Order.id == order_id, Order.shipment_order_id == shipment_id)
        .values(shipment_order_id=None, version=Order.version + 1, updated_at=utcnow())
    )
    session.commit()
    return _read(session, shipment)


@router.delete("/{order_id}")
def delete_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(ShipmentOrder, order_id)
    if not order or order.is_delete:
        not_found("集运订单")
    # 解除关联淘宝订单的挂靠，避免留下指向已删集运单的悬空外键
    session.execute(
        sa_update(Order)
        .where(Order.shipment_order_id == order_id, Order.is_delete.is_(False))
        .values(shipment_order_id=None, version=Order.version + 1, updated_at=utcnow())
    )
    soft_delete(order)
    session.add(order)
    session.commit()
    return {"ok": True}
