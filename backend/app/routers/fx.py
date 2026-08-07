"""汇率查询：当前 CNY→JPY（含历史兜底）+ 手动刷新。"""

import datetime as dt

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth import get_current_user
from ..config import settings
from ..database import get_session
from ..schemas import FxRead
from ..services import prefs
from ..services.fx import (
    JST, SOURCE_LABELS, latest_stored, refresh_and_store,
)

router = APIRouter(
    prefix="/api/fx", tags=["fx"], dependencies=[Depends(get_current_user)]
)


def _read(session: Session, row) -> FxRead:
    """FxRate 行 → FxRead。两个端点共用：抄两遍迟早有一边忘了加新字段。"""
    if not row:
        return FxRead(base=settings.FX_BASE, quote=settings.FX_QUOTE)
    chain = prefs.load(session)["fx.sources"]
    # 「备用」= 当前这条**不是链上第一个源**产出的。判据跟着配置走，
    # 而不是硬编码某个源名——用户把谷歌调到第一位时，中行才是那个「备用」。
    return FxRead(
        base=settings.FX_BASE,
        quote=settings.FX_QUOTE,
        rate=row.rate,
        date=row.date,
        stale=row.date < dt.datetime.now(JST).date(),
        source=row.source,
        source_label=SOURCE_LABELS.get(row.source, row.source),
        fallback=bool(chain) and row.source != chain[0],
    )


@router.get("", response_model=FxRead)
def get_fx(session: Session = Depends(get_session)):
    return _read(session, latest_stored(session))


@router.post("/refresh", response_model=FxRead)
async def refresh(session: Session = Depends(get_session)):
    return _read(session, await refresh_and_store(session))
