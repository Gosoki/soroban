"""Database engine + session。

双引擎模型：
- **控制引擎**（_control_engine）：始终指向 SQLite（soroban.db），保存 app_db_config
  （当前后端 + 加密的 MySQL 连接串）。永不删除、只留系统配置。
- **数据引擎**（_data_engine）：业务数据实际所在，SQLite 或 MySQL，由 app_db_config 决定；
  可在运行期**热切换**（迁移到 MySQL 后无需重启）。

所有业务代码通过 get_session() / get_engine() 取当前数据引擎——故热切换后自动生效。
SQLite 模式下数据引擎即复用控制引擎（同一 soroban.db）。
建表/迁移走 Alembic（见 backend/alembic/、README「数据库」章）。
"""

import asyncio
import logging
import sys
from pathlib import Path
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import settings
from .db import control

log = logging.getLogger("soroban.db")
# backend/（alembic.ini 所在）；PyInstaller 打包后 alembic.ini/alembic/ 打入 _MEIPASS 根。
_ROOT = (
    Path(sys._MEIPASS)                              # type: ignore[attr-defined]
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)


# 连接池必须**显式**配，别吃 SQLAlchemy 的默认值（QueuePool 5 + 10 = 15 条）。
#
# 为什么 15 条不够：`get_current_user` 依赖 `get_session`，而 FastAPI 的生成器依赖
# 一直开到**响应发完**。也就是说每个在飞请求都攥着一条连接，包括那些一个字节 DB 都不碰的
# ——OCR 请求在整段推理期间（秒级）占着一条。而同步路由跑在 anyio 的默认线程池上，
# 它有 40 个令牌。于是并发 40 路 OCR 时：40 个线程去抢 15 条连接，25 个堵在
# `get_current_user` 里等池，实测响应时间从 7ms 涨到 6220ms（**174 倍**），
# 30 秒 `pool_timeout` 之后抛 sqlalchemy.exc.TimeoutError。
# 对齐到 40 之后同一场景实测回到 7ms。
#
# 这是「任何在 session 生命周期内做慢活的路由」共同的地雷（插件执行、爬虫、汇率抓取都算），
# 所以池要按**线程池令牌数**配，而不是按「估计有几个人同时用」。
_POOL_SIZE, _MAX_OVERFLOW, _POOL_TIMEOUT = 20, 20, 30      # 20+20 = 40 = anyio 默认线程令牌数


def build_engine(url: str) -> Engine:
    """按方言构造 engine。
    - SQLite：check_same_thread=False（FastAPI 多线程共用连接池）。
    - MySQL：pool_pre_ping 防死连接（wait_timeout 掐断），pool_recycle 定期回收。
    - 两支都显式配连接池，理由见 _POOL_SIZE 上方。"""
    if url.startswith("sqlite"):
        # 内存库（`sqlite://` / `sqlite:///:memory:`）用的是 SingletonThreadPool，
        # 它**不接受** max_overflow/pool_timeout —— 传了会在 create_engine 就 TypeError。
        # 文件库才是 QueuePool。判据用「有没有文件路径」，别按 URL 前缀一刀切。
        memory = url in ("sqlite://", "sqlite:///:memory:") or url.endswith(":memory:")
        pool_kw = {} if memory else {
            "pool_size": _POOL_SIZE, "max_overflow": _MAX_OVERFLOW, "pool_timeout": _POOL_TIMEOUT,
        }
        return create_engine(url, connect_args={"check_same_thread": False}, **pool_kw)
    # 三个超时都要设：`connect_timeout` 只管 TCP 建连，**不管握手之后的读**。
    # 对着「接受 TCP 却不说 MySQL 握手」的对端（有状态防火墙/NAT、卡死的服务端），
    # pymysql 的 read_timeout 默认 None → 单次调用实测阻塞 384 秒不返回。
    # 后台循环把这种阻塞带进事件循环时，整站会跟着周期性卡死。
    #
    # 注意 MySQL 侧的 `max_connections`：这里最多开 40 条，多实例部署时要乘以实例数。
    return create_engine(
        url, pool_pre_ping=True, pool_recycle=3600,
        pool_size=_POOL_SIZE, max_overflow=_MAX_OVERFLOW, pool_timeout=_POOL_TIMEOUT,
        connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30},
    )


def _control_url() -> str:
    """控制/配置存储始终是 SQLite；.env 的 DATABASE_URL 仅用来定位 sqlite 文件。"""
    url = settings.DATABASE_URL
    return url if url.startswith("sqlite") else "sqlite:///./soroban.db"


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """每条新 SQLite 连接都开 WAL + 外键约束（MySQL 连接自动跳过）。"""
    if isinstance(dbapi_connection, SQLite3Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# --- 引擎初始化：控制引擎恒 SQLite；数据引擎按配置解析 ---------------------------
_control_engine: Engine = build_engine(_control_url())
control.ensure_schema(_control_engine)          # 保证 app_db_config 表存在


def _resolve_data_engine() -> tuple[Engine, str]:
    cfg = control.read_config(_control_engine)
    if cfg["backend"] == "mysql" and cfg["mysql_url"]:
        return build_engine(cfg["mysql_url"]), cfg["mysql_url"]
    return _control_engine, _control_url()      # SQLite 模式复用控制引擎


_data_engine, _data_url = _resolve_data_engine()


def get_engine() -> Engine:
    """当前**数据**引擎（热切换后返回新引擎）。"""
    return _data_engine


def control_engine() -> Engine:
    """控制引擎（恒 SQLite，存 app_db_config；也是「本地 SQLite」这个数据后端本体）。"""
    return _control_engine


def control_url() -> str:
    """本地 SQLite 的连接串（= 控制库）。切回 SQLite 时作为数据引擎 url。"""
    return _control_url()


def current_backend() -> str:
    return "mysql" if _data_engine is not _control_engine else "sqlite"


def set_data_engine(new_engine: Engine, url: str) -> None:
    """热切换数据引擎。旧引擎若不是控制引擎则释放其连接池。"""
    global _data_engine, _data_url
    old = _data_engine
    _data_engine = new_engine
    _data_url = url
    if old is not _control_engine and old is not new_engine:
        old.dispose()
    log.info("数据引擎已切换 → %s", current_backend())


def get_session():
    with Session(_data_engine) as session:      # 每次调用读当前全局 → 热切换自动生效
        yield session


def run_migrations(url: str) -> None:
    """对任意 url 跑 Alembic `upgrade head`（幂等）。
    - 全新库：建全 schema。
    - pre-Alembic 旧库（有表无 alembic_version）：自动 stamp 到 baseline 再升级。
    迁移到 MySQL 时由迁移服务先对 MySQL 调用本函数建 schema。"""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    # 转义 %→%%：Config 底层 ConfigParser 会把 % 当插值语法（MySQL 密码含 %40 时会报错）；
    # env.py 里 get_section 读回时插值自动还原为真实 URL。
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    # build_engine 每次都造新引擎（绝不会是 _control_engine/_data_engine 本身），用完必须释放，
    # 否则每次迁移都漏一个连接池。
    check_engine = build_engine(url)
    try:
        with check_engine.connect() as conn:
            tables = set(sa_inspect(conn).get_table_names())
    finally:
        check_engine.dispose()

    # 判 pre-Alembic 旧库时要排除控制表——它们由 control.ensure_schema 常驻创建，
    # 否则「全新业务库 + 已存在控制表」会被误判为旧库、错误 stamp 到 baseline 而不建业务表
    # （全新部署会因此崩溃）。
    # 排除集**从 control_metadata 自动派生**，不再手写：这里曾是一份手抄清单，
    # 每加一张控制表就得记得同步——加 migrate_state 时就漏了，全部部署路径当场瘫痪。
    control_tables = set(control.control_metadata.tables) | {"alembic_version"}
    business_tables = tables - control_tables
    if business_tables and "alembic_version" not in tables:
        base_rev = ScriptDirectory.from_config(cfg).get_base()
        command.stamp(cfg, base_rev)
        log.info("检测到 pre-Alembic 旧库 → 已 stamp 到 baseline %s", base_rev)
    command.upgrade(cfg, "head")


def switch_to_local() -> None:
    """把「当前后端」改回本地 SQLite 并立即生效。供离线自救用（见 tools/use_local_db.py）。"""
    control.write_config(_control_engine, "sqlite", None)
    set_data_engine(_control_engine, _control_url())


def create_db_and_tables() -> None:
    """启动/seed/demo 调用：保证控制表存在，并把**当前数据后端**迁移到最新。

    数据后端连不上时（MySQL 关机/换网/容器没起）**不自动降级回 SQLite**：切换是非破坏性的，
    本地 SQLite 里还留着切换那天的旧业务表；悄悄退回去会让用户对着一份陈旧数据继续记账，
    等 MySQL 回来就是两边各有一半——比起不开机，那才是真正难收拾的。
    这里只把报错换成**能照做的指引**，然后照样让启动失败（不写坏任何东西）。"""
    control.ensure_schema(_control_engine)
    try:
        run_migrations(_data_url)
    except Exception as e:
        if current_backend() == "mysql":
            log.error(
                "连接当前数据库（MySQL）失败：%s\n"
                "  soroban 不会自动退回本地 SQLite——那份数据停在切换当天，"
                "对着它继续记账会造成两边各有一半。\n"
                "  请先确认 MySQL 已启动、网络可达；确实要暂时切回本地账本：\n"
                "    源码运行： cd backend && .venv/bin/python -m tools.use_local_db\n"
                "    打包版　： soroban.exe --use-local-db      （在 exe 所在目录开命令行执行）",
                e,
            )
        raise


def _wal_truncate() -> None:
    """把控制 SQLite 的 WAL 合并回主库并截断 -wal（回收磁盘）。控制引擎恒 SQLite。"""
    if _control_engine.dialect.name != "sqlite":
        return
    with _control_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


async def wal_checkpoint_loop(interval: int = 600) -> None:
    """后台循环：每 interval 秒截断一次 WAL，控制 -wal 体积。单轮异常不结束循环。"""
    while True:
        await asyncio.sleep(interval)
        try:
            _wal_truncate()
        except Exception as e:
            log.warning("周期性 WAL checkpoint 失败：%s", e)


def checkpoint_and_dispose() -> None:
    """进程干净退出前调用：截断 WAL、关闭两个引擎的连接池。"""
    try:
        _wal_truncate()
    except Exception as e:
        log.warning("关库前 WAL checkpoint 失败：%s", e)
    finally:
        if _data_engine is not _control_engine:
            _data_engine.dispose()
        _control_engine.dispose()
