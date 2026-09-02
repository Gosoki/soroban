"""杂项支出 CRUD。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..models.base import not_deleted
from ..models import MiscExpense
from ..schemas import MiscCreate, MiscRead, MiscUpdate
from .common import (
    MAX_OFFSET, guarded_bump, list_totals, raise_conflict, raise_not_found,
                     soft_delete, stamp_fx)

router = APIRouter(
    prefix="/api/misc", tags=["misc"], dependencies=[Depends(get_current_user)]
)


@router.get("")
def list_expenses(
    session: Session = Depends(get_session),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    category: Optional[str] = None,
    q: Optional[str] = Query(None, description="按名称搜索"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
):
    conds = [not_deleted(MiscExpense)]
    if date_from:
        conds.append(MiscExpense.date >= date_from)
    if date_to:
        conds.append(MiscExpense.date <= date_to)
    if category:
        conds.append(MiscExpense.category == category)
    if q:
        conds.append(MiscExpense.name.contains(q, autoescape=True))

    totals = list_totals(session, MiscExpense, conds)
    rows = session.exec(
        select(MiscExpense)
        .where(*conds)
        .order_by(MiscExpense.date.desc(), MiscExpense.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [MiscRead.model_validate(r) for r in rows], **totals}


@router.post("", response_model=MiscRead)
def create_expense(payload: MiscCreate, session: Session = Depends(get_session)):
    expense = MiscExpense(**payload.model_dump())
    stamp_fx(session, expense)
    expense.compute_money()
    session.add(expense)
    session.commit()
    session.refresh(expense)
    return expense


@router.get("/{expense_id}", response_model=MiscRead)
def get_expense(expense_id: int, session: Session = Depends(get_session)):
    expense = session.get(MiscExpense, expense_id)
    if not expense or expense.is_delete:
        raise_not_found("杂项")
    return expense


@router.patch("/{expense_id}", response_model=MiscRead)
def update_expense(expense_id: int, payload: MiscUpdate, session: Session = Depends(get_session)):
    expense = session.get(MiscExpense, expense_id)
    if not expense or expense.is_delete:
        raise_not_found("杂项")
    if not guarded_bump(session, MiscExpense, expense_id, payload.version):
        raise_conflict()

    data = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in data.items():
        setattr(expense, key, value)
    # 幽灵新建行最自然的用法是「先敲名称回车建行 → 再点人民币格填金额」，
    # 金额是在这条 PATCH 里才第一次出现的。不在这里补汇率，这行就永远算不出日元。
    stamp_fx(session, expense)
    expense.compute_money()

    session.add(expense)
    session.commit()
    session.refresh(expense)
    return expense


@router.delete("/{expense_id}")
def delete_expense(expense_id: int, session: Session = Depends(get_session)):
    expense = session.get(MiscExpense, expense_id)
    if not expense or expense.is_delete:
        raise_not_found("杂项")
    soft_delete(expense)
    session.add(expense)
    session.commit()
    return {"ok": True}
