"""通用写入通道的 HTTP 层。

**新增一种数据不经过这个文件**——只写一个 handler 并 `@register(...)`。
本文件只负责：鉴权分发、批量事务边界、逐项回执。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ConfigDict, ValidationError
from sqlmodel import Field, Session, SQLModel

from ..auth import get_current_user
from ..database import get_session
from ..plugins import scopes
from ..services import ingest

log = logging.getLogger("soroban.ingest")

router = APIRouter(prefix="/api/plugins", tags=["plugins"],
                   dependencies=[Depends(get_current_user)])


class IngestIn(SQLModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=64)
    items: list[dict] = []


@router.get("/contract", openapi_extra={"x-scope": "meta:read"})
def get_contract():
    """插件启动自检：核心支持哪些 kind、各要什么字段、一批最多多少条。

    插件据此**自我投影**（只发核心认识的字段）。这比「核心默默丢掉不认识的字段」好：
    后者就是「200 OK + 什么都没写 + 零日志」的来源。
    """
    return ingest.contract()


@router.post("/ingest", openapi_extra={"x-scope": "*by-kind*"})
def post_ingest(payload: IngestIn, session: Session = Depends(get_session),
                current=Depends(get_current_user)):
    """插件把一批数据交给核心。

    权限**由 kind 决定**，不是由路由决定——所以这条路由的 `x-scope` 是个哨兵值，
    真正的判定在下面（中间件放行本路由，由这里按 kind 二次鉴权）。
    这么设计是因为一条路由要服务 N 种数据，而每种数据的风险不同：
    写汇率和写插件私有数据不该是同一份授权。
    """
    handler = ingest.KINDS.get(payload.kind)
    if handler is None:
        raise HTTPException(status_code=400, detail={
            "code": "unknown_kind", "known": sorted(ingest.KINDS)})
    if len(payload.items) > handler.max_batch:
        raise HTTPException(status_code=413, detail={
            "code": "batch_too_large", "max": handler.max_batch})

    claims = getattr(current, "_plugin_claims", None) or {}
    granted = set(claims.get("scp") or [])
    plugin_id = claims.get("plg") or "?"
    if claims and handler.scope not in granted:
        # 人类登录（无 claims）不受此限：他本来就有全部权限，走这条通道只是图方便。
        raise HTTPException(status_code=403, detail=(
            f"插件无权写入「{handler.label}」（需要 {handler.scope}，"
            f"当前持有 {sorted(granted)}）"))

    ctx = ingest.Ctx(plugin_id=plugin_id, run=claims.get("jti") or "manual")
    results: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(payload.items):
        results.append(_one(session, handler, raw, ctx, i, seen))
    session.commit()

    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    if summary.get("rejected"):
        log.warning("插件 %s 写入「%s」：%s（被拒 %d 条，首条原因：%s）",
                    plugin_id, handler.label, summary, summary["rejected"],
                    next(r["message"] for r in results if r["status"] == "rejected"))
    return {"kind": handler.kind, "summary": summary, "results": results}


def _one(session: Session, handler, raw: Any, ctx, index: int, seen: set) -> dict:
    """处理一项。**每项一个 savepoint**：一条坏数据不该让整批回滚，
    也不该让它自己的半截写入留在库里。"""
    out = {"index": index, "status": "rejected", "id": None, "code": "", "message": ""}
    try:
        item = handler.schema.model_validate(raw)
    except ValidationError as e:
        out["code"], out["message"] = "validation", _first_error(e)
        return out

    k = handler.key(item)
    if k in seen:
        # 只在**同一轮内**去重。刻意不做跨轮的落库收据：那会造成永久丢失——
        # 用户删掉一行之后，下一轮抓到同一条、payload 逐字节相同 → 命中收据 →
        # 回 unchanged 带一个已不存在的 id → 这条数据再也不会出现。
        out["status"], out["message"] = "unchanged", "本轮已提交过同一条"
        return out
    seen.add(k)

    sp = session.begin_nested()
    try:
        res = handler.apply(session, item, ctx)
        if res.status == "rejected":
            sp.rollback()
        else:
            sp.commit()
        out.update(status=res.status, id=res.id, code=res.code, message=res.message)
    except Exception as e:                                  # noqa: BLE001
        sp.rollback()
        log.exception("ingest %s 第 %d 项失败", handler.kind, index)
        out["code"], out["message"] = "internal", str(e)[:200]
    return out


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg', '校验失败')}" if loc else err.get("msg", "校验失败")


@router.get("/records/{kind}", openapi_extra={"x-scope": "data:own"})
def list_records(kind: str, limit: int = 200, offset: int = 0,
                 session: Session = Depends(get_session),
                 current=Depends(get_current_user)):
    """读本插件自己存的数据。**只回自己的**——命名空间隔离在这里落实。"""
    from sqlmodel import select

    from ..models import PluginRecord

    claims = getattr(current, "_plugin_claims", None) or {}
    plugin_id: Optional[str] = claims.get("plg")
    if not plugin_id:
        raise HTTPException(status_code=400, detail="该接口只供插件调用（需插件令牌）")
    rows = session.exec(
        select(PluginRecord)
        .where(PluginRecord.plugin_id == plugin_id, PluginRecord.kind == kind)
        .order_by(PluginRecord.key).offset(offset).limit(min(limit, 1000))
    ).all()
    return {"items": [{"key": r.key, "data": r.data, "updated_at": r.updated_at} for r in rows]}
