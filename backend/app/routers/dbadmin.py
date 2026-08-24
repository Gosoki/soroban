"""数据库管理 / 迁移接口（/api/db）——SQLite ↔ MySQL 双向 + 记录连接过的库。

- GET    /api/db/status              当前后端 + 已保存的连接列表（均不含密码）
- POST   /api/db/test                测试一个目标（MySQL 参数 / 已存连接 / 本地）；成功即记住
- POST   /api/db/migrate             把当前数据整表覆盖到目标（建库/建表/拷数据，不切换）
- POST   /api/db/switch              热切换到目标（无需重启；不清源库，故可逆）
- DELETE /api/db/connections/{id}    删除一条已保存连接（不能删当前正在用的）
- GET    /api/db/backups             已有的备份快照列表
- POST   /api/db/backups             立刻备份一次当前生效的库

**恢复刻意不给 HTTP 入口**：那是唯一一条能一键清空账本的操作，留在需要手敲 `yes`
的命令行里（`python -m tools.backup_db --restore <文件>`）。备份可以点，恢复要走人。

目标(Target)三选一：{connection_id} 一键复用已存连接 / {backend:'mysql', host...} 新连接 /
{backend:'sqlite'} 本地库。安全：DSN 全程 Fernet 加密存储，密码永不回传前端；库名白名单防注入；
全部端点需登录。
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.engine import make_url

from ..auth import get_current_user
from ..database import (
    build_engine,
    control_engine,
    control_url,
    current_backend,
    get_engine,
    run_migrations,
    set_data_engine,
)
from ..db import control
from ..maintenance import barrier
from ..services import db_migrate

log = logging.getLogger("soroban.db.admin")

router = APIRouter(prefix="/api/db", tags=["db"], dependencies=[Depends(get_current_user)])


class Target(BaseModel):
    """迁移/切换的目标库。三种指定方式见模块文档。"""
    connection_id: Optional[int] = None      # 复用已保存连接（一键切换）
    backend: Optional[str] = None            # 'sqlite' | 'mysql'
    host: Optional[str] = None
    port: int = 3306
    user: Optional[str] = None
    password: str = ""
    database: Optional[str] = None
    # 切换时若发现「迁移之后源库又被改过」会 409 拦下；用户明确表示放弃那些改动时置 True 再来一次。
    confirm_changed: bool = False
    # 迁移时若发现目标库**已经有数据**（会被整表删掉）会 409 拦下；用户看清了要覆盖什么再置 True。
    confirm_overwrite: bool = False


# --- 解析目标 ------------------------------------------------------------------

def _mysql_conn_fields(t: Target):
    """→ (host, port, user, password, database)。connection_id 时从已存 DSN 解出。"""
    if t.connection_id is not None:
        url = control.get_connection_url(control_engine(), t.connection_id)
        if not url:
            raise HTTPException(status_code=404, detail="连接不存在或无法解密（SECRET_KEY 可能已变更）")
        u = make_url(url)
        return u.host, int(u.port or 3306), u.username, u.password or "", u.database
    if not (t.host and t.user and t.database):
        raise HTTPException(status_code=400, detail="MySQL 参数不完整（需 host / user / database）")
    return t.host, int(t.port or 3306), t.user, t.password or "", t.database


def _resolve_target(t: Target):
    """→ (backend, url, engine, owns)。owns=True 表示 engine 是新建的、用完需 dispose。"""
    if t.connection_id is None and t.backend == "sqlite":
        return "sqlite", control_url(), control_engine(), False
    host, port, user, pw, db = _mysql_conn_fields(t)
    url = db_migrate.build_mysql_url(host, port, user, pw, db)
    return "mysql", url, build_engine(url), True


def _active_identity() -> dict:
    """当前生效后端 + （MySQL 时）连接标识，供前端展示/比对。不含密码。"""
    backend = current_backend()
    cfg = control.read_config(control_engine())
    # `degraded` 要**无条件**带上：它恰恰只在 backend 已被降级成 sqlite 时才非空，
    # 放进 `if backend == "mysql"` 里就永远发不出去。
    d = {"backend": backend, "degraded": cfg.get("degraded", "")}
    if backend == "mysql" and cfg["mysql_url"]:
        u = make_url(cfg["mysql_url"])
        d.update(host=u.host, port=u.port, user=u.username, database=u.database)
    return d


def _is_same_as_active(backend: str, url: str) -> bool:
    """目标是否就是当前正在使用的库（防止把线上库当迁移目标清空 / 重复切换）。"""
    active = current_backend()
    if backend == "sqlite":
        return active == "sqlite"
    if active != "mysql":
        return False
    cfg = control.read_config(control_engine())
    if not cfg["mysql_url"]:
        return False
    a, b = make_url(cfg["mysql_url"]), make_url(url)
    return (a.host, a.port, a.username, a.database) == (b.host, b.port, b.username, b.database)


def _target_key(t: Target) -> str:
    """目标库的稳定标识（不含密码），作 migrate_state 的主键。"""
    if t.connection_id is None and t.backend == "sqlite":
        return "sqlite"
    host, port, user, _pw, db = _mysql_conn_fields(t)
    return f"{user}@{host}:{port}/{db}"


def _remember(host, port, user, pw, db) -> int:
    """把 MySQL 连接记入「连接过的库」（加密 DSN）。返回连接 id。"""
    url = db_migrate.build_mysql_url(host, port, user, pw, db)
    return control.upsert_connection(
        control_engine(), backend="mysql", url=url,
        host=host, port=port, user=user, database=db,
    )


# --- 端点 ----------------------------------------------------------------------

@router.get("/backups")
def list_backups():
    """已有的备份快照（新的在前）。只列本模块自己造的那种文件名。"""
    from ..backup import _MINE, _default_dir

    d = _default_dir()
    if not d.is_dir():
        return {"dir": str(d), "items": []}
    items = []
    for f in sorted((x for x in d.iterdir() if _MINE.match(x.name)), reverse=True):
        st = f.stat()
        items.append({"name": f.name, "bytes": st.st_size,
                      # **带时区的 UTC**，与全站其它时间戳同一口径（`models.base.utcnow`）。
                      # 原先是 `fromtimestamp(ts)` —— 服务器**本地**时间且不带时区，
                      # 是全站唯一一处这么干的地方，也是唯一绕开前端 `parseUtc` 那条管线的。
                      # 服务器在 JST、看的人在别的时区时，这一列会与页面上所有别的时间差 9 小时，
                      # 而没有任何东西提示它们口径不同。
                      "mtime": dt.datetime.fromtimestamp(
                          st.st_mtime, dt.timezone.utc).isoformat(timespec="seconds")})
    return {"dir": str(d), "items": items}


@router.post("/backups")
def create_backup():
    """立刻备份一次**当前生效的库**（SQLite 或 MySQL 都一样）。

    拷贝期间会挂只读屏障，几秒内其他人的写入会收到 503 并自动重试——
    这是必须的：没有屏障就会拷出「订单拷过去了、它的物品还没拷」的撕裂快照。
    """
    from ..backup import make_backup

    buf = io.StringIO()
    try:
        # **`keep=0`：界面这一下只负责「多留一份」，不承担轮换。**
        # 轮换是备份策略的一部分，而策略的参数在 crontab 里（`--keep 60`，README 第 242 行
        # 明写着这个用法），应用**读不到**它。原先这里不传 keep ⇒ 走签名默认的 30 ⇒
        # 用户按 README 配成 60 份的目录，被这一次点击剪成 30：最旧的 31 份 `.db`
        # 连同配对的 `env-<时间戳>.txt` 一起 unlink，两个月的历史一次点击全没，不可逆。
        #
        # 而他什么都看不到：`_prune` 打的那 31 行「已清理旧备份 X」全进了这个
        # `io.StringIO`，随响应的 `log` 字段回给前端，而 `doBackup()` 只读 `total` 和 `file`。
        # 页面上唯一的痕迹是卡片角上「N 份」从 60 变成 30，没有任何东西说这是删除。
        #
        # 让它猜一个 keep 是错的：猜错就删掉真数据，而猜对没有任何收益
        # （cron 下一次运行照样会把该剪的剪掉）。与 `restore()` 的安全备份同一个判据。
        path, counts = make_backup(stream=buf, keep=0)
    except RuntimeError as e:
        # 「已有另一项维护操作在进行」——两个人同时点备份，或备份撞上迁移。
        # 这是 409 不是 500：没出错，只是现在轮不到，等几秒再点就行。
        # 与 migrate 的映射保持一致（前端只认 409 里那句「目标库里已经有数据」，
        # 不会把这条误当成覆盖确认）。
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.exception("备份失败")
        raise HTTPException(500, f"备份失败：{e}") from e
    return {"file": path.name, "dir": str(path.parent),
            "counts": {k: v for k, v in counts.items() if v}, "total": sum(counts.values()),
            "log": buf.getvalue().strip()}


@router.get("/status")
def status():
    return {"active": _active_identity(), "connections": control.list_connections(control_engine())}


def _reject_unsupported(version: str) -> None:
    """服务端版本不够就**在门口拒绝**，别让它跑到迁移中途才炸。

    MySQL 的 DDL 是隐式提交的：迁移链跑到一半失败时，前面几条已经落地、后面的没跑，
    库停在一个既不是旧版也不是新版的半升级态。而这一刻用户能看到的只有一句
    驱动层的英文报错（`Unknown collation` / 语法错误），完全不知道该回滚什么。
    这道闸挂在 test 与 migrate 两处——**test 是用户点「测试连接」的地方**，
    只挂 migrate 的话，界面会先说「连接成功」，再在下一步崩掉。
    """
    why = db_migrate.unsupported_server(version or "")
    if why:
        raise HTTPException(status_code=400, detail=why)


@router.post("/test")
def test(t: Target):
    if t.connection_id is None and t.backend == "sqlite":
        return {"ok": True, "kind": "sqlite"}      # 本地库永远可用
    host, port, user, pw, db = _mysql_conn_fields(t)
    ok, msg = db_migrate.test_connection(host, port, user, pw, db)
    if ok:
        _reject_unsupported(msg)
        cid = _remember(host, port, user, pw, db)
        return {"ok": True, "version": msg, "connection_id": cid}
    # 库尚不存在 → 退一步只测服务器连通（迁移时会自动建库）
    ok2, msg2 = db_migrate.test_connection(host, port, user, pw, None)
    if ok2:
        _reject_unsupported(msg2)
        cid = _remember(host, port, user, pw, db)
        return {"ok": True, "version": msg2, "connection_id": cid,
                "note": f"服务器可连，但库 {db} 暂不存在（迁移时会自动创建）"}
    # 完整驱动错误进服务端日志；回给（已登录的）用户一句可诊断的短提示（本工具仅本人用，需要它排障）。
    log.warning("MySQL 连接测试失败 host=%s port=%s db=%s：%s", host, port, db, msg2)
    raise HTTPException(status_code=400, detail=f"连接失败：{msg2}")


@router.post("/migrate")
def migrate(t: Target):
    """把**当前数据库**的数据整表覆盖到目标（建库→建表→拷数据）。不切换、不动当前库。"""
    backend, url, tgt, owns = _resolve_target(t)
    if _is_same_as_active(backend, url):
        raise HTTPException(status_code=400, detail="目标就是当前正在使用的数据库，无需迁移")
    try:
        if backend == "mysql":
            host, port, user, pw, db = _mysql_conn_fields(t)
            ok, msg = db_migrate.test_connection(host, port, user, pw, None)
            if not ok:
                raise HTTPException(status_code=400, detail=f"MySQL 连接失败：{msg}")
            # 版本闸门必须排在 ensure_database **之前**：建库本身在老服务端上是成功的，
            # 建完再拒绝就留下一个空库，而用户会以为「已经建好了、是别的地方出错」。
            _reject_unsupported(msg)
            try:
                db_migrate.ensure_database(host, port, user, pw, db)
            except ValueError as e:                             # 库名不合白名单
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:                              # noqa: BLE001
                # 最常见的是 1044/1045「Access denied … CREATE DATABASE」：账号能连服务器
                # （上面的 test_connection 只跑 SELECT VERSION()，权限完全够）却没有建库权。
                # 原先这类异常直接冲到 ASGI 层 → 裸 500，前端只显示 axios 的通用文案，
                # 用户看不到唯一有用的那句诊断。
                log.exception("目标库创建失败")
                raise HTTPException(status_code=400, detail=f"目标库创建失败：{e}")
            _remember(host, port, user, pw, db)
        # 目标建/更新 schema（Alembic，含方言生成列）；本地 SQLite 目标同样确保升到 head
        try:
            run_migrations(url)
        except Exception as e:                                  # noqa: BLE001
            log.exception("目标建表失败")
            raise HTTPException(status_code=500, detail=f"目标建表失败：{e}")
        # 整表覆盖拷贝：当前数据引擎 → 目标。
        # **全程挂只读屏障**：replace_data 逐表读源库，而 SQLite 侧没有读快照，期间的写入会
        # 产生撕裂的拷贝（订单拷了、物品没拷 / 子表引用尚未拷贝的父行）。见 app/maintenance.py。
        # 拷贝前体检：SQLite 不检查 VARCHAR 长度与 DECIMAL 范围，历史脏行会让 MySQL 侧
        # 抛 1406/1264；而拷贝是单事务，一行踩雷整批回滚。与其让用户对着一句原始异常发呆，
        # 不如先把具体是哪张表哪一行、哪个字段超了列出来。
        # **目标库里已有的数据会被 replace_data 逐表 delete 掉。**
        # 在此之前全仓没有任何一处比较源库与目标库谁更新：`_is_same_as_active` 只回答
        # 「目标是不是当前正在用的那个」，而 switch 里那道指纹闸只回答「源库比上次迁移时新不新」
        # ——**它看不见反方向**。于是有一条全程走本应用自己指引的路会毁掉整个账本：
        #   MySQL 连不上 → 按提示 `--use-local-db` 退回本地（内容停在切走那天）
        #   → 停机期间在本地补记几单（rescue.py 正是这么建议的）
        #   → MySQL 恢复后点「切换」→ 源库有改动 ⇒ 409
        #   → 弹窗的**默认按钮**是「重新迁移再切换」⇒ 直接调到这里
        #   → 用几个月前的本地快照覆盖掉 MySQL 上几个月的账本，单事务提交。
        # 而 MySQL 侧没有任何自动备份（backup.sh 在 MySQL 模式下是直接拒绝的），退不回去。
        # 所以这道闸按「目标非空」拦，并且**把对面有什么如实列出来**：用户要判断的是
        # 「值不值得覆盖」，只说一句「会覆盖同名表」他没法判断。
        existing = db_migrate.target_rows(tgt)
        if existing and not t.confirm_overwrite:
            # 单行不带 \n：前端用 ElMessageBox 显示，它默认不保留换行。
            # **必须说清「已经动过什么」。** 上面那句 `run_migrations(url)` 已经把目标库的
            # 表结构升到 head 了——那是 DDL，MySQL 上隐式提交、本仓库也没有 downgrade 路径。
            # 顺序是被迫的：表还没建就数不出行数（`target_rows` 要 select 业务表），
            # 反过来排会让「目标尚未建表」被误报成「目标是空的」，把用户推向覆盖。
            # 但用户看到这句 409 点了取消，会以为**什么都没发生**——而如果另一台机器
            # 还在用旧版 soroban 连同一个库，它下次启动就会撞上「库比代码新」。
            # 数据没动是真的，schema 动了也是真的，两句都要说。
            raise HTTPException(status_code=409, detail=(
                "目标库里已经有数据（"
                + "、".join(f"{k} {v} 条" for k, v in existing.items())
                + "），迁移会先把它们全部删除，再用当前库整表覆盖。"
                "请先确认目标库里没有当前库所没有的东西——这一步没有备份，无法撤销。"
                "（注意：为了能数出上面这些行数，目标库的**表结构**已经升到本版；"
                "数据一个字都没动，但若还有旧版 soroban 连着同一个库，它需要一并升级。）"))

        problems = db_migrate.preflight(get_engine(), tgt)
        if problems:
            raise HTTPException(
                status_code=400,
                detail="以下数据超出目标数据库（MySQL）的字段限制，迁移会失败，请先修正：\n"
                       + "\n".join(problems[:10])
                       + (f"\n…另有 {len(problems) - 10} 处" if len(problems) > 10 else ""),
            )
        try:
            with barrier.hold("数据库迁移中"):
                counts = db_migrate.replace_data(get_engine(), tgt)
                # 拷完立刻记下源库指纹：switch 时再算一次做对比，就能发现
                # 「迁移之后源库又被改过」——那些改动不会跟着过去。
                fp = db_migrate.source_fingerprint(get_engine())
            control.save_migrate_fingerprint(control_engine(), _target_key(t), fp)
        except HTTPException:
            raise
        except db_migrate.CopyFailed as e:                      # 必须在 RuntimeError 之前
            log.exception("数据拷贝失败")
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:                               # 已有另一项维护操作在进行
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:                                  # noqa: BLE001
            log.exception("数据拷贝失败")
            raise HTTPException(status_code=500, detail=f"数据拷贝失败：{e}")
    finally:
        if owns:
            tgt.dispose()
    return {"ok": True, "counts": counts, "total": sum(counts.values())}


@router.post("/switch")
def switch(t: Target):
    """热切换到目标（无需重启）。要求目标已迁移过（有数据），否则拒绝以防丢数据。不清空源库。"""
    backend, url, tgt, owns = _resolve_target(t)
    if _is_same_as_active(backend, url):
        raise HTTPException(status_code=400, detail="当前已在使用该数据库")

    # **服务端版本闸也要挂在这里。** 原先只挂在 test 与 migrate 两处（见 _reject_unsupported
    # 的 docstring），而界面上「测试连接 / 迁移到此库 / 切换」是三个平级按钮，
    # 没有任何东西强制「先测试」。跳过测试直接点「切换到此库」时，下面那句 run_migrations
    # 会对一台 MariaDB / MySQL 5.7 跑完整 alembic 链，跑到用 utf8mb4_0900_bin 那条时炸——
    # 而 MySQL 的 DDL 是**隐式提交**的：前面十几条已经落地，库停在既不是旧版也不是新版的
    # 半升级态，用户只拿到一句「表结构升级失败」。此后连「迁移到此库」都会被版本闸拒掉
    # （那道闸是好的），只能手工 DROP DATABASE 才能重来。
    if backend == "mysql":
        h, p, u, pw, dbn = _mysql_conn_fields(t)
        ok, msg = db_migrate.test_connection(h, p, u, pw, None)
        if not ok:
            if owns:
                tgt.dispose()
            raise HTTPException(status_code=400, detail=f"MySQL 连接失败：{msg}")
        try:
            _reject_unsupported(msg)
        except HTTPException:
            if owns:
                tgt.dispose()
            raise

    # 目标库的 schema 必须先升到 head，否则切过去全站 500（缺列 1054 / 缺表 1146，
    # 两者都不是 IntegrityError/ValueError，main.py 无对应 handler → 裸 500）。
    # 场景很平常：迁到 MySQL 用一阵 → 切回本地 SQLite → 升级 soroban → 再切回 MySQL，
    # 那份 MySQL 的 schema 还停在切走那天。两个方向对称，故对 SQLite 目标也一并执行。
    #
    # 选「自动升级」而不是「拒绝并提示先迁移」：进程重启时 migrate_to_latest 本来就会
    # 对当前后端 run_migrations，拒绝在这里反而不一致；更要紧的是，拒绝会把用户推向
    # 「迁移到此库」按钮——那个按钮会用源库快照**覆盖**目标库，把「schema 落后」变成
    # 「数据被旧快照盖掉」，是更难收拾的后果。
    # 必须在 is_target_empty 之前：它会 select 业务表，缺列时抛出的异常会被下面的兜底
    # 误报成「尚未建表/迁移」，同样把用户引向那个会覆盖数据的按钮。
    try:
        run_migrations(url)
    except Exception as e:                                     # noqa: BLE001
        if owns:
            tgt.dispose()
        log.exception("目标库 schema 升级失败")
        raise HTTPException(status_code=400, detail=f"目标数据库的表结构升级失败，未切换：{e}")

    # 目标必须已建表且有数据（= 迁移过），否则拒绝
    try:
        empty = db_migrate.is_target_empty(tgt)
    except Exception:                                          # noqa: BLE001  表不存在等
        if owns:
            tgt.dispose()
        raise HTTPException(status_code=400, detail="目标数据库尚未建表/迁移，请先点「迁移到此库」")
    if empty:
        if owns:
            tgt.dispose()
        raise HTTPException(status_code=400, detail="目标数据库无数据，请先点「迁移到此库」")

    # **临界段挂只读屏障。** 从这里到引擎交换完成为止，源库不许再有写入。
    #
    # 上面那段 `run_migrations(target)` 刻意**不**在屏障里：MySQL 目标落后几个版本时
    # 它要跑几十秒，把全站写入冻那么久是不划算的——而且它写的是**目标**库，
    # 期间源库照常被写也不会丢：下面的指纹检查会把那些改动查出来并 409。
    #
    # 真正关不上的是**指纹检查到 `set_data_engine` 之间**那几毫秒：落在那里的写会提交进
    # 旧库，切换之后既不在新库里、也没有任何提示——一张已经 200 的订单凭空消失。
    # 窗口很小，但后果是静默丢账，而代价只有这一行；`migrate()` 本来就挂着屏障，
    # 两个同级操作没有理由一个挂一个不挂。
    #
    # 顺带把指纹也挪进屏障内计算：这样「指纹之后没有新的写」是由构造保证的，
    # 而不是靠窗口足够窄。
    with barrier.hold("切换数据库中"):
        # 「迁移之后源库又被改过没有」——改过的那些**不会**跟着切换过去，切完就静默消失了。
        # 拿不到指纹（老版本迁的 / 换过机器）时不拦，只是没法给出这层保护。
        recorded = control.read_migrate_fingerprint(control_engine(), _target_key(t))
        if recorded is not None and not t.confirm_changed:
            changes = db_migrate.describe_fingerprint_diff(
                recorded, db_migrate.source_fingerprint(get_engine()))
            if changes:
                if owns:
                    tgt.dispose()
                raise HTTPException(
                    status_code=409,
                    detail="迁移之后当前库又有改动（" + "、".join(changes) + "）。"
                           "这些改动不在目标库里，直接切换会丢失。"
                           "建议先重新点「迁移到此库」；确认要放弃这些改动请再确认一次。",
                )

        if backend == "mysql":
            host, port, user, pw, db = _mysql_conn_fields(t)
            control.write_config(control_engine(), "mysql", url)
            set_data_engine(tgt, url)                          # tgt 成为线上引擎，不再 dispose
            cid = _remember(host, port, user, pw, db)
            control.touch_connection(control_engine(), cid)
        else:
            # 切回本地 SQLite：数据引擎复位为控制引擎（set_data_engine 会释放旧 MySQL 引擎）
            control.write_config(control_engine(), "sqlite", None)
            set_data_engine(control_engine(), control_url())
    return {"ok": True, "backend": backend}


@router.delete("/connections/{cid}")
def remove_connection(cid: int):
    url = control.get_connection_url(control_engine(), cid)
    if url and _is_same_as_active("mysql", url):
        raise HTTPException(status_code=400, detail="不能删除当前正在使用的连接，请先切换到其它数据库")
    control.delete_connection(control_engine(), cid)
    return {"ok": True}
