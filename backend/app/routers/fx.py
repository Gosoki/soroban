"""汇率查询：当前 CNY→JPY（含历史兜底）。**自动获取由汇率插件负责**，本模块只读。"""

import datetime as dt

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..auth import get_current_user
from ..config import settings
from ..database import get_session
from ..schemas import FxRead
from ..services.fx import JST, SOURCE_LABELS, is_expired, latest_stored, rate_age_hours

router = APIRouter(
    prefix="/api/fx", tags=["fx"], dependencies=[Depends(get_current_user)]
)


def _read(session: Session, row) -> FxRead:
    """FxRate 行 → FxRead。两个端点共用：抄两遍迟早有一边忘了加新字段。"""
    if not row:
        return FxRead(base=settings.FX_BASE, quote=settings.FX_QUOTE)
    return FxRead(
        base=settings.FX_BASE,
        quote=settings.FX_QUOTE,
        rate=row.rate,
        date=row.date,
        stale=row.date < dt.datetime.now(JST).date(),
        source=row.source,
        # 认不出就原样透传裸 key：源标识由插件自定，核心不维护它的中文名
        source_label=SOURCE_LABELS.get(row.source, row.source),
        age_hours=rate_age_hours(row),
        expired=is_expired(session, row),
    )


@router.get("", response_model=FxRead, openapi_extra={"x-scope": "meta:read"})
def get_fx(session: Session = Depends(get_session)):
    return _read(session, latest_stored(session))
