"""汇率查询：当前 CNY→JPY（含历史兜底）。**自动获取由汇率插件负责**，本模块只读。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, select

from ..auth import get_current_user
from ..config import settings
from ..database import get_session
from ..models import FxRate
from ..schemas import FxRead
from ..services.fx import (
    JST, SOURCE_LABELS, is_expired, latest_stored, pick_on, rate_age_hours, rows_on,
)

router = APIRouter(
    prefix="/api/fx", tags=["fx"], dependencies=[Depends(get_current_user)]
)


def _jst_hm(t: Optional[dt.datetime]) -> str:
    """UTC 时间戳 → JST 的 HH:MM。与 `FxRate.date`（JST 日期）同一时区。"""
    if t is None:
        return ""
    if t.tzinfo is None:                # SQLite 取回可能是 naive，按 UTC 解
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(JST).strftime("%H:%M")


def _auto_provider() -> str:
    """现在有没有装着能写汇率的插件；有就返回它的名字。

    判据是**声明了 `fx:write` 权限**，不是插件 id——核心不认识任何具体插件。
    界面靠它说实话：插件被删掉之后还写着「自动获取由汇率插件负责」，那句话是假的，
    用户点过去会看到一个空的插件页。
    """
    from ..routers.plugins import discover

    try:
        for m in discover():
            if "fx:write" in (m["_m"].scopes or ()):
                return m["_m"].name
    except Exception:                       # noqa: BLE001  发现失败不该让汇率接口炸
        pass
    return ""


def _read(session: Session, row) -> FxRead:
    """FxRate 行 → FxRead。两个端点共用：抄两遍迟早有一边忘了加新字段。"""
    if not row:
        # ⚠️ 这条早退分支同样要带 `auto_provider`：库里一条汇率都没有，
        # 恰恰是最需要告诉用户「有没有插件在供给」的时刻——设置页就是靠它
        # 在「有插件但还没跑」和「压根没装插件」之间说对话。
        # 漏掉的话那句提示永远显示成「没有能自动取汇率的插件」。
        return FxRead(base=settings.FX_BASE, quote=settings.FX_QUOTE,
                      auto_provider=_auto_provider())
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
        auto_provider=_auto_provider(),
    )


@router.get("", response_model=FxRead, openapi_extra={"x-scope": "meta:read"})
def get_fx(session: Session = Depends(get_session)):
    return _read(session, latest_stored(session))


@router.get("/history", openapi_extra={"x-scope": "meta:read"})
def history(days: int = Query(60, ge=1, le=730), session: Session = Depends(get_session)):
    """按天汇总：每天抓了几条、当天**实际采用**的是哪一条、区间是多少。

    「实际采用」用的是 `pick_on`（手填优先、其次当天最后一条）——与建单时**同一个函数**。
    如果这里另算一遍，页面上显示的和账本里真正用的就会是两个数，
    而那种不一致要等到对账才发现。
    """
    since = dt.datetime.now(JST).date() - dt.timedelta(days=days)
    rows = session.exec(
        select(FxRate).where(FxRate.date >= since).order_by(col(FxRate.date).desc())
    ).all()
    by_day: dict[dt.date, list] = {}
    for r in rows:
        by_day.setdefault(r.date, []).append(r)
    out = []
    for d in sorted(by_day, reverse=True):
        same = by_day[d]
        used = pick_on(session, d)
        vals = [x.rate for x in same]
        out.append({
            "date": d, "count": len(same),
            "used": used.rate if used else None,
            "used_source": used.source if used else "",
            "used_label": SOURCE_LABELS.get(used.source, used.source) if used else "",
            "low": min(vals), "high": max(vals),
            "sources": sorted({x.source for x in same}),
        })
    return {"items": out, "days": days}


@router.get("/history/{on}", openapi_extra={"x-scope": "meta:read"})
def history_day(on: dt.date, session: Session = Depends(get_session)):
    """某一天抓到的全部汇率，新的在前；标出实际采用的那条。"""
    rows = rows_on(session, on)
    used = pick_on(session, on)
    return {
        "date": on,
        "items": [{
            "id": r.id, "rate": r.rate, "source": r.source,
            "source_label": SOURCE_LABELS.get(r.source, r.source),
            "fetched_at": r.fetched_at,
            # 抓取时刻按 **JST** 给，和 `date` 同一个时区。
            # `date` 是 JST 日期、`fetched_at` 是 UTC，前端若按浏览器本地渲染，
            # 在非 JST 的机器上会出现「2026-08-07 那天的明细里写着 8/6 22:00」——
            # 同一行里两个时区，没人看得懂。
            "at": _jst_hm(r.fetched_at),
            "used": bool(used and r.id == used.id),
        } for r in rows],
    }
