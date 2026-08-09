"""soroban FastAPI app entrypoint."""

import os
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError

from . import models  # noqa: F401  确保建表前所有模型已注册
from . import single_process
from .config import settings
from .database import (
    _control_url,
    checkpoint_and_dispose,
    create_db_and_tables,
    wal_checkpoint_loop,
)
from .maintenance import barrier
from .routers import (
    auth, dashboard, dbadmin, fx,
    ingest, items, layout, meta, misc, orders, plugins,
    settings as settings_router, shipment, staging, tags,
)
from .routers.plugins import (
    reclaim_stale_runs,
    scheduler_loop,
    seed_bundled_plugins,
    shutdown_plugins,
)
from .services.ingest import load_kinds

# 通用写入通道的 handler 在**模块级**注册：重复注册 / 未知权限 → 导入即炸，
# 与 `services/ingest/__init__.py` 承诺的「启动即炸」一致。
#
# 原先这行挂在只读屏障中间件的第一句（连 GET 和静态资源都走）。注册失败时，
# 失败的 import 不会进 sys.modules → **每个请求都重试、每个请求都 500**，
# 而栈顶是屏障不是注册点，表现成「服务起来了，然后全站 500」。
#
# 也**不放 lifespan**：conftest 的 client 夹具明写不进 lifespan，
# 放那儿会让 KINDS 只在测试里手动 load 过的地方存在，新测试忘了就收 400 unknown_kind。
load_kinds()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("soroban")

_INSECURE_KEYS = {"", "dev-insecure-key-change-me", "change-me-to-a-long-random-string"}


def _check_secret_key() -> None:
    """SECRET_KEY 是默认值/过短 → **拒绝启动**，而不是打个警告继续跑。

    为什么必须 fail-closed：这个 key 同时用来签发登录 JWT 与加密数据库连接串，而默认值就写在
    公开仓库里。只要它还是默认值，任何人都能自己签一个 `{"sub":"1"}` 的令牌拿到管理员权限
    ——auth.get_current_user 只验签名，没有服务端会话状态可兜底。
    这类问题也**没法登录进来再修**（令牌本身就不可信），所以不适合像「默认密码」那样只告警。

    正常路径都不会踩到：start.sh / start.bat / run.py 首启都会生成含随机 key 的 .env。
    """
    key = settings.SECRET_KEY
    if key in _INSECURE_KEYS or len(key) < 16:
        raise RuntimeError(
            "SECRET_KEY 是不安全的默认值或过短，拒绝启动。\n"
            "  它用于签发登录令牌与加密数据库连接串——保持默认值等于任何人都能伪造管理员身份。\n"
            "  解决：删掉 .env 让程序重新生成，或手动在 .env 里写一行\n"
            "    SECRET_KEY=<python -c \"import secrets;print(secrets.token_hex(32))\" 的输出>"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_secret_key()
    # 单进程闸：排在**建表之前**。多 worker 时后来的那些会在这里当场退出，
    # 而不是先各自跑一遍迁移（幂等归幂等，但那是几个进程同时 ALTER 同一个库）。
    single_process.acquire(_control_url())
    create_db_and_tables()          # Alembic upgrade head（幂等；旧库自动接管，见 database.py）
    # 打包版：把 exe 里自带的插件释放到运行目录（源码运行是空操作）。
    # 必须排在定时循环之前——那个循环起来就会去发现插件并按 schedule 触发。
    seed_bundled_plugins()
    # 建表**之后**、放定时循环之前：库里若还留着上一条命的「执行中」，此刻收掉。
    # 进程刚起来，在飞的插件必然是零个，所以这一刻的判据最干净。
    reclaim_stale_runs()
    tasks = [
        asyncio.create_task(scheduler_loop()),
        # 控制引擎恒为 SQLite（存 app_db_config），故 WAL 截断循环始终运行
        asyncio.create_task(wal_checkpoint_loop(600)),   # 每 10 分钟截断一次 WAL
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        # **先收子进程再关库**：插件可能正在通过 HTTP 回灌，而回灌走的是同一个连接池。
        # 顺序反了的话，那些请求会撞上一个已经 dispose 的池，日志里刷一片无意义的异常。
        shutdown_plugins()              # 不做这件事，浏览器会变成 PPID=1 的孤儿永久留着
        checkpoint_and_dispose()        # 合并并截断 WAL、关连接池 → 回收 -wal/-shm
        single_process.release()        # 松锁排最后：松早了下一个实例会撞上还没关完的库


# 交互式文档：**只在只监听环回时默认开**。
# /docs、/redoc、/openapi.json 都不需要登录，把完整 API 面（含每条路由的插件 scope 标注）
# 摆出来；绑到 0.0.0.0 时那就是给整个局域网看的一张地图。
# 环回下没有这个问题，而它平时很有用，所以不是一刀切关掉。
# 想在局域网上也开，显式设 SOROBAN_DOCS=1。
_LAN = os.environ.get("HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1")
_DOCS = os.environ.get("SOROBAN_DOCS", "").strip().lower() in ("1", "true", "yes") or not _LAN

app = FastAPI(title="soroban", version="0.1.0", lifespan=lifespan,
              docs_url="/docs" if _DOCS else None,
              redoc_url="/redoc" if _DOCS else None,
              openapi_url="/openapi.json" if _DOCS else None)
if not _DOCS:
    log.info("HOST=%s（非环回）→ 已关闭 /docs 与 /openapi.json。要开：SOROBAN_DOCS=1",
             os.environ.get("HOST"))

# 数据库迁移期间只读：拷贝没有读快照，期间写入会产生撕裂的拷贝（详见 app/maintenance.py）。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 迁移/切换端点自己不能被自己拦住；登录也放行，否则屏障期间连进来看一眼状态都做不到
# （登录只读 user 表 + 进程内计数，不写业务数据）。
_ALLOWED_WHILE_READONLY = ("/api/db/", "/api/auth/login")


@app.middleware("http")
async def _readonly_barrier(request: Request, call_next):
    """写请求在只读屏障期间一律 503。

    用中间件而不是给每个端点挂依赖：将来新增的写端点**自动**被覆盖，不会有人忘了加。
    """
    path = request.url.path
    if request.method in _SAFE_METHODS or path.startswith(_ALLOWED_WHILE_READONLY):
        return await call_next(request)
    reason = barrier.begin_write()          # 原子：查屏障 + 登记在飞写
    if reason is not None:
        return JSONResponse(
            status_code=503,
            content={"detail": f"{reason}，此期间暂停写入，请稍后重试"},
            headers={"Retry-After": "10"},
        )
    try:
        return await call_next(request)
    finally:
        barrier.end_write()


@app.middleware("http")
async def _plugin_gate(request: Request, call_next):
    """插件令牌只能进它声明过、且被用户授权的那扇门。**默认拒绝。**

    与只读屏障同一条理由：用中间件而不是逐端点挂依赖，将来新增的端点自动被覆盖。
    区别是这里默认**关着**——新端点不显式挂 `x-scope`，插件就进不去，
    不会因为谁加了个 router 而悄悄扩大插件的权限面。

    注册在只读屏障之后 → 运行时在它**外层**：越权请求在 403 时就被挡掉，
    不会先去占一个「在飞写」计数。人类登录的请求一字不改地穿过去。
    """
    from .plugins import scopes as _scopes

    claims = _scopes.peek_claims(request)
    if claims is None:
        return await call_next(request)
    if not _scopes.alive(claims.get("jti")):
        return JSONResponse(status_code=401, content={"detail": "插件令牌已失效（任务已结束）"})
    matched, need = _scopes.route_scope(request.app, request)
    granted = set(claims.get("scp") or [])
    # 通用写入通道一条路由服务多种数据，权限由 kind 决定 → 这里只验「有权限总量」，
    # 真正的判定在 routers/ingest.py 里按 kind 做。
    ok = matched and need is not None and (need in granted or (need == "*by-kind*" and granted))
    if not ok:
        log.warning("插件 %s(run=%s) 越权：%s %s 需要 %s，持有 %s",
                    claims.get("plg"), claims.get("jti"),
                    request.method, request.url.path, need, sorted(granted))
        return JSONResponse(status_code=403, content={"detail": "插件无权访问该接口"})
    request.state.plugin_scope_ok = True
    return await call_next(request)


# 令牌走 Authorization 头、不使用 cookie，故 allow_credentials=False（更安全）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (
    auth.router, orders.router, shipment.router, misc.router, items.router,
    staging.router, dashboard.router, fx.router, layout.router, tags.router, plugins.router,
    dbadmin.router, settings_router.router, meta.router, ingest.router,
):
    app.include_router(r)


@app.exception_handler(IntegrityError)
async def _integrity_handler(request: Request, exc: IntegrityError):
    # 数据库完整性冲突（唯一约束/外键/必填等）→ 干净的 409，而非 500。
    # 真实约束记进日志（前端只给通用提示，不臆断具体原因，避免误导排查方向）。
    log.warning("IntegrityError on %s %s: %s", request.method, request.url.path, exc.orig)
    return JSONResponse(
        status_code=409,
        content={"detail": "数据完整性冲突（唯一约束/外键/必填），请检查后重试"},
    )


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    # 业务层校验（如 compute_money 的金额上限）抛的 ValueError → 干净的 422，而非 500。
    # 注意：请求体解析阶段的 pydantic 校验走 RequestValidationError，不经这里；这里只兜住
    # 逃逸到 ASGI 层的 ValueError。仍记日志，避免把真正的代码 bug 悄悄伪装成「输入错误」。
    log.warning("ValueError on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# MySQL 的并发写冲突。**按 errno 判，不按文案判**：
#   1213 ER_LOCK_DEADLOCK      —— 死锁，服务端已经回滚了其中一方
#   1205 ER_LOCK_WAIT_TIMEOUT  —— 等锁超时（innodb_lock_wait_timeout，默认 50s）
# 两者都是「有人跟你抢同一批行」，重试就能过去；语义与乐观锁冲突同类。
_RETRYABLE_MYSQL = {1213, 1205}


@app.exception_handler(OperationalError)
async def _deadlock_handler(request: Request, exc: OperationalError):
    """MySQL 死锁 / 等锁超时 → 409（前端已有成熟处理），其余 OperationalError 照旧 500。

    **为什么不能全部 OperationalError 都转 409**：这个异常类是个大杂烩，
    「连接断了」「库不存在」「权限不足」也走它。把连接断开报成「数据已变，请刷新重试」
    会让用户一直刷新一个根本连不上的库——那比裸 500 更误导。
    所以按 errno 精确挑出可重试的那两个，剩下的**原样抛出去**走默认 500 路径。

    409 是刻意的：前端对它已经有一整套处理（提示「数据已变」+ 重新拉取），
    而死锁的正确用户动作恰好就是「再试一次」。挂在这里而不是逐个端点 try/except——
    与只读屏障同一条理由：将来新增的写端点自动被覆盖，不会有人忘了加。
    """
    # 取 errno 必须防空：驱动层的异常不保证带 args（SQLite 那些就不带），
    # 直接 `args[0]` 会在 handler 里抛 IndexError——异常处理器自己炸掉，
    # 用户看到的是一个完全无关的 500，而真正的原因彻底消失。
    args = getattr(getattr(exc, "orig", None), "args", ()) or ()
    code = args[0] if args else None
    if code not in _RETRYABLE_MYSQL:
        raise exc                       # 不是并发冲突：交给默认处理，别伪装成业务冲突
    log.warning("MySQL 并发写冲突(errno=%s) on %s %s", code, request.method, request.url.path)
    return JSONResponse(
        status_code=409,
        content={"detail": "另一个操作正在改同一批数据（数据库死锁），已回滚，请重试"},
    )


@app.exception_handler(SATimeoutError)
async def _pool_timeout_handler(request: Request, exc: SATimeoutError):
    """连接池被榨干 → 503 + Retry-After，而不是裸 500。

    池满是**过载**（一堆慢请求同时攥着连接，见 database.py 的 _POOL_SIZE），不是 bug。
    漏成 500 的话前端只会说「服务器错误」，用户会以为数据坏了而不是「等一会儿再试」。
    """
    log.warning("数据库连接池耗尽 on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "服务器繁忙（数据库连接已满），请稍后重试"},
        headers={"Retry-After": "5"},
    )


@app.get("/api/health", tags=["health"])
def health():
    return {"ok": True}


# 生产托管：若前端已 `npm run build` 出 frontend/dist，则由后端**同源**托管静态文件。
# 挂在最后 → /api/* 仍走上面的路由；其余路径回退到 SPA（index.html）。这样生产只需跑
# 一个 uvicorn（不用再单独起 vite，也无跨域），dev 时没有 dist 目录则跳过、照旧用 vite。
from pathlib import Path  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

# 打包后前端 frontend/dist 打入 _MEIPASS；源码运行时取仓库里的 frontend/dist。
_DIST = (
    Path(sys._MEIPASS) / "frontend" / "dist"        # type: ignore[attr-defined]
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)
class _SpaStatic(StaticFiles):
    """带正确缓存策略的静态托管。

    **为什么必须自己设 Cache-Control**：StaticFiles 只给 etag/last-modified，不给
    Cache-Control。浏览器对这种响应会**启发式缓存**——于是前端每次 `npm run build`，
    资源文件名的哈希都变了，而老用户拿到的还是缓存里的旧 `index.html`，
    它引用的旧哈希文件已经不存在 → 一连串 404 → **整站白屏**，且刷新也不一定好
    （得硬刷新才绕过缓存）。这不是理论风险，本项目就这么白过一次。

    策略：
      · `index.html` —— `no-cache`：每次都回源校验（有 etag，没变就 304，不费流量）。
        它是**唯一**记录着「当前该加载哪些哈希文件」的地方，必须永远最新。
      · `/assets/*` —— 一年 + `immutable`：文件名里带内容哈希，内容变了名字就变，
        所以旧文件永远不会被复用，缓存多久都安全。
    """

    async def get_response(self, path: str, scope):
        resp = await super().get_response(path, scope)
        if path.startswith("assets/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


if _DIST.is_dir():
    app.mount("/", _SpaStatic(directory=str(_DIST), html=True), name="frontend")
    log.info("已挂载前端静态文件（生产同源托管）：%s", _DIST)
