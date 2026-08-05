"""淘宝订单 CRUD。软删过滤、乐观锁、金额重算、OrderItem 子表替换。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..models import ShipmentOrder, OrderItem, StagingStatus, Order, OrderStaging, utcnow
from ..schemas import OrderCreate, OrderRead, OrderUpdate
from .common import (
    build_items, conflict, guarded_bump, mirror_to_staging, not_found, run_ocr, soft_delete,
)

router = APIRouter(
    prefix="/api/orders", tags=["orders"], dependencies=[Depends(get_current_user)]
)


def _check_shipment(session: Session, shipment_id):
    """挂靠的集运订单必须存在且未软删（防悬空/无效外链）。

    用标量 SELECT 直读 DB，而非 session.get——后者命中身份映射缓存会返回加载时的旧
    is_delete，同事务里第二次校验（create_order flush 后复核）就形同虚设。标量列查询
    每次都发 SQL、读事务内最新状态，才能真正闭合「校验通过→集运单被并发软删→仍挂上」。"""
    if shipment_id is not None:
        alive = session.execute(
            select(ShipmentOrder.id).where(
                ShipmentOrder.id == shipment_id, ShipmentOrder.is_delete.is_(False)
            )
        ).first()
        if alive is None:
            raise HTTPException(status_code=422, detail="所属集运订单不存在或已删除")


@router.get("")
def list_orders(
    session: Session = Depends(get_session),
    id: Optional[int] = Query(None, description="定位单条：供集运页点订单号跳转过来隔离显示"),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    platform_account: Optional[str] = None,
    express_no: Optional[str] = None,
    shipment_order_id: Optional[int] = None,
    unassigned: Optional[bool] = Query(None, description="仅未挂靠集运的订单（供集运页点选添加）"),
    order_no: Optional[str] = Query(None, description="按订单号精确匹配（OCR 去重用，区别于模糊 q）"),
    q: Optional[str] = Query(None, description="按订单号搜索（子串模糊）"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conds = [Order.is_delete.is_(False)]
    if id is not None:
        conds.append(Order.id == id)
    if unassigned:
        conds.append(Order.shipment_order_id.is_(None))
    if date_from:
        conds.append(Order.date >= date_from)
    if date_to:
        conds.append(Order.date <= date_to)
    if status:
        conds.append(Order.status == status)
    if platform:
        conds.append(Order.platform == platform)
    if platform_account:
        conds.append(Order.platform_account == platform_account)
    if express_no:
        conds.append(Order.express_no == express_no)
    if shipment_order_id is not None:
        conds.append(Order.shipment_order_id == shipment_order_id)
    if order_no:
        conds.append(Order.order_no == order_no)   # 精确：OCR 去重靠它，不受子串 q 的 20 条上限影响
    if q:   # 统一模糊搜：物品名 / 商品标题 / 订单号 / 快递号（物品名用 EXISTS 子查询，不重复行）
        conds.append(
            Order.order_no.contains(q, autoescape=True)
            | Order.shop.contains(q, autoescape=True)
            | Order.express_no.contains(q, autoescape=True)
            | Order.items.any(OrderItem.name.contains(q, autoescape=True))
        )

    total = session.exec(select(func.count()).select_from(Order).where(*conds)).one()
    rows = session.exec(
        select(Order)
        .where(*conds)
        .order_by(Order.date.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [OrderRead.model_validate(r) for r in rows], "total": total}


@router.post("/ocr")
async def ocr_order(file: UploadFile = File(...)):
    """识别订单详情截图，抽取快递公司/快递号/订单号/成交价供前端自动填表。"""
    from ..services.ocr import recognize_order

    return await run_ocr(file, recognize_order)


@router.post("", response_model=OrderRead)
def create_order(payload: OrderCreate, session: Session = Depends(get_session)):
    from ..services.fx import current_rate  # 局部导入避免循环

    data = payload.model_dump(exclude={"items", "price_cny"})   # 订单价由物品派生，不直接落库
    order = Order(**data)
    _check_shipment(session, order.shipment_order_id)   # 挂靠的集运单不存在/已删 → 友好 422（而非 FK 撞库转 409）
    if order.fx_rate is None:                 # 新建时写入当天汇率
        order.fx_rate = current_rate(session)
    # 最小单位是物品：至少 1 条（无物品则按商品名+货款自动生成，灰显可改）。
    # 播种用「货款」= 订单价种子 - 邮费，避免把邮费也摊进物品单价（否则 sync 加邮费会重复计）。
    seed_goods = (payload.price_cny - (payload.postage_cny or 0)) if payload.price_cny is not None else None
    order.items = [OrderItem(**d) for d in build_items(payload.items, seed_goods, payload.shop)]
    order.sync_from_items()                   # price_cny = Σ(单价×数量) + 邮费，并重算日元
    session.add(order)
    session.flush()                           # 写入并占写锁；FK 保证集运单硬存在
    # flush 后复核集运单仍未软删：_check_shipment 用标量 SELECT 直读 DB（见其注释），与本次
    # 写入同一事务，闭合「校验通过→集运单被并发软删→订单仍挂上」的 TOCTOU
    _check_shipment(session, order.shipment_order_id)
    session.commit()
    session.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderRead)
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        not_found("淘宝订单")
    return order


@router.patch("/{order_id}", response_model=OrderRead)
def update_order(order_id: int, payload: OrderUpdate, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        not_found("淘宝订单")
    if not guarded_bump(session, Order, order_id, payload.version):
        conflict()
    # 集运单存活校验放在 guarded_bump 之后：此时写事务已开启并持写锁，校验与写入同一事务，
    # 闭合「校验通过 → 集运单被并发软删 → 仍挂上」的 TOCTOU（与 attach_order 的 EXISTS 守卫同效）。
    if "shipment_order_id" in payload.model_fields_set:
        _check_shipment(session, payload.shipment_order_id)

    # price_cny 由物品派生，不接受直接改（订单列表 RMB 只读，改价走物品）
    data = payload.model_dump(exclude_unset=True, exclude={"version", "items", "price_cny"})
    for key, value in data.items():
        setattr(order, key, value)

    # seed 兜底（货款口径）：物品都无单价时用「订单当前价 - 邮费」重播种，避免把邮费摊进单价再被 sync 重复加
    seed = payload.price_cny if payload.price_cny is not None else order.price_cny
    seed_goods = (seed - (order.postage_cny or 0)) if seed is not None else None
    built = None
    if payload.items is not None:            # 给了 items 就整体替换（[] → 自动补 1 条占位）
        built = build_items(payload.items, seed_goods, order.shop)
        order.items = [OrderItem(**d) for d in built]
    elif not order.items:                    # 兜底：历史订单可能无物品 → 补占位，守住「≥1 物品」不变量
        order.items = [OrderItem(**d) for d in build_items([], seed_goods, order.shop)]
    order.sync_from_items()                  # 无论是否改物品：价格恒由物品派生，并按新 fx/override 重算日元
    mirror_to_staging(session, order, built)  # 若由暂存导入：镜像回暂存行，避免删单后重导丢失编辑

    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@router.delete("/{order_id}")
def delete_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        not_found("淘宝订单")
    soft_delete(order)
    session.add(order)
    # 若此单是从暂存导入的：删除后把暂存行的挂靠清掉、状态回「待处理」，使其可重新导入
    # （对齐集运删除时清子订单外键的做法，避免暂存行永远卡在「已导入」且指向已删订单）。
    session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.imported_order_id == order_id)
        .values(imported_order_id=None, status=StagingStatus.pending.value,
                version=OrderStaging.version + 1, updated_at=utcnow())
    )
    session.commit()
    return {"ok": True}
