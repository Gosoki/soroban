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
from typing import Optional
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .config import settings
from .paths import runtime_dir
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
    """控制/配置存储始终是 SQLite；.env 的 DATABASE_URL 仅用来定位 sqlite 文件。

    **两条分支都必须是绝对路径。** `DATABASE_URL` 是 sqlite 时，`Settings._anchor_sqlite_path`
    已经把它锚到运行时目录了；但它是 **MySQL 串**时那个校验器直接放行（它只管 sqlite），
    于是这里的兜底字面量 `"sqlite:///./soroban.db"` **从来没有被锚过**——又变回按当前
    工作目录解析，正是 §140 想根除的那件事，只是漏在了这一条分支上。

    这个状态是够得到的：`scripts/migrate_sqlite_to_mysql.py` 就明确让人把 `.env` 的
    `DATABASE_URL` 指向 MySQL 去建 schema（「建完记得改回去」），没改回去、
    或者沿用切换功能之前的老 `.env`，就落在这一支上。
    实测后果：在别的目录跑 `python -m tools.use_local_db` 会读到一个凭空新建的空控制库，
    然后回一句「当前后端已经是本地 SQLite，无需切换」并退出 0——
    而真正的控制库里写着 mysql，那条自救路径就这么白跑了。
    """
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        return url
    return f"sqlite:///{runtime_dir() / 'soroban.db'}"


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
    # 换了一个库，就换了一套数据分布——旧库的统计信息对它毫无意义，而新库多半
    # 根本没有（`replace_data` 只搬 SQLModel.metadata 里的业务表，`sqlite_stat1` 不在其中；
    # 从备份恢复出来的库同理）。挂在这里而不是各个调用点：切库、迁回本地、恢复后重绑
    # 全都要过这一个函数，一处就够，也不会有人下次新加一条路径时忘了带上。
    refresh_planner_stats()


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


def _looks_like_newer_db(exc: Exception) -> bool:
    """这个迁移失败是不是「库里的版本号，当前代码不认识」。

    典型来源：用户装过新版（库被 upgrade 到新 revision），又换回旧版 exe。
    alembic 抛的是 `CommandError: Can't locate revision identified by 'xxx'`。

    **按异常类型 + revision 特征判，不按整句文案**：alembic 的措辞会随版本变，
    而把它错认成「连不上数据库」会给出一条完全南辕北辙的指引（去查 MySQL 有没有起）。
    认不出时回落到原有分支，只是少一条更贴切的提示，不会更糟。
    """
    if type(exc).__name__ != "CommandError":
        return False
    t = str(exc).lower()
    return "revision" in t and ("locate" in t or "not found" in t or "no such" in t)


def _nothing_to_lose(url: str) -> bool:
    """这个库里有没有值得留撤销点的东西。

    全新安装（业务表还没建）与**每次跑测试的临时库**都会走到这里——
    不挡的话，跑一次测试就往真实的 `backups/` 里写一份快照。
    判据是「一行业务数据都没有」而不是「修订号是不是 None」：
    pre-Alembic 的旧库修订号同样是 None，但它里面装着整本账。
    """
    eng = build_engine(url)
    try:
        from .services.db_migrate import MIGRATION_ORDER, is_target_empty

        with eng.connect() as conn:
            tables = set(sa_inspect(conn).get_table_names())
        # 表名**从模型派生**，不写死 "orders"：这张表历史上被改过名
        # （`taobaoorder` → `orders`），写死的话下次改名会让这里静默返回「全新库」，
        # 于是安全网无声无息地消失。
        if not (tables & {m.__tablename__ for m in MIGRATION_ORDER}):
            return True                                 # 一张业务表都还没有 ⇒ 全新库
        return is_target_empty(eng)
    except Exception as e:                              # noqa: BLE001
        log.warning("判断库是否为空时出错（%s），当作「有数据」处理", e)
        return False                                    # 拿不准就留一份，宁可多备
    finally:
        eng.dispose()


def _snapshot_before_migrating() -> None:
    """真要跑迁移时，先给当前账本留一个**撤销点**。

    为什么必须有：这个应用**每次启动都自动 `alembic upgrade head`**，而分发形态是
    双击运行的 exe——没有终端、没有人会先手工备份。一旦某次迁移出事，就没有退路了。
    而代码自己就写着：**MySQL 的 DDL 是隐式提交的**，迁移链跑到一半失败时，
    前面几条已经落地、后面的没跑，库停在一个既不是旧版也不是新版的半升级态。
    那种状态下最需要的东西就是「动手之前那一刻的完整拷贝」。

    **只在真的有待跑迁移时才留**：每次启动都拷一份既慢又会把 backups/ 塞满，
    而绝大多数启动是无事发生的。

    **拷贝失败不阻断启动**：把「备份没成功」升级成「应用打不开」是更糟的失败形态
    （磁盘满就会让人进不了自己的账本）。但必须响亮地记一条 —— 静默地没有安全网，
    比明摆着没有安全网更危险。
    """
    try:
        head = pending_revision(_data_url)
    except Exception as e:                              # noqa: BLE001
        log.warning("判断是否需要迁移时出错（%s），跳过迁移前快照", e)
        return
    if head is None:
        return                                          # 已是最新，这次启动不动库
    if _nothing_to_lose(_data_url):
        return                                          # 一行业务数据都没有，撤销点无意义
    log.info("检测到待跑的迁移（→ %s），先留一份迁移前快照", head)
    if not _data_url.startswith("sqlite"):
        # MySQL 侧拿不到「忠于旧 schema」的拷贝：文件级备份无从谈起，而 `replace_data`
        # 会按新模型的列去读旧库（正是下面那条注释说的坑），mysqldump 又不一定装了。
        # **说实话比假装有安全网强。**
        log.warning("⚠️ 当前后端不是 SQLite，无法自动留迁移前快照。"
                    "升级前请自行 mysqldump 一份：本次迁移没有撤销点。")
        return
    try:
        from .backup import _default_dir, snapshot_sqlite_file
        path = snapshot_sqlite_file(_data_url, _default_dir(), f"pre-{head}")
        log.info("迁移前快照已留在 %s", path)
    except Exception as e:                              # noqa: BLE001
        log.warning("⚠️ 迁移前快照失败（%s）——本次迁移没有撤销点。"
                    "如果接下来出事，请从 backups/ 里更早的一份恢复。", e)


def pending_revision(url: str) -> Optional[str]:
    """这个库离最新还差几步？返回 head 修订号（有待跑的迁移）或 None（已是最新）。

    用来决定「这次启动要不要先留一个撤销点」。**判「相不相等」而不是「跑不跑得动」**：
    `command.upgrade` 本身是幂等的，跑一次已经最新的库什么都不会发生——
    但我们要在**动库之前**就知道会不会动，不能等它动完了再说。

    **读不出来就返回 None（当成「不用迁移」）而不是 head。** 这一句是有代价地选的：
    连上库之后读 `alembic_version` 几乎不会失败（表不存在只会返回 None），
    所以读不出来 ≈ **库根本连不上**——那种情况下备份也做不成。
    返回 head 的话会接着触发「判空失败」「快照失败」两条警告，
    把三条吓人的警告堆在真正那句「能照做的指引」前面，而后者才是用户要看的。
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    eng = build_engine(url)
    try:
        with eng.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    except Exception as e:                              # noqa: BLE001
        log.info("读不出当前修订号（%s）——库多半连不上，跳过迁移前快照；"
                 "真正的原因会由下面那次迁移报出来", e)
        return None
    finally:
        eng.dispose()
    return None if current == head else head


def migrate_to_latest() -> None:
    """启动/seed/demo 调用：保证控制表存在，并把**当前数据后端**迁移到最新。

    2026-08-22 从 `create_db_and_tables` 改名而来。旧名字撞的是 SQLModel 教程里那个
    （`metadata.create_all` 的薄壳，语义是「缺表就建，无害且幂等」），而这个函数做的是
    **对生产库跑整条 alembic 迁移链**——风险完全不是一个量级。
    证据是：改名前仓库里有**五处注释**专门在纠正它，每一处都要补一句
    「⇒ 完整 alembic upgrade」。需要五处注释来更正的名字，就是错的。

    数据后端连不上时（MySQL 关机/换网/容器没起）**不自动降级回 SQLite**：切换是非破坏性的，
    本地 SQLite 里还留着切换那天的旧业务表；悄悄退回去会让用户对着一份陈旧数据继续记账，
    等 MySQL 回来就是两边各有一半——比起不开机，那才是真正难收拾的。
    这里只把报错换成**能照做的指引**，然后照样让启动失败（不写坏任何东西）。"""
    control.ensure_schema(_control_engine)
    _snapshot_before_migrating()
    try:
        run_migrations(_data_url)
    except Exception as e:
        # **「库比代码新」要单独说。** 这一支与「连不上数据库」是完全不同的处境，
        # 而它恰好落在**分发版唯一的形态**（SQLite）上：用户装了新版建过库，
        # 又退回旧版 exe，alembic 就报 `Can't locate revision identified by 'xxx'`。
        # 不认这条的话，用户看到的是一行英文 CommandError——既不知道数据有没有事
        # （其实完好无损），也不知道该往前装还是往后退。
        # 判据用 alembic 自己的异常类型 + 那句 revision 特征，不按整句文案匹配。
        newer, on_mysql = _looks_like_newer_db(e), current_backend() == "mysql"
        if newer and on_mysql:
            # **这一支必须排在 SQLite 那支前面。** 「库比代码新」和「后端是什么」是
            # 两个正交的维度，原先却按「先认 newer」串成一条链，于是 MySQL 用户
            # （另一台机器上的新版 soroban 升级了同一个库，很常见）拿到的是
            # 「删掉 soroban.db」——那是**控制库**，存着 Fernet 加密的 MySQL 连接串。
            # 照做的结果：业务数据一个字节没动，却再也连不回去了，
            # 而他要修的问题压根不在那个文件里。
            log.error(
                "这个 MySQL 账本被**更新版本的 soroban** 升级过，当前程序认不出它的数据库版本：%s\n"
                "  你的数据没有问题，一个字节都没动——只是这个版本的程序读不了它。\n"
                "  多半是另一台机器（或另一个 exe）上装了新版，连的是同一个库。\n"
                "  两条路，选一条：\n"
                "    · 把这台也升到那个（或更新的）版本，账本照常打开；\n"
                "    · 暂时改用本地账本记账（**不会动 MySQL 里的数据**）：\n"
                "        源码运行： cd backend && .venv/bin/python -m tools.use_local_db\n"
                "        打包版　： soroban.exe --use-local-db      （在 exe 所在目录开命令行执行）\n"
                "  **不要**删本地的 soroban.db——那里面只有连接配置，删了就连不回这个 MySQL 了，"
                "而它并不是报错的原因。",
                e,
            )
        elif newer:
            # **「库比代码新」要单独说。** 这一支与「连不上数据库」是完全不同的处境，
            # 而它恰好落在**分发版唯一的形态**（SQLite）上：用户装了新版建过库，
            # 又退回旧版 exe，alembic 就报 `Can't locate revision identified by 'xxx'`。
            # 不认这条的话，用户看到的是一行英文 CommandError——既不知道数据有没有事
            # （其实完好无损），也不知道该往前装还是往后退。
            # 判据用 alembic 自己的异常类型 + 那句 revision 特征，不按整句文案匹配。
            log.error(
                "这个账本是**更新版本的 soroban** 建的，当前程序认不出它的数据库版本：%s\n"
                "  你的数据没有问题，一个字节都没动——只是这个版本的程序读不了它。\n"
                "  两条路，选一条：\n"
                "    · 装回你之前用的那个（或更新的）版本，账本照常打开；\n"
                "    · 确实要用当前这个旧版本：先把账本目录整个备份一份，再删掉其中的\n"
                "      soroban.db（连同 -wal / -shm），当前版本会建一个全新的空账本。\n"
                "  **不要**在没有备份的情况下删——那一步不可逆。",
                e,
            )
        elif on_mysql:
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
    else:
        refresh_planner_stats()


def refresh_planner_stats() -> None:
    """让 SQLite 的查询规划器有据可依——**这不是可选的调优，是补一个缺失的前提**。

    没有 `sqlite_stat1` 表时，规划器只能按内置的默认选择性猜。实测（6000 单的库）：
    它会挑中 `ix_orders_is_delete`——**全表最没有选择性的那根**（97.5% 的行 is_delete=0）
    ——把捞出来的 5850 行丢进临时 B 树全排一遍，才取走 `LIMIT 50` 要的那 50 行。
    列表页 2.6 ms、翻到第 20 页 8.8 ms，而且**随 OFFSET 线性变差**。

    跑过一次之后规划器改走 `ix_orders_date`，顺序天然就对，临时 B 树消失：
    列表页 0.24 ms、第 20 页 0.57 ms。

    **今天不痛，说清楚**：生产账本 56 单，两条路径都是微秒级，用户一点感觉都没有。
    这行是给库长大之后留的——爬虫一次回灌几百上千单，涨得比想象快。
    **为什么是无条件 ANALYZE，不是 `PRAGMA optimize`**：optimize 看的是**本连接的查询历史**
    ——它只重算这个连接查过、且统计已经明显陈旧的表。启动时的连接没有任何历史，
    实测（SQLite 3.45.1，有索引/无索引 × 3 行/6000 行四种组合）它**一次都没建出 stat1**。
    那条路在这里是恒 no-op。

    也不做「已经有 stat1 就跳过」：库会从 56 单长到几千单，而 stat1 还记着 56 单的分布，
    规划器照样会选错——那等于把今天的问题推到明天，还更难发现。
    代价也不值得省：实测 ANALYZE 在 56 / 6000 / 100000 单（各 5 根索引）上分别是
    2.6 / 5.4 / 49.5 ms。这个账本永远到不了十万单，五十毫秒是启动耗时里量不出来的一格。

    **为什么不是加复合索引** `(is_delete, date, id)`：也量了。深翻页确实再快一点
    （0.29 vs 0.57 ms），但它要多占 7% 的库体积、每次写入多维护一根索引，
    还会把「合计求和」从 0.94 拖到 1.76 ms（规划器改用更宽的索引做全扫）。
    缺的是**统计信息**，不是索引；照着症状加索引是治标。

    MySQL 不做：InnoDB 自己维护统计信息。
    """
    eng = get_engine()
    if eng.dialect.name != "sqlite":
        return
    try:
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            conn.commit()
    except Exception as e:
        # **不能因为它启动失败。** 这只是让查询快一点，不是正确性的前提；
        # 而它最可能的失败是撞上写锁（另一个进程正在迁移），那属于稍后重试就好的事。
        log.warning("刷新查询规划器统计信息失败（不影响使用，只是列表页会慢一点）：%s", e)


def _wal_truncate() -> None:
    """把控制 SQLite 的 WAL 合并回主库并截断 -wal（回收磁盘）。控制引擎恒 SQLite。"""
    if _control_engine.dialect.name != "sqlite":
        return
    with _control_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")


async def wal_checkpoint_loop(interval: int = 600) -> None:
    """后台循环：每 interval 秒截断一次 WAL，控制 -wal 体积。单轮异常不结束循环。"""
    from starlette.concurrency import run_in_threadpool

    while True:
        await asyncio.sleep(interval)
        try:
            # **必须丢线程池。** 这是个协程，裸调 `_wal_truncate()` 就是在事件循环线程上
            # 做同步 SQLite 调用——而 `PRAGMA wal_checkpoint(TRUNCATE)` 撞上写锁时
            # 会一直等到 sqlite3 的 busy timeout，实测阻塞 5 秒。那 5 秒里整个事件循环停摆：
            # 健康检查、前端轮询、静态资源全卡，而且**一行日志都不会有**——
            # checkpoint 撞锁是返回 busy 而不是抛异常，下面这个 except 根本进不去。
            # 现实触发路径：数据库页点「迁回本地 SQLite」，迁移在单事务里逐表 delete+insert，
            # 全程握着写锁；600 秒一次的这一轮正好落进那段窗口。
            await run_in_threadpool(_wal_truncate)
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
