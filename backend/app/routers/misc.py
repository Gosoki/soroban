"""杂项支出 CRUD。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..models import MiscExpense
from ..schemas import MiscCreate, MiscRead, MiscUpdate
from .common import guarded_bump, raise_conflict, raise_not_found, soft_delete

router = APIRouter(
    prefix="/api/misc", tags=["misc"], dependencies=[Depends(get_current_user)]
)


@router.get("")
def list_items(
    session: Session = Depends(get_session),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    category: Optional[str] = None,
    q: Optional[str] = Query(None, description="按名称搜索"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conds = [MiscExpense.is_delete.is_(False)]
    if date_from:
        conds.append(MiscExpense.date >= date_from)
    if date_to:
        conds.append(MiscExpense.date <= date_to)
    if category:
        conds.append(MiscExpense.category == category)
    if q:
        conds.append(MiscExpense.name.contains(q, autoescape=True))

    total = session.exec(select(func.count()).select_from(MiscExpense).where(*conds)).one()
    rows = session.exec(
        select(MiscExpense)
        .where(*conds)
        .order_by(MiscExpense.date.desc(), MiscExpense.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [MiscRead.model_validate(r) for r in rows], "total": total}


@router.post("", response_model=MiscRead)
def create_item(payload: MiscCreate, session: Session = Depends(get_session)):
    from ..services.fx import current_rate  # 局部导入避免循环

    item = MiscExpense(**payload.model_dump())
    if item.price_cny is not None and item.fx_rate is None:
        # 有人民币价却没填汇率 → 补当天汇率（对齐集运 create）。否则 compute_money 算不出
        # jpy_auto/jpy_settled，该笔支出会静默不计入看板合计、结算列空白。
        item.fx_rate = current_rate(session)
    item.compute_money()
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.get("/{item_id}", response_model=MiscRead)
def get_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(MiscExpense, item_id)
    if not item or item.is_delete:
        raise_not_found("杂项")
    return item


@router.patch("/{item_id}", response_model=MiscRead)
def update_item(item_id: int, payload: MiscUpdate, session: Session = Depends(get_session)):
    item = session.get(MiscExpense, item_id)
    if not item or item.is_delete:
        raise_not_found("杂项")
    if not guarded_bump(session, MiscExpense, item_id, payload.version):
        raise_conflict()

    data = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in data.items():
        setattr(item, key, value)
    item.compute_money()

    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete("/{item_id}")
def delete_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(MiscExpense, item_id)
    if not item or item.is_delete:
        raise_not_found("杂项")
    soft_delete(item)
    session.add(item)
    session.commit()
    return {"ok": True}
