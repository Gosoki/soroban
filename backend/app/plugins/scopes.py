"""插件权限（scope）：声明什么、能进哪扇门。

**为什么要有**：插件今天拿到的是一枚完整用户 JWT（`create_access_token(user, 30min)`），
能调任何接口——包括删订单、清账本、改数据库连接。它是本机子进程、代码用户可改，
所以这套东西**不是防恶意插件**（防不住：拿到令牌就能干令牌能干的事）。它防的是：

  1. **误伤**：抓取插件里一个写错的 URL 把 DELETE 发到了 `/api/orders/1`；
  2. **静默扩权**：`git pull` 更新插件后它多要了一项权限，没人察觉；
  3. **说不清**：出问题时无法回答「那一轮插件到底动了什么」。

对用户可见的价值只有一句：**卡片上写着「本插件只能写暂存，不能直接进账本」，而这句话是真的。**
真话的前提是 scope 挂在**路由**上而不是路径前缀上——`POST /api/staging/{id}/import`
在 `/api/staging` 前缀下，干的却是「直接建正式账本单」。按前缀授权的话那句话就是假的。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import threading
import time
from typing import NamedTuple, Optional

from fastapi import Request
from fastapi.routing import APIRoute
from jose import JWTError, jwt
from starlette.routing import Match

from ..config import settings

log = logging.getLogger("soroban.plugins.scopes")


class ScopeSpec(NamedTuple):
    label: str          # 授权勾选框上的名字
    hint: str           # 用户凭它决定给不给
    risk: str = "low"   # low | medium | high —— 高风险项在 UI 上要显眼


# 唯一一张权限表。新增一种能力 = 这里加一条 + 在对应路由上挂 x-scope。
SCOPES: dict[str, ScopeSpec] = {
    "meta:read": ScopeSpec("读状态机规则", "只读，不含任何业务数据"),
    "staging:read": ScopeSpec("读暂存订单", "用于增量去重，避免重复推送"),
    "staging:write": ScopeSpec("写暂存订单", "新建/更新暂存行。**不含**导入账本、忽略、删除"),
    "staging:promote": ScopeSpec("把暂存单导入账本", "越过人工确认闸门直接建账本单", risk="high"),
    "orders:read": ScopeSpec("读商品订单", "含金额"),
    "shipment:read": ScopeSpec("读集运单", "含金额"),
    "shipment:update": ScopeSpec("改集运单", "PATCH 会顺带盖汇率并重算日元金额", risk="medium"),
    "fx:write": ScopeSpec("写入汇率", "每天最多一行，值受核心区间校验，写不进离谱值"),
    "data:own": ScopeSpec("读写本插件自己的存储", "与账本隔离，其它插件读不到"),
}

# 进程内活跃令牌集：jti → 到期单调时刻。任务收割后立即剔除。
# 这是 `auth.py` 明确拒绝过的「令牌版本列 + 迁移」之外唯一便宜的撤销手段；
# 重启即全清，是 fail-closed 的那一侧，符合本项目取向。
_ALIVE: dict[str, float] = {}
_ALIVE_LOCK = threading.Lock()
_HARD_TTL = dt.timedelta(minutes=30)     # 硬上限：清单只能调低，不能调高


def issue(user, plugin_id: str, granted: set[str], timeout_s: int = 600) -> tuple[str, str]:
    """签发插件令牌。返回 (token, jti)。

    TTL 取 `min(timeout+120s, 30min)`——**不能让清单说了算**：一个写着
    `timeout = 604800` 的清单否则就换来一枚一周有效的令牌。
    身份仍然是人（`sub` 不变），所以业务代码里的 `current` 语义与审计链都不断。
    """
    jti = secrets.token_urlsafe(9)
    ttl = min(dt.timedelta(seconds=max(60, timeout_s) + 120), _HARD_TTL)
    now = dt.datetime.now(dt.timezone.utc)
    tok = jwt.encode(
        {"sub": str(user.id), "username": user.username, "typ": "plugin",
         "plg": plugin_id, "scp": sorted(granted), "jti": jti,
         "iat": now, "exp": now + ttl},
        settings.SECRET_KEY, algorithm=settings.ALGORITHM,
    )
    with _ALIVE_LOCK:
        _ALIVE[jti] = time.monotonic() + ttl.total_seconds()
    return tok, jti


def revoke(jti: Optional[str]) -> None:
    """任务结束即作废。插件跑完之后那枚令牌不该还能用 25 分钟。"""
    if not jti:
        return
    with _ALIVE_LOCK:
        _ALIVE.pop(jti, None)


def alive(jti: Optional[str]) -> bool:
    if not jti:
        return False
    with _ALIVE_LOCK:
        exp = _ALIVE.get(jti)
        if exp is None:
            return False
        if exp < time.monotonic():          # 顺手清理过期项，别让表无限长
            _ALIVE.pop(jti, None)
            return False
    return True


def peek_claims(request: Request) -> Optional[dict]:
    """请求带的是不是插件令牌；是就返回它的 claims，否则 None。

    **只解不验签名之外的东西**——真正的身份校验仍在 `auth.get_current_user`。
    这里只是让中间件知道「要不要走 scope 这条路」，人类登录的请求一字不改。
    """
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    try:
        claims = jwt.decode(auth[7:], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None                          # 无效令牌交给 get_current_user 去 401
    return claims if claims.get("typ") == "plugin" else None


def route_scope(app, request: Request) -> tuple[bool, Optional[str]]:
    """(有没有匹配到路由, 该路由声明的 scope)。

    用 Starlette 自己的 `Route.matches` 走一遍真实路由表，**不维护第二份前缀表**——
    第二份表必然漂移，而漂移的方向通常是「权限比以为的大」。
    线性匹配几十条路由，对每分钟几次的插件请求可以忽略。
    """
    for r in _iter_routes(app):
        match, _ = r.matches(request.scope)
        if match is Match.FULL:
            return True, (r.openapi_extra or {}).get("x-scope")
    return False, None


_ROUTES_CACHE: dict[int, list] = {}


def _iter_routes(app) -> list:
    """展平路由表（结果按 app 缓存——路由表启动后不再变）。

    ⚠️ 本版 FastAPI 把 `include_router` 进来的路由包在 `_IncludedRouter` 里，它
    **没有 `.routes`**，子路由挂在 `.original_router` 上。只看 `app.routes` 只能拿到
    `/api/health` 那一条——那会让整个闸门要么全拒（插件什么都干不了）、
    要么全放行（权限形同虚设），取决于默认分支怎么写。两种都很难在测试里看出来，
    所以 `tests/test_plugin_scopes.py` 有一条断言「展平出来的路由数 > 40」。
    """
    key = id(app)
    if key not in _ROUTES_CACHE:
        out, stack = [], list(app.routes)
        while stack:
            r = stack.pop()
            if isinstance(r, APIRoute):
                out.append(r)
                continue
            for attr in ("routes", "original_router"):
                v = getattr(r, attr, None)
                if v is None:
                    continue
                if hasattr(v, "routes"):
                    v = v.routes
                if isinstance(v, (list, tuple)):
                    stack.extend(v)
                    break
        _ROUTES_CACHE[key] = out
    return _ROUTES_CACHE[key]


def token_scopes(manifest: dict, cfg) -> set[str]:
    """实际发给插件的权限 = 清单声明 ∩ 用户授权 ∩ 核心已知。

    三重交集各挡一件事：
      ∩清单   → 用户在界面上多勾了也没用（插件自己都没说要）
      ∩授权   → 插件升级后偷偷多加一项 scope 不会生效，卡片上标「需要新授权」
      ∩已知   → 核心删掉某个 scope 之后，库里存着的旧授权自动失效
    """
    declared = set(manifest.get("scopes") or [])
    granted = set(json.loads(getattr(cfg, "granted_scopes", None) or "[]")) if cfg else set()
    return declared & granted & set(SCOPES)


def describe() -> list[dict]:
    """给前端的授权勾选表。与设置页同一套做法：元信息从后端来，前端不写死第二份。"""
    return [{"key": k, "label": v.label, "hint": v.hint, "risk": v.risk}
            for k, v in SCOPES.items()]
