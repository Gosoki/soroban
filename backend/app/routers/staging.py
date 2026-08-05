"""淘宝抓取暂存（全部淘宝订单）→ 人工确认「导入」才进正式账本。

机器人（将来）只写这里；现在支持手动新建/内联编辑。一单多物用 StagingItem 子表，
结构对齐账本的 Order/OrderItem。导入=从暂存行生成 Order（含全部物品）。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..models import (
    OrderItem,
    Source,
    StagingItem,
    StagingStatus,
    Order,
    OrderStaging,
    OrderStatus,
    utcnow,
)
from ..schemas import StagingCreate, StagingItemRead, StagingRead, StagingUpdate, OrderRead
from .common import build_items, conflict, goods_seed, guarded_bump, not_found

router = APIRouter(
    prefix="/api/staging", tags=["staging"], dependencies=[Depends(get_current_user)]
)

# 已导入行：暂存字段 → 账本字段的映射（单一真源写穿账本；status=导入工作流状态留暂存自身）。
# price_cny 不在此列：它由物品单价×数量派生（见 sync_from_items），改价走物品、不直接写。
_SHARED_TO_ORDER = {
    "order_date": "date",
    "order_no": "order_no",
    "shop": "shop",
    "platform_account": "platform_account",
    "platform": "platform",
    "express_no": "express_no",
    "postage_cny": "postage_cny",
    "fx_rate": "fx_rate",
    "order_status": "status",
}


def _linked_order(session: Session, row: OrderStaging) -> Optional[Order]:
    """已导入且账本订单仍在（未软删）→ 返回该订单，否则 None。"""
    if row.imported_order_id is None:
        return None
    order = session.get(Order, row.imported_order_id)
    return order if order and not order.is_delete else None


def _overlay(row: OrderStaging, order: Optional[Order]) -> StagingRead:
    """已导入行的共享字段用账本的实时值覆盖显示（单一真源，两页永远一致）。"""
    data = StagingRead.model_validate(row)
    if order is not None:
        data.order_date = order.date
        data.order_no = order.order_no
        data.shop = order.shop
        data.platform_account = order.platform_account
        data.express_no = order.express_no
        data.price_cny = order.price_cny
        data.postage_cny = order.postage_cny
        data.fx_rate = order.fx_rate
        data.order_status = order.status
        data.items = [
            StagingItemRead(id=it.id, name=it.name, quantity=it.quantity,
                            price_cny=it.price_cny, auto=it.auto)
            for it in order.items
        ]
    return data


def _read_many(session: Session, rows: list[OrderStaging]) -> list[StagingRead]:
    """批量构造响应：一次 IN 查询取回整页已导入行对应的账本订单（连同物品），
    而不是逐行 session.get + 逐单懒加载 items——一屏 100 行时那是 200 条 SQL。"""
    ids = [r.imported_order_id for r in rows if r.imported_order_id is not None]
    linked: dict[int, Order] = {}
    if ids:
        for o in session.exec(
            select(Order)
            .where(Order.id.in_(ids), Order.is_delete.is_(False))
            .options(selectinload(Order.items))
        ).all():
            linked[o.id] = o
    return [_overlay(r, linked.get(r.imported_order_id)) for r in rows]


def _read(session: Session, row: OrderStaging) -> StagingRead:
    return _overlay(row, _linked_order(session, row))


@router.get("")
def list_staging(
    session: Session = Depends(get_session),
    status: Optional[str] = None,
    platform: Optional[str] = None,
    platform_account: Optional[str] = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    q: Optional[str] = Query(None, description="模糊搜：物品名/商品标题/订单号/快递号"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conds = []
    if status:
        conds.append(OrderStaging.status == status)
    if platform:
        conds.append(OrderStaging.platform == platform)
    if platform_account:
        conds.append(OrderStaging.platform_account == platform_account)
    if date_from:
        conds.append(OrderStaging.order_date >= date_from)
    if date_to:
        conds.append(OrderStaging.order_date <= date_to)
    if q:   # 统一模糊搜：物品名 / 商品标题 / 订单号 / 快递号（物品名用 EXISTS 子查询，不重复行）
        conds.append(
            OrderStaging.order_no.contains(q, autoescape=True)
            | OrderStaging.shop.contains(q, autoescape=True)
            | OrderStaging.express_no.contains(q, autoescape=True)
            | OrderStaging.items.any(StagingItem.name.contains(q, autoescape=True))
        )
    total = session.exec(select(func.count()).select_from(OrderStaging).where(*conds)).one()
    rows = session.exec(
        select(OrderStaging)
        .where(*conds)
        .options(selectinload(OrderStaging.items))   # 一次取回整页的物品，避免每行懒加载(N+1)
        .order_by(OrderStaging.scraped_at.desc(), OrderStaging.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": _read_many(session, rows), "total": total}


@router.post("", response_model=StagingRead)
def create_staging(payload: StagingCreate, session: Session = Depends(get_session)):
    from ..services.fx import rate_for_date  # 局部导入避免循环

    row = OrderStaging(**payload.model_dump(exclude={"items", "price_cny"}))  # 价由物品派生
    if row.fx_rate is None:                  # 按下单日期匹配汇率；无记录则退回当前(入库当天)汇率
        row.fx_rate = rate_for_date(session, row.order_date)
    # 最小单位是物品：至少 1 条。播种用「货款」= 种子价 - 邮费，避免邮费摊进单价再被 sync 重复计
    seed_goods = goods_seed(payload.price_cny, payload.postage_cny)
    row.items = [StagingItem(**d) for d in build_items(payload.items, seed_goods, payload.shop)]
    row.sync_from_items()                    # price_cny = Σ(单价×数量) + 邮费
    session.add(row)
    session.commit()
    session.refresh(row)
    return _read(session, row)


@router.patch("/{row_id}", response_model=StagingRead)
def update_staging(row_id: int, payload: StagingUpdate, session: Session = Depends(get_session)):
    row = session.get(OrderStaging, row_id)
    if not row:
        not_found("暂存记录")
    # 暂存行乐观锁：加载后被爬虫/他人改过 → 409，前端刷新（version 原子自增 + 刷新 updated_at）
    if not guarded_bump(session, OrderStaging, row_id, payload.version):
        conflict()
    data = payload.model_dump(exclude_unset=True, exclude={"items", "version", "price_cny"})  # 价由物品派生
    order = _linked_order(session, row)

    if order is not None:
        # 已导入：共享字段写穿到账本（唯一真源），仅「导入状态」留在暂存自身。
        # 账本侧再走一次乐观锁：原子自增账本 version，让淘宝页也能察觉此次改动。
        if not guarded_bump(session, Order, order.id, order.version):
            conflict()
        for key, value in data.items():
            if key in _SHARED_TO_ORDER:
                setattr(order, _SHARED_TO_ORDER[key], value)
                setattr(row, key, value)   # 暂存行自身原始列也同步，避免 tags._data_values / 列表筛选读到陈旧值
            elif key == "status":
                row.status = value
        if payload.items is not None:                   # 物品写穿账本（单一真源）+ 暂存镜像，两页一致
            # 种子只认本次显式传来的 price_cny，不回退到当前价——理由同 orders.update_order
            seed_goods = goods_seed(payload.price_cny, order.postage_cny)
            built = build_items(payload.items, seed_goods, order.shop)
            order.items = [OrderItem(**d) for d in built]
            row.items = [StagingItem(**d) for d in built]
        order.sync_from_items()                         # 账本价+日元由物品派生（fx 变也重算）
        row.sync_from_items()                           # 暂存价镜像
        session.add(order)
        session.add(row)
        session.commit()                                # order_no 撞账本唯一索引 → IntegrityError → 409
        session.refresh(row)
        return _read(session, row)

    # 未导入：编辑暂存自身副本
    for key, value in data.items():
        setattr(row, key, value)
    if payload.items is not None:                       # 给了 items 就整体替换（[] → 自动补 1 条占位）
        # 种子只认本次显式传来的 price_cny，不回退到当前价——理由同 orders.update_order
        seed_goods = goods_seed(payload.price_cny, row.postage_cny)
        row.items = [StagingItem(**d) for d in build_items(payload.items, seed_goods, row.shop)]
    row.sync_from_items()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _read(session, row)


@router.delete("/{row_id}")
def delete_staging(row_id: int, session: Session = Depends(get_session)):
    row = session.get(OrderStaging, row_id)
    if not row:
        not_found("暂存记录")
    if _linked_order(session, row) is not None:
        # 已导入且账本单仍在：直接删暂存会孤立账本 Order（永远占死 order_no 唯一号、无法再导入）。
        # 要求先在「商品订单」页删掉对应订单，再删此暂存，保持暂存↔账本一致。
        raise HTTPException(
            status_code=409,
            detail="该记录已导入账本，请先在「商品订单」页删除对应订单，再删此暂存记录",
        )
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/{row_id}/ignore", response_model=StagingRead)
def ignore_staging(row_id: int, session: Session = Depends(get_session)):
    # 原子标记忽略：version 在 DB 层自增（而非 Python 读-改-写），避免并发忽略/爬虫写
    # 丢失 version 自增、绕过乐观锁；与 import_staging 的原子门闸同风格。
    # 门闸再加 imported_order_id IS NULL：已导入的行不允许翻成「已忽略」，否则状态=已忽略
    # 却仍挂着活账本单（_read 还会覆盖显示），导致工作流状态与账本发散。
    res = session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.id == row_id, OrderStaging.imported_order_id.is_(None))
        .values(status=StagingStatus.ignored.value,
                version=OrderStaging.version + 1, updated_at=utcnow())
    )
    if res.rowcount != 1:
        row = session.get(OrderStaging, row_id)     # 区分「不存在」与「已导入」
        if row is None:
            not_found("暂存记录")
        raise HTTPException(status_code=409, detail="该记录已导入账本，不能忽略")
    session.commit()
    row = session.get(OrderStaging, row_id)
    return _read(session, row)


@router.post("/{row_id}/import", response_model=OrderRead)
def import_staging(row_id: int, session: Session = Depends(get_session)):
    """从暂存行生成正式淘宝订单（含全部物品），并标记暂存为已导入。"""
    from ..services.fx import rate_for_date  # 局部导入避免循环

    row = session.get(OrderStaging, row_id)
    if not row:
        not_found("暂存记录")
    if row.imported_order_id is not None:
        raise HTTPException(status_code=409, detail="该记录已导入")

    order = Order(
        date=row.order_date or dt.date.today(),
        order_no=row.order_no,
        shop=row.shop,
        platform_account=row.platform_account,
        platform=row.platform,               # 来源随单迁移到账本
        express_no=row.express_no,
        postage_cny=row.postage_cny,         # 邮费随单迁移
        fx_rate=row.fx_rate or rate_for_date(session, row.order_date),  # 优先暂存记录的汇率；否则按下单日期匹配
        status=row.order_status or OrderStatus.paid.value,   # 订单状态一同迁移
        source=Source.imported.value,
    )
    # 物品（含单价/auto）随单迁移；订单价由物品派生（= 暂存价，一致）。暂存无物品时兜底自动生成 1 条。
    if row.items:
        order.items = [OrderItem(name=it.name, quantity=it.quantity, price_cny=it.price_cny, auto=it.auto)
                       for it in row.items]
    else:
        # 0 物品兜底：种子用货款(总价-邮费)，避免 sync 再加邮费重复计（对齐其它 build_items 站点）
        seed_goods = goods_seed(row.price_cny, row.postage_cny)
        order.items = [OrderItem(**d) for d in build_items([], seed_goods, row.shop)]
    order.sync_from_items()
    session.add(order)
    session.flush()                             # 拿到 order.id

    # 原子门闸：只有 imported 仍为空的那次导入能成功，防并发/重复导入建重复单
    claimed = session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.id == row_id, OrderStaging.imported_order_id.is_(None))
        .values(
            status=StagingStatus.imported.value,
            order_status=order.status,          # 快照对齐账本（此后以账本为准，读时覆盖）
            imported_order_id=order.id,
            version=OrderStaging.version + 1,
            updated_at=utcnow(),
        )
    )
    if claimed.rowcount != 1:                    # 被别人抢先导入 → 回滚刚建的订单
        # 显式 rollback：get_session 的 with 块退出时 Session.close() 也会回滚，但那是隐式副作用。
        # 这里刚 flush 过一张 Order，把「不提交」写成代码而不是靠调用方的清理顺序。
        session.rollback()
        raise HTTPException(status_code=409, detail="该记录已导入")

    session.commit()                            # order_no 与账本冲突 → IntegrityError → 全局 409
    session.refresh(order)
    return order
