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
from pathlib import Path
from typing import Optional

_STAMP_FMT = "%Y%m%d-%H%M%S"
# 轮换只认**本模块自己造的**这个确切形状。刻意不用 `soroban-*.db` 这种宽匹配：
# 同一个目录里躺着 `soroban-20260808-093421-pre-<revision>.db` 这种「动库之前」的快照，
# 那是整个目录里最不该被自动删掉的文件，而宽匹配正好会吃掉它。
_MINE = re.compile(r"^soroban-\d{8}-\d{6}\d{0,2}\.db$")   # 末尾两位是同秒撞名时的序号


def _default_dir() -> Path:
    """默认落点：`backend/backups/`——迁移前快照本来就落在这里，备份归拢到一处。"""
    return Path(__file__).resolve().parent.parent / "backups"


def make_backup(out_dir: Optional[Path] = None, *, stream=None,
                keep: int = 30) -> tuple[Path, dict]:
    """备份一次。返回 (快照文件路径, 各表行数)。

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
        part = dest_dir / f"soroban-{stamp}.db.part"
        final = dest_dir / f"soroban-{stamp}.db"
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

    run_migrations(f"sqlite:///{part}")
    dst = build_engine(f"sqlite:///{part}")
    try:
        # **拷贝期间必须挂只读屏障**：SQLite 侧没有读快照（pysqlite 只在写之前才 BEGIN，
        # SELECT 一律跑在 autocommit 下），期间的写入会产生**撕裂的拷贝**
        # ——订单拷过去了、它的物品还没拷。详见 app/maintenance.py。
        with barrier.hold("备份中"):
            counts = replace_data(get_engine(), dst)
    finally:
        dst.dispose()
    part.rename(final)

    env = Path(__file__).resolve().parent.parent / ".env"
    if env.is_file():
        # `SECRET_KEY` 丢了，已保存的 MySQL 连接串就再也解不开。
        shutil.copy2(env, dest_dir / f"env-{stamp}.txt")
        say(f"  同时备份了 .env（{dest_dir / f'env-{stamp}.txt'}）——它里面的 SECRET_KEY "
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
    try:
        safety, _ = make_backup(stream=out)
        say(f"（已先把当前账本备份到 {safety.name}，恢复错了可以用它退回来）")
    except Exception as e:  # noqa: BLE001
        say(f"⚠️ 恢复前的安全备份失败（{e}）。已中止——没有退路就不动手。")
        return 1

    src = build_engine(f"sqlite:///{snapshot}")
    try:
        run_migrations(f"sqlite:///{snapshot}")   # 老快照可能停在旧 revision
        with barrier.hold("恢复备份中"):
            counts = replace_data(src, target)
    finally:
        src.dispose()
    say(f"已恢复 {sum(counts.values())} 行。")
    return 0
