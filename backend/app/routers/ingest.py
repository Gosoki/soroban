"""通用写入通道的 HTTP 层。

**新增一种数据不经过这个文件**——只写一个 handler 并 `@register(...)`。
本文件只负责：鉴权分发、批量事务边界、逐项回执。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ConfigDict, ValidationError
from sqlmodel import Field, Session, SQLModel

from ..auth import get_current_user
from ..database import get_session
from ..plugins import runlog, scopes
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
    # **人类令牌不能写「插件私有存储」**：这类数据按 plugin_id 分命名空间，
    # 人没有 plugin_id，会落进 `plugin_id="?"` 这个假命名空间；
    # 而读接口（GET /records/{kind}）明确拒绝人类令牌 ⇒ 写进去的东西**谁都读不回来**。
    # 一条 200 OK、库里真的多了行、然后永远拿不出来——比直接报错难查得多。
    # 别的 kind 不受影响：它们写的是账本表，人类本来就该能写（手工补录、排障）。
    if not claims and handler.kind == "plugin.record":
        raise HTTPException(status_code=400, detail=(
            "「插件私有存储」按插件分命名空间，只能由插件令牌写入。"
            "人类登录写进去之后没有任何接口读得回来。"))
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
        first = next(r["message"] for r in results if r["status"] == "rejected")
        log.warning("插件 %s 写入「%s」：%s（被拒 %d 条，首条原因：%s）",
                    plugin_id, handler.label, summary, summary["rejected"], first)
        # **同时记进 runlog**：上面这行只进日志，而用户看的是卡片，
        # 卡片显示的又是插件自报的那句话。一个不看回执的插件会把
        # 「核心一条没写」显示成绿色的「已导入 30 单」。
        # runlog 里这一笔在收尾时会被并进卡片文案，插件说什么都盖不掉。
        runlog.note_rejected(ctx.run, plugin_id, handler.label,
                             summary["rejected"], first)
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

    sp = session.begin_nested()
    try:
        res = handler.apply(session, item, ctx)
        if res.status == "rejected":
            sp.rollback()
        else:
            sp.commit()
            # **只有真的落了库才算「本轮提交过」。** 原先这一句排在 savepoint 之前，
            # 回滚时又不撤销：于是一条被拒的项会把自己的键**毒掉**，
            # 同一批里后面那条同键的有效项直接短路成 `unchanged` +「本轮已提交过同一条」
            # ——而那是**假话**，那个键一个字都没写进去。
            # 更糟的是 `unchanged` 算成功：`summary["rejected"]` 少数一条，
            # `runlog.note_rejected` 也只被告知一次损失，卡片上的拒收数比实际少。
            seen.add(k)
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
def list_records(kind: str,
                 # 必须有下界：`?limit=-1` 在 SQLite 上等于「不限」，在 MySQL 上是语法错 500。
                 # 零消费者的现在改最便宜——一旦有第一个真消费者，这些边界就再也改不动了。
                 limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
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
        .order_by(PluginRecord.key).offset(offset).limit(limit)
    ).all()
    # 写侧收的是 dict、落库是 JSON 字符串，读侧就该还原成 dict——
    # 原样回字符串的话，同一个字段在写/存/读三处是三种类型，插件那边还得自己再解一次。
    def _data(raw):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw                      # 手改坏了也别让整个列表打不开
    return {"items": [{"key": r.key, "data": _data(r.data), "updated_at": r.updated_at}
                      for r in rows]}
