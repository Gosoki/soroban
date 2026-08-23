"""账本备份：把**当前生效后端**的全部业务数据拷成一个独立的 SQLite 文件。

放在 `app/` 而不是 `tools/`，理由与 `rescue.py` 逐字相同：`app.*` 天然在 PyInstaller 的
导入图里（run.py → app.main），打包版拿得到；`tools/` 既不在导入图里也没被 spec 收进去。
`tools/backup_db.py` 是本模块的薄壳。

**为什么不用 mysqldump / sqlite3 命令行**（`backup.sh` 原先两者都要）：

  · 这两个二进制**在部署机上都可能没有**（本项目的开发机就两个都没装，
    于是 `backup.sh` 第一行 `command -v sqlite3 || exit 1` 直接退出——
    「有一个备份脚本」和「有备份」是两回事，而 crontab 里那条天天返回退出码 0）；
  · 打包版更没有；
  · mysqldump 还要把加密的 DSN 解出来递给另一个进程，并且会 dump 出模型里没有的
    生成列（MySQL 的「活跃唯一」列），恢复时反而炸。

走 `replace_data(源, 目标)` 则两个后端对称、纯 Python、打包版可用、默认测试跑得到——
那个函数本来就收任意 src/dst（「切换可逆、可反复迁移」正是它的设计意图）。

**快照里没有控制表。** `app_db_config` / `db_connection` 用的是独立的 MetaData，
不在 `SQLModel.metadata` 里，所以 `run_migrations` 建出来的快照库只有 13 张业务表。
这一点是设计的一部分而不是巧合：它意味着「恢复一份快照」不可能顺带把
「你正连着哪个库」也一起还原，也不会把加密的 MySQL 连接串复制出去。

**`.env` 一并备份。** `SECRET_KEY` 丢了，已保存的 MySQL 连接串就再也解不开
（`db/control.py` 的 Fernet 密钥由它派生），`read_config` 会静默降级成空的本地库。
这条失败路径在代码里写了三段注释、却一行防护都没有，而它只值这几行。
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .paths import runtime_dir

_STAMP_FMT = "%Y%m%d-%H%M%S"
# 轮换只认**本模块自己造的**这个确切形状。刻意不用 `soroban-*.db` 这种宽匹配：
# 同一个目录里躺着 `soroban-20260808-093421-pre-<revision>.db` 这种「动库之前」的快照，
# 那是整个目录里最不该被自动删掉的文件，而宽匹配正好会吃掉它。
_MINE = re.compile(r"^soroban-\d{8}-\d{6}\d{0,2}\.db$")   # 末尾两位是同秒撞名时的序号


def _default_dir() -> Path:
    """默认落点：**跟着账本走**——账本是哪个 sqlite 文件，就落在它旁边的 `backups/`。

    正常部署下 `DATABASE_URL` 就是 `backend/soroban.db`，所以结果仍是 `backend/backups/`，
    与从前一致。不一致的只有两种情况，而在这两种情况下「跟着账本走」都更对：

      · 有人把账本指到别处 ⇒ 备份跟过去，而不是留在一个跟它无关的目录里；
      · 跑测试时账本是个临时库 ⇒ 快照落在那个临时目录，**不会污染真实的 backups/**。
        （加迁移前自动快照那天，测试当场往真实备份目录里写了一份，就是这么来的。）

    MySQL 后端没有「文件旁边」可言，退回运行时目录。
    """
    from sqlalchemy.engine import make_url

    from .config import settings
    
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        try:
            db = make_url(url).database
        except Exception:                               # noqa: BLE001
            db = None
        if db and db != ":memory:":
            return Path(db).resolve().parent / "backups"
    return runtime_dir() / "backups"


def make_backup(out_dir: Optional[Path] = None, *, stream=None,
                keep: int = 30, tag: str = "") -> tuple[Path, dict]:
    """备份一次。返回 (快照文件路径, 各表行数)。

    `tag` 给文件名加一个后缀（`soroban-<时间戳>-<tag>.db`）。**带 tag 的快照永远不参与轮换**
    ——`_MINE` 只认不带后缀的那种形状。迁移前快照就走这条：它是某一次升级的撤销点，
    被 30 次日常备份轮换掉就完全失去了意义。（历史上手工留的
    `soroban-20260808-093421-pre-<revision>.db` 也是这个形状，两者自然对齐。）

    `keep` = 保留最近几份，更旧的删掉。**只删本函数自己造的那种文件名**
    （`soroban-<时间戳>.db`），绝不碰目录里别的东西——备份目录里往往还躺着
    用户自己手动放的东西。
    """
    from .database import build_engine, get_engine, run_migrations
    from .maintenance import barrier
    from .services.db_migrate import replace_data

    out = stream or sys.stdout

    def say(msg: str) -> None:
        print(msg, file=out)

    dest_dir = Path(out_dir) if out_dir else _default_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 时间戳只精确到秒，而这套系统同时会有 2–3 个人在用，撞名是会真发生的。
    # 撞了就往时间戳后面补两位序号（`_MINE` 认得这个形状，轮换才清得掉）。
    for attempt in range(60):
        stamp = dt.datetime.now().strftime(_STAMP_FMT) + ("" if not attempt else f"{attempt:02d}")
        # 先写 `.part`，全部成功后再改名。中途失败/断电时留下的是一个显然没写完的名字，
        # 而不是一个看起来正常、其实缺表的 `.db`——后者会在真要用它的时候才暴露。
        suffix = f"-{tag}" if tag else ""
        part = dest_dir / f"soroban-{stamp}{suffix}.db.part"
        final = dest_dir / f"soroban-{stamp}{suffix}.db"
        try:
            # `x` 模式原子占位：两个进程同时跑时，只有一个人能占到这个 `.part`。
            part.touch(exist_ok=False)
        except FileExistsError:
            continue
        if final.exists():
            # 占到了 `.part`，但最终名已经有人用了。**这条同时覆盖两种情况**：
            #   · 同一秒里先后跑两次（前一次已经把 `.part` 改名走了，占位根本挡不住）；
            #   · 并发时对方刚好在我们占到 `.part` 的前一瞬完成改名。
            # 所以前面不需要再加一次「最终名在不在」的预检查——那只是同一件事做两遍。
            part.unlink(missing_ok=True)
            continue
        break
    else:
        raise RuntimeError("同一秒里备份重名了 60 次，放弃")
    # 占位符**不撤**：SQLite 把 0 字节文件当成一个空库，alembic 直接往里建表就行。
    # 撤掉再让 alembic 重建会重新打开一个窗口，别人正好在这一瞬占同一个名字。

    # 从这里开始任何一步失败，都必须把占位的 `.part` 收掉——否则失败一次就永久留一个残file，
    # 而它们会在备份目录里越堆越多，看上去像一堆坏掉的备份。
    try:
        run_migrations(f"sqlite:///{part}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    dst = build_engine(f"sqlite:///{part}")
    try:
        # **拷贝期间必须挂只读屏障**：SQLite 侧没有读快照（pysqlite 只在写之前才 BEGIN，
        # SELECT 一律跑在 autocommit 下），期间的写入会产生**撕裂的拷贝**
        # ——订单拷过去了、它的物品还没拷。详见 app/maintenance.py。
        with barrier.hold("备份中"):
            counts = replace_data(get_engine(), dst)
    except BaseException:
        dst.dispose()
        part.unlink(missing_ok=True)
        raise
    finally:
        dst.dispose()
    part.rename(final)

    env = runtime_dir() / ".env"      # 不用 __file__，理由见 snapshot_sqlite_file 里同名处
    if env.is_file():
        # `SECRET_KEY` 丢了，已保存的 MySQL 连接串就再也解不开。
        shutil.copy2(env, dest_dir / f"env-{stamp}{suffix}.txt")
        say(f"  同时备份了 .env（{dest_dir / f'env-{stamp}{suffix}.txt'}）——它里面的 SECRET_KEY "
            f"是解开已保存 MySQL 连接串的唯一钥匙")

    total = sum(counts.values())
    say(f"已备份 {total} 行到 {final}")
    for t, n in sorted(counts.items()):
        if n:
            say(f"    {t}: {n}")
    if total == 0:
        # 一行都没有多半是「连错库了」而不是「账本真的空」。备份最怕的就是
        # 天天返回退出码 0、备的却是错东西——那比明摆着没有备份更危险。
        say("  ⚠️ 一行数据都没备到。确认一下当前连的是不是你以为的那个库"
            "（应用内「数据库」页会显示当前后端）。")

    _prune(dest_dir, keep, say)
    return final, counts


def _prune(dest_dir: Path, keep: int, say) -> None:
    """只清理本模块自己造的那两种文件名，按时间戳倒序保留最近 `keep` 份。"""
    if keep <= 0:
        return
    snaps = sorted((p for p in dest_dir.iterdir() if _MINE.match(p.name)), reverse=True)
    for old in snaps[keep:]:
        stamp = old.name[len("soroban-"):-len(".db")]
        old.unlink(missing_ok=True)
        (dest_dir / f"env-{stamp}.txt").unlink(missing_ok=True)
        say(f"  已清理旧备份 {old.name}")


def restore(snapshot: Path, *, assume_yes: bool = False, stream=None) -> int:
    """把一份快照恢复到**当前生效的后端**。返回进程退出码。

    ⚠️ 这是**覆盖**：`replace_data` 会先按逆外键序清空目标的全部业务表。
    所以默认要人确认一次，并且先把当前状态再备份一份——
    「恢复错了」和「一开始就没有备份」应该是两件不同严重程度的事。

    **不走裸 SQL 导入**：那会绕开 `dbadmin.migrate` 已有的目标非空 409、preflight
    和只读屏障，等于给「唯一一条能一次性毁掉线上账本的操作」新开一条没有守卫的路。
    """
    from .database import build_engine, get_engine, run_migrations
    from .maintenance import barrier
    from .services.db_migrate import replace_data

    out = stream or sys.stdout

    def say(msg: str) -> None:
        print(msg, file=out)

    snapshot = Path(snapshot)
    if not snapshot.is_file():
        say(f"找不到快照文件：{snapshot}")
        return 1

    # 这句提示必须从**真正要写的那个 engine** 上取，不能去查 `current_backend()`：
    # 两者会不一致（控制表说 sqlite、进程里连的却是 MySQL），而这是人决定
    # 「要不要覆盖账本」的唯一一处。说错库，人确认的就是另一件事。
    target = get_engine()
    say(f"即将把 {snapshot.name} 恢复到：{target.url.render_as_string(hide_password=True)}")
    say("这会先清空这个库的全部业务表，再写入快照里的内容。")
    if not assume_yes:
        try:
            if input("确认继续？输入 yes 回车：").strip().lower() != "yes":
                say("已取消，什么都没动。")
                return 1
        except (EOFError, KeyboardInterrupt):
            say("\n已取消，什么都没动。")
            return 1

    # 恢复之前先给当前状态留一份。恢复错了还有退路。
    #
    # **`keep=0`（这一次不轮换）是必须的，不是保守。** 安全备份默认落在
    # `_default_dir()`——跟着账本走，也就是用户放快照的**同一个** `backups/`。
    # 稳定态（cron 每天一次、keep=30）目录里正好 30 份，安全备份写进去变 31 份，
    # `_prune` 就会删掉 `snaps[30:]`——**最旧的那一份**。而人来拿最旧那份的动机，
    # 通常正是「新的几份已经是坏数据」。于是这条命令在读它之前先把它删了，
    # 下面 `shutil.copy2` 裸抛 FileNotFoundError，恢复一步没跑。
    # 屏幕上确实会有一行「已清理旧备份 <那个文件名>」，但它夹在一段备份输出里、
    # 措辞是「清理旧备份」——没有任何东西说明它就是崩溃的原因。
    # 连带删掉的还有配对的 `env-<同一时间戳>.txt`（那天的 SECRET_KEY 副本）。
    #
    # 而且这不只发生在失败那次：恢复**任何**一份快照都会驱逐最旧那份，
    # 保留窗口每恢复一次就静默少一天。README 里写的 `--keep 60` 更糟——
    # 安全备份用的是模块默认的 30，61 份里 `snaps[30:]` 是**一次删 31 份**，
    # 而这次恢复本身还成功了，用户没有任何理由回头去数文件。
    #
    # `tag` 是另一件事，理由独立：这份快照是**某一次危险操作的撤销点**，
    # 与迁移前快照 `soroban-<时间戳>-pre-<revision>.db` 同一类，
    # 被后来的 30 次日常备份轮换掉就完全失去了意义。带 tag 即永不参与轮换。
    try:
        safety, _ = make_backup(stream=out, keep=0, tag="pre-restore")
        say(f"（已先把当前账本备份到 {safety.name}，恢复错了可以用它退回来）")
    except Exception as e:  # noqa: BLE001
        say(f"⚠️ 恢复前的安全备份失败（{e}）。已中止——没有退路就不动手。")
        return 1

    # **在副本上迁移，绝不动用户那份快照。**
    # 老快照可能停在旧 revision，`replace_data` 要求两边 schema 一致，所以必须先升级。
    # 但就地升级会**改掉用户手里那份「动手之前的拷贝」**——而迁移前快照的全部意义
    # 正是「保住动手之前的样子」。更糟的情况很具体：如果要撤销的那次迁移**删过一列**，
    # 就地升级等于在快照上把同一列再删一次，那一列的数据就永远拿不回来了。
    # 拷一份到临时目录再升级，原件保持原样，人还能从它里面把东西捞出来。
    with tempfile.TemporaryDirectory(prefix="soroban-restore-") as td:
        working = Path(td) / snapshot.name
        shutil.copy2(snapshot, working)
        src = build_engine(f"sqlite:///{working}")
        try:
            run_migrations(f"sqlite:///{working}")
            with barrier.hold("恢复备份中"):
                counts = replace_data(src, target)
        finally:
            src.dispose()
    say(f"已恢复 {sum(counts.values())} 行。")
    return 0


def snapshot_sqlite_file(src_url: str, dest_dir: Path, tag: str) -> Path:
    """对 SQLite 账本做**文件级**在线备份（sqlite3 自带的 backup API）。返回快照路径。

    **为什么迁移前快照不能走 `replace_data`**：那个函数按**模型声明的列**去 SELECT，
    而迁移前的库还停在**旧 schema** 上。只要这次待跑的迁移加了一列（绝大多数迁移都是），
    读的时候那一列还不存在 ⇒ `no such column` ⇒ 快照失败。
    也就是说：安全网恰恰在**唯一真正需要它**的时候不在。
    （2026-08-22 实测：迁到倒数第二个修订号再跑一次，当场
     `no such column: pluginconfig.last_ok_at`，一份快照都没留下。）

    `Connection.backup()` 拷的是**磁盘上真实的那个库**，不认识也不需要认识 schema，
    WAL 里的内容也一并带走，并且是在线一致的（不需要停写）。
    这正是「动手之前那一刻的完整拷贝」该有的语义。

    日常备份仍然走 `replace_data`：那条路要能 SQLite↔MySQL 双向、要产出
    **当前 schema** 的可移植快照。两种机制的分界很清楚——
    **只有迁移前这一种情况，源库的 schema 是故意与模型不一致的。**
    """
    import sqlite3

    from sqlalchemy.engine import make_url

    src_path = make_url(src_url).database
    if not src_path or src_path == ":memory:":
        raise ValueError(f"这不是一个文件型 SQLite 库：{src_url}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime(_STAMP_FMT)
    final = dest_dir / f"soroban-{stamp}-{tag}.db"
    part = final.with_suffix(".db.part")
    part.unlink(missing_ok=True)

    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)                     # 在线一致拷贝，不需要停写
        finally:
            dst.close()
    except BaseException:
        part.unlink(missing_ok=True)            # 失败不留残file
        raise
    finally:
        src.close()

    # **必须走 `runtime_dir()`，不能用 `__file__`。** 打包之后 `__file__` 落在 PyInstaller
    # 的临时解包目录里，而 `.env` 在 exe 旁边——`is_file()` 于是恒为 False，
    # `.env` 备份**静默地根本不发生**。而它存在的唯一理由就是保住 SECRET_KEY，
    # 偏偏打包版才是分发形态（也就是最不可能另有一份副本的那种部署）。
    env = runtime_dir() / ".env"
    if env.is_file():
        shutil.copy2(env, dest_dir / f"env-{stamp}-{tag}.txt")
    part.rename(final)
    return final
