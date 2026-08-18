"""列布局：每个表的列顺序 + 宽度，存后端，所有人/每次渲染一致。"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import upsert
from ..models import ColumnLayout, utcnow
from ..schemas import LayoutRead, LayoutUpdate

router = APIRouter(
    prefix="/api/layout", tags=["layout"], dependencies=[Depends(get_current_user)]
)

_TABLES = {"orders", "shipment", "misc", "staging", "items"}


def _check_table(name: str):
    if name not in _TABLES:
        raise HTTPException(status_code=422, detail=f"未知表名：{name}")


@router.get("/{table_name}", response_model=LayoutRead)
def get_layout(table_name: str, session: Session = Depends(get_session)):
    _check_table(table_name)
    row = session.get(ColumnLayout, table_name)
    cols = json.loads(row.columns_json) if row else []
    return LayoutRead(table_name=table_name, columns=cols)


@router.put("/{table_name}", response_model=LayoutRead)
def put_layout(table_name: str, payload: LayoutUpdate, session: Session = Depends(get_session)):
    _check_table(table_name)
    data = json.dumps([c.model_dump() for c in payload.columns], ensure_ascii=False)
    now = utcnow()
    # 原子 upsert，避免并发首次保存的 read-then-insert 主键冲突
    stmt = upsert(
        session.get_bind(), ColumnLayout,
        {"table_name": table_name, "columns_json": data, "updated_at": now},
        ["table_name"], {"columns_json": data, "updated_at": now},
    )
    session.execute(stmt)
    session.commit()
    return LayoutRead(table_name=table_name, columns=payload.columns)
