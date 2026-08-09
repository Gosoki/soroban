"""插件权限（scope）：声明什么、能进哪扇门。

**为什么要有**：在这套东西之前，插件拿到的是一枚**完整用户 JWT**
（`create_access_token(user, 30min)`），能调任何接口——包括删订单、清账本、改数据库连接。
现在它拿到的是一枚只带 `scp` 声明的短期令牌（见 `issue`），由 `main._plugin_gate`
按路由上的 `x-scope` 逐请求核对，**默认拒绝**。

它是本机子进程、代码用户可改，所以这套东西**不是防恶意插件**
（防不住：拿到令牌就能干令牌能干的事）。它防的是：

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
    # baseline = 每枚插件令牌**默认就带**的基础设施权限，不出现在授权勾选框里。
    # 只有「零业务数据、且插件不拿到就没法正确工作」的元数据才配得上这个标记，
    # 详见 _BASELINE 上方的说明。
    baseline: bool = False


# 唯一一张权限表。新增一种能力 = 这里加一条 + 在对应路由上挂 x-scope。
SCOPES: dict[str, ScopeSpec] = {
    # label 是勾选框上的名字，**别往里塞解释**——它要跟在复选框后面排成一列，
    # 塞进整句话就把每一行撑成一根长条、长短不一还扫不出重点。
    # 五个字上下：短到能扫、长到能读懂。要说的话放 hint（悬停才显示，能换行、能限宽）。
    # hint 必须**穷尽**这枚权限实际开出去的门，否则它就是一句让人放心的假话。
    #
    # meta:read 曾经混着两类东西：核心元数据（状态机规则、ingest 契约）和**汇率读取**。
    # 先是把 hint 从「不含任何业务数据」改成「含历史汇率」——那只是让假话变成真话，
    # 混淆本身还在。真正的代价在令牌按 `cmd.needs` 收窄之后暴露出来：
    # 淘宝的 fetch 只声明了 staging 两项，于是拿不到 meta:read，
    # `GET /api/meta/status-rules` 403 → **已导入订单的状态永远同步不上**，
    # 而结果还被记成「挡下 N」这个看起来完全正常的数字。
    # 所以拆开：核心元数据归 meta:read（baseline，人人都有），汇率读取归 fx:read（要授权）。
    "meta:read": ScopeSpec("读核心元数据", "状态机规则、通用写入通道的字段契约。\n零业务数据，每个插件默认持有。",
                           baseline=True),
    "fx:read": ScopeSpec("读汇率", "读当前汇率与**历史汇率**。\n不含订单、集运、暂存等账目数据。"),
    "staging:read": ScopeSpec("读暂存订单", "读暂存订单，用于增量去重、避免重复推送"),
    "staging:write": ScopeSpec("写暂存订单", "新建与更新暂存行。\n不含：导入账本、忽略、删除"),
    # 同理：staging:promote 挂着的不只是 /import，还有 DELETE 暂存行与「忽略」。
    # 只写「导入账本」会让人以为最坏结果是「多了一条账」，实际它能让暂存行**消失**。
    "staging:promote": ScopeSpec("处置暂存单", "把暂存单建成账本单（越过「人工确认」），\n以及忽略、删除暂存行。", risk="high"),
    "orders:read": ScopeSpec("读商品订单", "读商品订单，含金额"),
    "shipment:read": ScopeSpec("读集运单", "读集运单，含金额"),
    "shipment:update": ScopeSpec("改集运单", "修改集运单。\n注意它会顺带盖汇率并重算日元金额。", risk="medium"),
    # 一天可以有多条汇率（每次抓取追加一条），所以这里不再说「每天最多一行」。
    # 写进来的值仍会被核心按 [FX_MIN, FX_MAX] 复核，写不进离谱值。
    "fx:write": ScopeSpec("写入汇率", "记一条汇率。\n值由核心复核区间，写不进离谱数；\n你手填的那条不会被它盖掉。"),
    "data:own": ScopeSpec("插件自有存储", "读写本插件自己的存储，\n与账本隔离，其它插件读不到。"),
}


# **基础设施权限**：每枚插件令牌无条件带上，不出现在授权勾选框里。
#
# 为什么必须无条件带：`/api/plugins/contract` 是「插件自我投影」这个设计的地基
# ——插件启动时问一句「核心支持哪些 kind、各要什么字段」，据此只发核心认识的字段。
# 令牌按 `cmd.needs` 收窄之后，仓库里两个插件四条命令**没有任何一条**把 meta:read
# 写进 needs，于是这条自检接口对所有插件恒 403：地基根本没浇上。
# 而它返回的是纯元数据（kind 名、字段名、批量上限），零业务数据——
# 要求为它单独授权，等于让每个插件作者都去踩一次同样的坑。
#
# 门槛（新增 baseline 项前必须逐条回答「是」）：
#   1. 这条路由**一个字节**账目数据都不返回吗？
#   2. 插件拿不到它就无法正确工作（而不是「少一个便利」）吗？
#   3. 它是只读的吗？
_BASELINE = frozenset(k for k, v in SCOPES.items() if v.baseline)


# 进程内活跃令牌集：jti → 到期单调时刻。任务收割后立即剔除。
# 这是 `auth.py` 明确拒绝过的「令牌版本列 + 迁移」之外唯一便宜的撤销手段；
# 重启即全清，是 fail-closed 的那一侧，符合本项目取向。
_ALIVE: dict[str, float] = {}
_ALIVE_LOCK = threading.Lock()
# 硬上限：清单只能调低，不能调高。**必须严格大于子进程的收割超时**
# （routers/plugins.py 的 _REAP_TIMEOUT，今天是 30 分钟）——取 30 分钟的话，
# 令牌与进程同一刻到期，跑满时长的那次抓取最后一笔回灌照样 401。
_HARD_TTL = dt.timedelta(minutes=40)


def issue(user, plugin_id: str, granted: set[str], timeout_s: int = 600) -> tuple[str, str]:
    """签发插件令牌。返回 (token, jti)。

    TTL 取 `min(timeout+120s, 30min)`——**不能让清单说了算**：一个写着
    `timeout = 604800` 的清单否则就换来一枚一周有效的令牌。
    身份仍然是人（`sub` 不变），所以业务代码里的 `current` 语义与审计链都不断。
    """
    jti = secrets.token_urlsafe(9)
    granted = set(granted) | _BASELINE      # 基础设施权限无条件带上，见 _BASELINE
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
    所以 `tests/test_plugin_paths.py` 有一条断言「展平出来的路由数 > 40」。
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

    **再并上 baseline**：它是这个函数的语义要求，不是顺手加的。
    调用方拿它算两件事——签发令牌用的权限集，以及卡片上「这条命令缺哪几项授权」
    （`blocked = needs - effective`）。baseline 项在 `describe()` 里不出现，
    用户勾不到它 ⇒ 它永远不会进 granted ⇒ 三重交集里永远没有它。
    于是任何把 baseline 项写进 `needs` 的命令，按钮会**永久禁用且无法自救**，
    而 `issue()` 其实照发不误。两处口径必须一致。
    """
    declared = set(manifest.get("scopes") or [])
    granted = set(json.loads(getattr(cfg, "granted_scopes", None) or "[]")) if cfg else set()
    return (declared & granted & set(SCOPES)) | _BASELINE


def describe() -> list[dict]:
    """给前端的授权勾选表。与设置页同一套做法：元信息从后端来，前端不写死第二份。"""
    return [{"key": k, "label": v.label, "hint": v.hint, "risk": v.risk}
            for k, v in SCOPES.items() if not v.baseline]
