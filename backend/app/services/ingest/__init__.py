"""通用写入通道：插件把数据交给核心的唯一新增入口。

**这是「以后不用再开新接口」的落点。** 加一种新数据（快递轨迹、比价快照、税费…）=
写一个 handler 并 `@register(...)`：不新增路由、不改插件客户端的形状、前端插件卡片零改动。

**不是**用来替换已经能工作的 REST：淘宝插件继续走 `/api/staging`（那条路由上的
per-route scope 已经能精确约束它）。通道存在是为了新种类不必新开路由，
以及让「核心自己落库」与「插件落库」走同一把尺子。

三条铁律（各有守卫，见 tests/test_plugin_paths.py）：

1. **handler 只能 add/flush，禁止 commit / delete / 裸 SQL。**
   路由层统一提交，每一项包一个 `begin_nested()` savepoint。
   这条不成立会**静默写入已被拒绝的数据**：handler 内部一 commit 就击穿外层 savepoint，
   `Outcome("rejected")` 照常回给插件，而那一行其实已经落库——回执与事实相反，最难查。
   （`fx` 早年那个自己 commit 的写入函数正是这个形状，已拆成 `store()` + 显式提交，
   要连提交一起的走 `store_and_commit()`——名字里就写着它会提交。）
2. **handler 必须落在与人手 API 同一套模型方法上**（`fx.store` / `sync_from_items` /
   `compute_money` / `_q_fx`），不另写一份 SQL。两份写入逻辑必然漂移，
   而漂移的表现是「同一笔钱经不同入口算出两个数」。
3. **入参 schema 一律 `extra="forbid"`**，与 `schemas.py` 的家法一致。
   「插件比核心新」的兼容由插件侧按 `GET /api/plugins/contract` 自我投影解决，
   不靠核心默默丢字段——那正是「200 OK + 什么都没写」的来源。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, NamedTuple, Optional

from sqlmodel import Session, SQLModel

from ...plugins.scopes import SCOPES

log = logging.getLogger("soroban.ingest")


class Outcome(NamedTuple):
    """一项的处理结果。`rejected` 必须带 code+message——插件要靠它自我诊断。"""
    status: str                      # created | updated | unchanged | rejected
    id: Optional[int] = None
    code: str = ""                   # validation | conflict | not_found | internal
    message: str = ""


class Ctx(NamedTuple):
    """handler 能看到的上下文。刻意很窄：给多了 handler 就会去碰不该碰的东西。"""
    plugin_id: str
    run: str                         # 本轮的 jti，用于同轮去重与日志追溯


class Handler(NamedTuple):
    kind: str
    label: str
    scope: str                       # 必须是 SCOPES 里的一项——权限只有一张表
    schema: type[SQLModel]
    key: Callable[[Any], str]        # 天然键，**只用于同一轮内去重**
    apply: Callable[[Session, Any, Ctx], Outcome]
    max_batch: int = 200


KINDS: dict[str, Handler] = {}


def register(*, kind: str, label: str, scope: str, schema, key, max_batch: int = 200):
    """注册一种可写入的数据。重复注册/未知权限 → **启动即炸**，不静默覆盖。

    启动即炸是刻意的：一个被悄悄覆盖的 handler，表现是「插件写进去的数据被另一套逻辑解释了」，
    而两套逻辑通常只在边界情况下才不同——那种 bug 会在账本里潜伏很久。
    """
    def deco(fn):
        if kind in KINDS:
            raise RuntimeError(f"重复注册 ingest kind：{kind}")
        if scope not in SCOPES:
            raise RuntimeError(f"{kind} 声明了未知权限 {scope}（权限表见 app/plugins/scopes.py）")
        KINDS[kind] = Handler(kind=kind, label=label, scope=scope, schema=schema,
                              key=key, apply=fn, max_batch=max_batch)
        return fn
    return deco


def contract() -> dict:
    """插件启动自检用：核心支持哪些 kind、各自要什么字段、一批最多多少条。

    插件据此**自我投影**（只发核心认识的字段），这是已被验证过的做法：
    爬虫的 `_PUSH_FIELDS` 白名单就是这个思路，只是原先写死在插件里、
    核心改了字段名它不知道。改成从这里取，跨仓字段漂移就变成插件自己能发现的事。
    """
    return {
        "api": 1,
        "kinds": {
            k: {"label": h.label, "scope": h.scope, "max_batch": h.max_batch,
                "fields": sorted(h.schema.model_fields)}
            for k, h in sorted(KINDS.items())
        },
    }


def load_kinds() -> None:
    """导入所有 handler 模块，触发注册。

    显式导入而不是自动扫目录：扫目录会让「文件名写错 → 这种数据永远写不进来」
    变成一个没有任何报错的失败，而 handler 注册失败恰恰是那种要立刻知道的事。
    """
    from .kinds import fx_rate, plugin_record  # noqa: F401
