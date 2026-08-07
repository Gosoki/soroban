"""soroban FastAPI app entrypoint."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from . import models  # noqa: F401  确保建表前所有模型已注册
from .config import settings
from .database import checkpoint_and_dispose, create_db_and_tables, wal_checkpoint_loop
from .maintenance import barrier
from .routers import (
    auth, dashboard, dbadmin, fx, items, layout, misc, orders, plugins,
    settings as settings_router, shipment, staging, tags,
)
from .routers.plugins import scheduler_loop
from .services.fx import fx_loop

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
    create_db_and_tables()          # Alembic upgrade head（幂等；旧库自动接管，见 database.py）
    tasks = [
        asyncio.create_task(fx_loop()),
        asyncio.create_task(scheduler_loop()),
        # 控制引擎恒为 SQLite（存 app_db_config），故 WAL 截断循环始终运行
        asyncio.create_task(wal_checkpoint_loop(600)),   # 每 10 分钟截断一次 WAL
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        checkpoint_and_dispose()        # 合并并截断 WAL、关连接池 → 回收 -wal/-shm


app = FastAPI(title="soroban", version="0.1.0", lifespan=lifespan)

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
    dbadmin.router, settings_router.router,
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
