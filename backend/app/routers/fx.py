"""汇率查询：当前 CNY→JPY（含历史兜底）。**自动获取由汇率插件负责**，本模块只读。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, col, select

from ..auth import get_current_user
from ..config import settings
from ..database import get_session
from ..models import FxRate
from ..plugins import scopes
from ..schemas import FxRead
from ..services.fx import (
    JST, SOURCE_LABELS, current_row, is_expired, pick_from, pick_on, rate_age_hours,
    rows_on,
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


def _auto_provider(session: Session) -> tuple[str, str]:
    """汇率现在**真的会**自动更新吗。返回 `(能跑的插件名, 装了但跑不起来的原因)`。

    判据是**声明了 `fx:write` 权限**，不是插件 id——核心不认识任何具体插件。

    「声明了 fx:write」只是第一关。原先就到此为止，于是把汇率插件停用之后，
    设置页仍然写着「自动获取由『X』插件负责」——而它永远不会再跑。
    这条假话很贵：汇率取不到时账本会**继续用兜底值建单**（有值好过没值，且逐行可审计），
    所以「以为它在自动更新」和「知道它停了」之间，用户的行为完全不同。

    四关全过才算数：声明了 → 装好了（venv 在）→ 用户授权了 fx:write → 总开关是开的。
    差一关就把原因说出来，而不是退回「没有能自动取汇率的插件」——那对
    「明明装了」的用户同样是假话。
    """
    from ..models import PluginConfig
    from ..routers.plugins import _python, discover

    blocked = ""
    try:
        for m in discover():
            if "fx:write" not in (m["_m"].scopes or ()):
                continue
            name = m["_m"].name
            cfg = session.get(PluginConfig, m["_m"].id)
            if not _python(m).exists():
                blocked = blocked or f"「{name}」还没装好（缺运行环境）"
                continue
            if "fx:write" not in scopes.token_scopes(m, cfg):
                blocked = blocked or f"「{name}」还没被授权写入汇率"
                continue
            if not (cfg and cfg.enabled):
                blocked = blocked or f"「{name}」已停用"
                continue
            return name, ""
    except Exception:                       # noqa: BLE001  发现失败不该让汇率接口炸
        pass
    return "", blocked


def _read(session: Session, row) -> FxRead:
    """FxRate 行 → FxRead。两个端点共用：抄两遍迟早有一边忘了加新字段。"""
    if not row:
        # ⚠️ 这条早退分支同样要带 `auto_provider`：库里一条汇率都没有，
        # 恰恰是最需要告诉用户「有没有插件在供给」的时刻——设置页就是靠它
        # 在「有插件但还没跑」和「压根没装插件」之间说对话。
        # 漏掉的话那句提示永远显示成「没有能自动取汇率的插件」。
        prov, blocked = _auto_provider(session)
        return FxRead(base=settings.FX_BASE, quote=settings.FX_QUOTE,
                      auto_provider=prov, auto_blocked=blocked)
    return FxRead(
        base=settings.FX_BASE,
        quote=settings.FX_QUOTE,
        rate=row.rate,
        date=row.date,
        not_today=row.date < dt.datetime.now(JST).date(),
        source=row.source,
        # 认不出就原样透传裸 key：源标识由插件自定，核心不维护它的中文名
        source_label=SOURCE_LABELS.get(row.source, row.source),
        age_hours=rate_age_hours(row),
        expired=is_expired(session, row),
        **dict(zip(("auto_provider", "auto_blocked"), _auto_provider(session))),
    )


@router.get("", response_model=FxRead, openapi_extra={"x-scope": "fx:read"})
def get_fx(session: Session = Depends(get_session)):
    # 口径与看板、与建单**同一个函数**（services.fx.current_row）。
    # 这里曾经有一份同名的本地实现，而看板走的是 latest_stored——两处显示不同的数。
    return _read(session, current_row(session))


@router.get("/history", openapi_extra={"x-scope": "fx:read"})
def history(days: int = Query(60, ge=1, le=730), session: Session = Depends(get_session)):
    """按天汇总：每天抓了几条、当天**实际采用**的是哪一条、区间是多少。

    「实际采用」用的是 `pick_on`（手填优先、其次当天最后一条）——与建单时**同一个函数**。
    如果这里另算一遍，页面上显示的和账本里真正用的就会是两个数，
    而那种不一致要等到对账才发现。
    """
    # `days - 1`：区间是**闭**的（`date >= since` 且今天也算一天）。
    # 写 `- days` 会返回 days+1 天——「近 7 天」给出 8 天，看板按它算日均就偏低。
    since = dt.datetime.now(JST).date() - dt.timedelta(days=days - 1)
    # 同时按 fetched_at 倒序：`pick_from` 要求「新的在前」，靠这条 ORDER BY 保证，
    # 分完组后各天的列表天然就是有序的，不用再排一次。
    rows = session.exec(
        select(FxRate).where(FxRate.date >= since)
        .order_by(col(FxRate.date).desc(), col(FxRate.fetched_at).desc())
    ).all()
    by_day: dict[dt.date, list] = {}
    for r in rows:
        by_day.setdefault(r.date, []).append(r)
    out = []
    for d in sorted(by_day, reverse=True):
        same = by_day[d]
        used = pick_from(same)          # 与建单同一个规则，但不再每天多查一次库
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


@router.get("/history/{on}", openapi_extra={"x-scope": "fx:read"})
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
