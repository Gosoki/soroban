"""备份与恢复：**往返一次**，不是「备份命令没报错」。

这个仓库原来有一个 `backup.sh`，但它两种模式下都跑不起来：
第 19 行 `command -v sqlite3 || exit 1`，而部署机上 `sqlite3` 与 `mysqldump` **两个都没装**；
crontab 里也没有任何备份任务。**「有一个备份脚本」和「有备份」是两回事。**

比没有备份更危险的是**假的安全感**：一个天天返回退出码 0、备的却是错东西的 cron。
所以这里的守卫必须是往返的——只断言「备份函数没抛异常」等于什么都没测。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.backup import make_backup, restore
from app.database import build_engine, run_migrations
from app.models import MiscExpense, Order, OrderItem, ShipmentOrder


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """一个独立的临时账本（不碰会话共享库，也绝不碰真库）。"""
    url = f"sqlite:///{tmp_path / 'ledger.db'}"
    run_migrations(url)
    eng = build_engine(url)

    from app import backup as mod

    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "backups")
    # `make_backup` / `restore` 用的是 app.database.get_engine —— 换成这本临时账本
    import app.database as dbmod

    monkeypatch.setattr(dbmod, "get_engine", lambda: eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def frozen_clock(monkeypatch):
    """把 `make_backup` 用的时钟钉死，让「同一秒」这个前提**确实成立**。

    不钉的话前提是碰运气的：每次备份都要跑一遍 alembic，两次调用多半落在不同的秒里，
    于是撞名分支根本走不到——测试照样绿，却什么都没测。
    """
    import types

    from app import backup as mod

    fixed = dt.datetime(2026, 8, 19, 12, 0, 0)

    class _Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(mod, "dt", types.SimpleNamespace(datetime=_Frozen))
    return fixed


def _fill(eng, *, title: str, jpy: int) -> None:
    with Session(eng) as s:
        o = Order(date=dt.date(2027, 5, 1), title=title, order_no=title,
                  purchase_status="待收货", jpy_override=jpy)
        o.items = [OrderItem(name="物品甲", quantity=2, unit_price_cny=None, auto=True),
                   OrderItem(name="物品乙", quantity=1, unit_price_cny=None, auto=True)]
        o.compute_money()
        s.add(o)
        s.add(ShipmentOrder(date=dt.date(2027, 5, 2), shipment_no="SHIP-" + title))
        s.add(MiscExpense(date=dt.date(2027, 5, 3), name="杂项-" + title))
        s.commit()


def _titles(eng) -> list[str]:
    with Session(eng) as s:
        return sorted(o.title for o in s.exec(select(Order)).all())


def test_backup_then_restore_brings_the_ledger_back(ledger, tmp_path):
    """**往返**：备份 → 把账本改坏 → 恢复 → 数据回来了。

    只测「备份文件生成了」是不够的：本项目最怕的失败形态正是
    「备份天天成功、真要用的时候才发现备的是错东西」。
    """
    _fill(ledger, title="备份前", jpy=1234)
    snap, counts = make_backup(tmp_path / "out")
    assert snap.is_file(), "快照文件没生成"
    assert counts["orders"] == 1 and counts["orderitem"] == 2, counts

    # 把账本改成另一副样子（模拟「出事了」）
    with Session(ledger) as s:
        for o in s.exec(select(Order)).all():
            s.delete(o)
        s.commit()
    _fill(ledger, title="出事之后", jpy=9999)
    assert _titles(ledger) == ["出事之后"]

    assert restore(snap, assume_yes=True) == 0
    assert _titles(ledger) == ["备份前"], "恢复之后数据没回来"
    with Session(ledger) as s:
        o = s.exec(select(Order)).one()
        assert o.jpy_override == 1234
        assert sorted(i.name for i in o.items) == ["物品乙", "物品甲"], "物品子表没跟着回来"
        assert len(s.exec(select(ShipmentOrder)).all()) == 1
        assert len(s.exec(select(MiscExpense)).all()) == 1


def test_restore_takes_a_safety_copy_first(ledger, tmp_path, monkeypatch):
    """恢复之前先把**当前**状态也备一份——「恢复错了」不该是没有退路的一步。"""
    _fill(ledger, title="旧的", jpy=1)
    snap, _ = make_backup(tmp_path / "out")
    with Session(ledger) as s:
        for o in s.exec(select(Order)).all():
            s.delete(o)
        s.commit()
    _fill(ledger, title="现在的", jpy=2)

    from app import backup as mod

    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "safety")
    assert restore(snap, assume_yes=True) == 0
    safety = sorted((tmp_path / "safety").glob("soroban-*.db"))
    assert safety, "恢复前没有留安全备份"
    # 那份安全备份里应该是**恢复之前**的样子
    eng = build_engine(f"sqlite:///{safety[-1]}")
    try:
        assert _titles(eng) == ["现在的"]
    finally:
        eng.dispose()


def test_a_snapshot_never_carries_the_control_tables(ledger, tmp_path):
    """快照里**不许**有控制表（`app_db_config` / `db_connection`）。

    它们记的是「当前连哪个库」和**加密的 MySQL 连接串**。跟着快照走的话：
    恢复一份旧快照会顺带把「你正连着哪个库」也还原回去，而这两件事没有任何关系；
    连接串也会被复制到备份目录里（而备份往往会被拷到别处）。
    这一条今天成立是因为控制表用的是独立 MetaData，不在 `SQLModel.metadata` 里——
    那是设计的一部分，所以钉住它。
    """
    from sqlalchemy import inspect

    _fill(ledger, title="X", jpy=1)
    snap, _ = make_backup(tmp_path / "out")
    eng = build_engine(f"sqlite:///{snap}")
    try:
        names = set(inspect(eng).get_table_names())
    finally:
        eng.dispose()
    assert "orders" in names, "快照里连业务表都没有"
    assert not ({"app_db_config", "db_connection"} & names), \
        f"快照里带上了控制表：{sorted(names)}"


def test_backup_keeps_only_the_recent_ones_and_touches_nothing_else(ledger, tmp_path):
    """轮换只删**本模块自己造的**那种文件名，绝不碰目录里别的东西。

    备份目录里躺着两类**不是本模块造的**文件：用户手敲的东西，以及迁移工具留下的
    `soroban-<时间戳>-pre-<revision>.db`（「动库之前」的快照）。后者才是真陷阱——
    它长得跟本模块的产物几乎一样，`soroban-*.db` 这种宽匹配会一口吃掉，
    而它恰恰是整个目录里最不该被自动删的文件。
    """
    out = tmp_path / "out"
    out.mkdir()
    others = {
        out / "我自己放的.sql": "别动我",
        out / "soroban-20260808-093421-pre-f8a9b0c1d2e3.db": "迁移前快照",
        out / "soroban-手写备注.db": "名字像但不是我造的",
    }
    for f, text in others.items():
        f.write_text(text, encoding="utf-8")

    _fill(ledger, title="X", jpy=1)
    make_backup(out, keep=2)
    # 时间戳精确到秒，同一秒内跑两次会重名——直接造几个旧文件来测轮换更稳
    for i in range(3):
        (out / f"soroban-2020010{i}-000000.db").write_text("", encoding="utf-8")
        (out / f"env-2020010{i}-000000.txt").write_text("", encoding="utf-8")
    make_backup(out, keep=2)

    from app.backup import _MINE

    # 先断言「别人的文件」——顺序是有意的：宽匹配的轮换会同时踩坏两条断言，
    # 而先红的那条决定了失败信息说的是哪件事。这里真正的伤害是删了别人的备份。
    for f, text in others.items():
        assert f.is_file() and f.read_text(encoding="utf-8") == text, \
            f"把不是自己造的文件删了：{f.name}"
    # 用实现里的 `_MINE` 而不是在测试里另抄一份正则——抄一份正是两边会漂的原因
    mine_left = sorted(p.name for p in out.iterdir() if _MINE.match(p.name))
    assert len(mine_left) == 2, f"轮换没生效：{mine_left}"


def test_the_confirmation_names_the_db_it_will_actually_overwrite(tmp_path, monkeypatch, capsys):
    """确认提示必须点名**真正要写的那个库**，而且不能把密码打出来。

    这句话是人决定「要不要覆盖账本」的唯一一处。它一度是去查 `current_backend()`
    （控制表里记的后端），而真正要写的是 `get_engine()`——两者会不一致：
    2026-08-19 的恢复演练里，提示说「sqlite」而实际写进了 MySQL。
    说错库，人确认的就是另一件事。
    """
    import builtins

    import app.database as dbmod

    eng = build_engine("mysql+pymysql://u:hunter2@10.0.0.9:3306/some_db?charset=utf8mb4")
    monkeypatch.setattr(dbmod, "get_engine", lambda: eng)
    # 让 current_backend 说另一套——如果实现回头去查它，这条就会红
    monkeypatch.setattr(dbmod, "current_backend", lambda: "sqlite")
    monkeypatch.setattr(builtins, "input", lambda *_: "no")

    snap = tmp_path / "soroban-20260101-000000.db"
    snap.write_bytes(b"")
    try:
        assert restore(snap) == 1, "回答 no 之后不该继续"
    finally:
        eng.dispose()
    said = capsys.readouterr().out
    assert "some_db" in said, f"提示没点名真正要写的库：{said}"
    assert "hunter2" not in said, f"提示把密码打出来了：{said}"


def test_two_backups_in_the_same_second_do_not_clobber_each_other(ledger, tmp_path, frozen_clock):
    """同一秒里连备两次要得到**两份**快照，而不是后者把前者盖掉。

    时间戳只精确到秒，而这套系统同时会有 2–3 个人在用。撞名的后果不是「少一份备份」，
    而是后来者把前者写了一半的 `.part` 删掉——两边都可能坏。
    """
    _fill(ledger, title="X", jpy=1)
    out = tmp_path / "out"
    first, _ = make_backup(out)
    second, _ = make_backup(out)
    assert first != second, "同一秒的两次备份撞到同一个文件名了"
    assert first.is_file() and second.is_file()
    # 两份都要是能打开、有数据的库——不能只是名字不同
    for f in (first, second):
        eng = build_engine(f"sqlite:///{f}")
        try:
            assert _titles(eng) == ["X"], f"{f.name} 里没有数据"
        finally:
            eng.dispose()


def test_rotation_also_covers_the_same_second_names(ledger, tmp_path, frozen_clock):
    """撞名时补的序号后缀，轮换也必须认得——否则那些备份会永远堆着不清。

    这条是 `_MINE` 正则和撞名后缀两处必须对齐的地方：它们分别在文件的两头，
    改一处忘了另一处不会有任何报错，只会在几个月后表现为「磁盘满了」。
    """
    import re

    from app.backup import _MINE

    out = tmp_path / "out"
    out.mkdir()
    _fill(ledger, title="X", jpy=1)
    make_backup(out)
    make_backup(out)   # 同一秒 → 带序号后缀
    names = sorted(p.name for p in out.glob("soroban-*.db"))
    assert len(names) == 2, names
    for n in names:
        assert _MINE.match(n), f"轮换认不出自己造的这个名字：{n}"
    # 而迁移工具留下的那种名字仍然不许被认作「自己的」
    assert not _MINE.match("soroban-20260808-093421-pre-f8a9b0c1d2e3.db")


# --- HTTP 入口 -----------------------------------------------------------------

def test_the_backup_endpoint_backs_up_and_lists(client, tmp_path, monkeypatch):
    """点一下就能备份 —— 打包成 exe 之后 `tools/` 根本不存在，命令行不是入口。"""
    from app import backup as mod

    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "ui")

    r = client.post("/api/db/backups")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file"].startswith("soroban-") and body["file"].endswith(".db")
    assert body["total"] >= 1 and body["counts"], body

    r2 = client.get("/api/db/backups")
    assert r2.status_code == 200
    names = [i["name"] for i in r2.json()["items"]]
    assert body["file"] in names, names
    assert all(i["bytes"] > 0 for i in r2.json()["items"])


def test_there_is_no_http_way_to_restore(client):
    """**恢复刻意没有 HTTP 入口**：那是唯一一条能一键清空账本的操作。

    这不是「还没做」，是设计决定——所以钉住它，免得哪天有人顺手加上去。
    备份可以点，恢复要走命令行、要手敲 yes。
    """
    from app.main import app

    paths = app.openapi()["paths"]
    offenders = [p for p in paths if "restore" in p.lower()]
    assert not offenders, f"有人给恢复开了 HTTP 入口：{offenders}"


def test_the_backup_endpoint_needs_login(anon):
    """备份会把整本账读一遍，未登录不许调。"""
    assert anon.post("/api/db/backups").status_code in (401, 403)
    assert anon.get("/api/db/backups").status_code in (401, 403)


def test_two_people_backing_up_at_once_get_409_not_500(client, tmp_path, monkeypatch):
    """两个人同时点备份 → 后一个拿 409（「现在轮不到」），不是 500（「出错了」）。

    这套系统同时会有 2–3 个人在用，撞上是常态而不是异常。500 会让人以为备份坏了，
    而实际只需要过几秒再点一次。
    """
    from app import backup as mod
    from app.maintenance import barrier

    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "busy")
    with barrier.hold("测试占住"):
        r = client.post("/api/db/backups")
    assert r.status_code == 409, f"{r.status_code} {r.text}"
    assert "维护操作" in r.json()["detail"], r.text
    # 屏障放开之后照样能备
    assert client.post("/api/db/backups").status_code == 200


# --- 迁移前的自动撤销点 ---------------------------------------------------------

def _fresh_db(tmp_path, name="live.db"):
    """一个已经迁到最新、并且装着数据的库。"""
    url = f"sqlite:///{tmp_path / name}"
    run_migrations(url)
    eng = build_engine(url)
    _fill(eng, title="要保住的", jpy=777)
    return url, eng


def test_a_snapshot_is_taken_before_a_migration_actually_runs(tmp_path, monkeypatch):
    """真要跑迁移时，先给当前账本留一个**撤销点**。

    这个应用**每次启动都自动 `alembic upgrade head`**，而分发形态是双击运行的 exe——
    没有终端、没有人会先手工备份。而代码自己就写着：MySQL 的 DDL 是隐式提交的，
    迁移链跑到一半失败时库停在半升级态。那种时刻最需要的就是「动手之前那一刻的完整拷贝」。
    """
    import app.database as db

    url, eng = _fresh_db(tmp_path)
    try:
        monkeypatch.setattr(db, "_data_url", url)
        monkeypatch.setattr(db, "pending_revision", lambda _u: "abc123")   # 假装差一步
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        # 落点跟的是 settings.DATABASE_URL（conftest 的临时库），钉到本用例自己的目录
        import app.backup as bk

        monkeypatch.setattr(bk, "_default_dir", lambda: tmp_path / "backups")
        db._snapshot_before_migrating()
    finally:
        eng.dispose()

    snaps = sorted((tmp_path / "backups").glob("soroban-*-pre-abc123.db"))
    assert snaps, f"没留下迁移前快照：{sorted(p.name for p in (tmp_path / 'backups').glob('*'))}"
    got = build_engine(f"sqlite:///{snaps[-1]}")
    try:
        assert _titles(got) == ["要保住的"], "快照里没有动手之前那份数据"
    finally:
        got.dispose()


def test_no_snapshot_when_there_is_nothing_to_migrate(tmp_path, monkeypatch):
    """已经是最新就不留——绝大多数启动都是无事发生的，每次都拷会把备份目录塞满。"""
    import app.database as db

    url, eng = _fresh_db(tmp_path)
    try:
        monkeypatch.setattr(db, "_data_url", url)
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        import app.backup as bk

        monkeypatch.setattr(bk, "_default_dir", lambda: tmp_path / "backups")
        assert db.pending_revision(url) is None, "刚迁完的库不该还有待跑的迁移"
        db._snapshot_before_migrating()
    finally:
        eng.dispose()
    assert not (tmp_path / "backups").exists(), "无事发生的启动也留了快照"


def test_no_snapshot_for_an_empty_ledger(tmp_path, monkeypatch):
    """一行业务数据都没有 ⇒ 撤销点没有意义（全新安装，以及每次跑测试的临时库）。

    不挡这条的话，跑一次测试就会往**真实的** backups/ 里写一份快照——
    加这个功能那天当场就发生了。
    """
    import app.database as db

    url = f"sqlite:///{tmp_path / 'empty.db'}"
    run_migrations(url)
    eng = build_engine(url)
    try:
        monkeypatch.setattr(db, "_data_url", url)
        monkeypatch.setattr(db, "pending_revision", lambda _u: "abc123")
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        import app.backup as bk

        monkeypatch.setattr(bk, "_default_dir", lambda: tmp_path / "backups")
        db._snapshot_before_migrating()
    finally:
        eng.dispose()
    assert not (tmp_path / "backups").exists(), "空库也留了快照"


def test_a_failed_snapshot_does_not_block_startup(tmp_path, monkeypatch, caplog):
    """拷贝失败**不许**阻断启动——把「备份没成功」升级成「应用打不开」是更糟的失败形态。

    但必须响亮地记一条：静默地没有安全网，比明摆着没有安全网更危险。
    """
    import logging

    import app.database as db

    url, eng = _fresh_db(tmp_path)
    try:
        monkeypatch.setattr(db, "_data_url", url)
        monkeypatch.setattr(db, "pending_revision", lambda _u: "abc123")
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        import app.backup as bk

        # 打的是**这条路径真正会调的那个函数**。原先打的是 `make_backup`——
        # 迁移前快照改走文件级拷贝之后它就不再被调用了，这条测试于是变成
        # 「什么都没发生、也就什么都没记」，绿得毫无意义。
        monkeypatch.setattr(bk, "snapshot_sqlite_file",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("磁盘满了")))
        # 这里**不需要**手工把 caplog 的 handler 挂到 logger 上了。
        # 上面 `_fresh_db()` 会跑一次真迁移，而 alembic 的 `fileConfig()` 曾经会重建
        # root 的 handler 列表、把 caplog 掀掉（写这条守卫时就栽在这上面）。
        # 根因已在 `alembic/env.py` 修掉——只在 root 还没有 handler 时才由 alembic 配日志，
        # 由 `test_migrations.py::test_running_a_migration_does_not_silence_caplog` 钉住。
        with caplog.at_level(logging.WARNING):
            db._snapshot_before_migrating()          # 不许抛
    finally:
        eng.dispose()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "没有撤销点" in text, f"备份失败了却没说出来：{text[:300]}"


def test_a_tagged_snapshot_is_never_rotated_away(ledger, tmp_path):
    """带 tag 的快照（迁移前撤销点）**永远不参与轮换**。

    它是某一次升级的撤销点，被 30 次日常备份轮换掉就完全失去了意义。
    """
    out = tmp_path / "out"
    _fill(ledger, title="X", jpy=1)
    pre, _ = make_backup(out, tag="pre-deadbeef")
    for i in range(4):
        (out / f"soroban-2020010{i}-000000.db").write_text("", encoding="utf-8")
    make_backup(out, keep=1)

    assert pre.is_file(), "迁移前快照被轮换掉了"
    assert "pre-deadbeef" in pre.name


def test_the_snapshot_is_wired_into_startup_and_runs_before_the_migration(monkeypatch):
    """`migrate_to_latest()` 必须**先**留快照、**再**跑迁移。

    顺序是要命的：迁移之后再拷一份，拷到的已经是迁完的样子，等于没有撤销点。
    这条盯的是**接线**——上面几条只证明了那个函数本身好使，
    而把 `_snapshot_before_migrating()` 这一句从启动路径里删掉，它们全都照样绿。
    """
    import app.database as db

    order = []
    monkeypatch.setattr(db.control, "ensure_schema", lambda *a, **k: None)
    monkeypatch.setattr(db, "_snapshot_before_migrating", lambda: order.append("snapshot"))
    monkeypatch.setattr(db, "run_migrations", lambda url: order.append("migrate"))

    db.migrate_to_latest()

    assert order == ["snapshot", "migrate"], (
        f"启动路径没有「先快照、后迁移」：{order}\n"
        "（空列表 = 那一句被从 migrate_to_latest 里删掉了）")


def test_the_pre_migration_snapshot_works_when_the_schema_is_actually_behind(tmp_path, monkeypatch):
    """**待跑的迁移真的会改 schema 时**，快照仍然要成功。

    这是这个功能唯一真正有用的场景，而第一版在这里是坏的：
    走 `replace_data` 按**模型声明的列**去 SELECT，而迁移前的库停在旧 schema 上——
    只要这次迁移加了一列（绝大多数迁移都是），就 `no such column` ⇒ 快照失败。
    也就是说安全网恰恰在最需要它的时候不在，而且只记一条警告。

    这条把库真的迁到**倒数第二个**修订号再验，不是拿一个已经最新的库假装「差一步」。
    前一版的测试就是那么写的，所以它照样绿。
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlmodel import Session

    import app.backup as bk
    import app.database as db

    url = f"sqlite:///{tmp_path / 'old.db'}"
    cfg = Config(str(db._ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db._ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    sd = ScriptDirectory.from_config(cfg)
    head = sd.get_current_head()
    prev = sd.get_revision(head).down_revision
    assert prev, "迁移链只有一个修订号，这条测不出东西"
    command.upgrade(cfg, prev)                      # 真的停在上一版

    eng = build_engine(url)
    try:
        _fill(eng, title="迁移前的账", jpy=555)
        monkeypatch.setattr(db, "_data_url", url)
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        monkeypatch.setattr(bk, "_default_dir", lambda: tmp_path / "backups")

        assert db.pending_revision(url) == head, "没造出「库比代码旧一版」的前提"
        db._snapshot_before_migrating()
    finally:
        eng.dispose()

    snaps = sorted((tmp_path / "backups").glob(f"soroban-*-pre-{head}.db"))
    assert snaps, "库真的落后一版时，一份快照都没留下"

    # 快照必须**忠于旧 schema**、并且装着动手之前那份数据
    got = build_engine(f"sqlite:///{snaps[-1]}")
    try:
        assert _titles(got) == ["迁移前的账"]
        from sqlalchemy import inspect

        rev = None
        with got.connect() as c:
            from alembic.runtime.migration import MigrationContext

            rev = MigrationContext.configure(c).get_current_revision()
        assert rev == prev, f"快照被写成了新 schema（{rev}），那就不是「动手之前」的样子了"
    finally:
        got.dispose()


def test_the_env_copy_follows_the_runtime_dir_not_this_source_file(ledger, tmp_path, monkeypatch):
    """`.env` 要在**运行时目录**里找，不能相对本源文件找。

    打包之后 `__file__` 落在 PyInstaller 的临时解包目录，而 `.env` 在 exe 旁边——
    用 `__file__` 的话 `is_file()` 恒为 False，`.env` 备份**静默地根本不发生**。
    而它存在的唯一理由就是保住 SECRET_KEY（没有它，已保存的 MySQL 连接串再也解不开），
    偏偏打包版才是分发形态，也就是最不可能另有一份副本的那种部署。

    这里把运行时目录换成一个临时目录并在里面放一份假 `.env`：
    只有「跟着 runtime_dir 走」的实现才会把它备出来。
    """
    import app.backup as mod

    fake_home = tmp_path / "exe-dir"
    fake_home.mkdir()
    (fake_home / ".env").write_text("SECRET_KEY=从-exe-旁边读到的\n", encoding="utf-8")
    monkeypatch.setattr(mod, "runtime_dir", lambda: fake_home)

    _fill(ledger, title="X", jpy=1)
    out = tmp_path / "out"
    make_backup(out)

    copies = sorted(out.glob("env-*.txt"))
    assert copies, "没有把 .env 备份出来（多半还在用 __file__ 定位）"
    assert "从-exe-旁边读到的" in copies[-1].read_text(encoding="utf-8")


def test_restoring_an_old_snapshot_leaves_the_snapshot_file_untouched(tmp_path, monkeypatch):
    """恢复一份**停在旧 revision** 的快照，不许改动那个文件本身。

    `replace_data` 要求两边 schema 一致，所以老快照必须先升级。但**就地升级**
    会把用户手里那份「动手之前的拷贝」改掉——而迁移前快照的全部意义正是保住那个样子。
    最具体的坏处：要撤销的那次迁移如果**删过一列**，就地升级等于在快照上把同一列再删一次，
    那一列的数据就永远拿不回来了。

    判据是文件的**字节**没变，不是「能不能恢复成功」——后者两种实现都成立。
    """
    import hashlib

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import app.database as db

    # 造一份停在上一版的快照
    snap_url = f"sqlite:///{tmp_path / 'old-snap.db'}"
    cfg = Config(str(db._ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db._ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", snap_url)
    sd = ScriptDirectory.from_config(cfg)
    prev = sd.get_revision(sd.get_current_head()).down_revision
    assert prev, "迁移链只有一个修订号，这条测不出东西"
    command.upgrade(cfg, prev)
    snap_eng = build_engine(snap_url)
    try:
        _fill(snap_eng, title="快照里的账", jpy=888)
    finally:
        snap_eng.dispose()

    snap = tmp_path / "old-snap.db"
    before = hashlib.sha256(snap.read_bytes()).hexdigest()

    # 恢复到一个独立的目标库
    tgt_url = f"sqlite:///{tmp_path / 'target.db'}"
    run_migrations(tgt_url)
    tgt = build_engine(tgt_url)
    import app.backup as mod
    import app.database as dbmod

    monkeypatch.setattr(dbmod, "get_engine", lambda: tgt)
    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "safety")
    try:
        assert restore(snap, assume_yes=True) == 0
        assert _titles(tgt) == ["快照里的账"], "根本没恢复成功，这条就无从谈起"
    finally:
        tgt.dispose()

    after = hashlib.sha256(snap.read_bytes()).hexdigest()
    assert after == before, "恢复过程把用户那份快照就地改掉了（在它上面跑了一次迁移）"


def test_the_backup_list_reports_times_in_utc_like_every_other_endpoint(client, tmp_path, monkeypatch):
    """备份列表的时间戳必须是**带时区的 UTC**，与全站其它时间戳同一口径。

    原先用 `fromtimestamp(ts)`——服务器**本地**时间、不带时区，是全站唯一一处这么干的
    地方，也是唯一绕开前端 `parseUtc` 那条管线的。服务器在 JST、看的人在别的时区时，
    这一列会与页面上所有别的时间差 9 小时，而没有任何东西提示它们口径不同。

    判据是「解析回来跟文件的 mtime 对得上」，而不是字符串长什么样——
    后者换个 isoformat 参数就会红，前者才是真正要成立的东西。
    """
    import datetime as dt
    import io

    from app import backup as mod

    monkeypatch.setattr(mod, "_default_dir", lambda: tmp_path / "ui")
    r = client.post("/api/db/backups")
    assert r.status_code == 200, r.text
    name = r.json()["file"]

    items = client.get("/api/db/backups").json()["items"]
    mine = next(i for i in items if i["name"] == name)

    parsed = dt.datetime.fromisoformat(mine["mtime"])
    assert parsed.tzinfo is not None, f"时间戳不带时区：{mine['mtime']}"
    assert parsed.utcoffset() == dt.timedelta(0), f"不是 UTC：{mine['mtime']}"

    real = dt.datetime.fromtimestamp((tmp_path / "ui" / name).stat().st_mtime, dt.timezone.utc)
    assert abs((parsed - real).total_seconds()) <= 1, \
        f"报出来的时间与文件的 mtime 对不上：{parsed} vs {real}"


def test_restoring_the_oldest_snapshot_does_not_delete_it_first(tmp_path, monkeypatch):
    """恢复目录里**最旧**的那份快照——它不能被这条命令自己删掉。

    这是本仓最贵的一条：`restore()` 先跑安全备份（含轮换）、后才读快照，而安全备份
    默认落在 `_default_dir()`——跟着账本走，也就是用户放快照的**同一个** `backups/`。
    稳定态（cron 每天一次、keep=30）正好 30 份，安全备份写进去变 31 份，
    `_prune` 删 `snaps[30:]` = 最旧那一份，正是他指定要恢复的文件。
    随后 `shutil.copy2` 裸抛 FileNotFoundError，恢复一步没跑。

    人来拿最旧那份的动机，通常正是「新的几份已经是坏数据」——这条 bug 触发的那一刻，
    被删的往往是目录里唯一还好用的那份。

    **这条守卫必须让安全备份落在同一个目录**。仓库里原有的 restore 用例都把
    `_default_dir` 指到了另一处（`tmp_path/"safety"`），于是「同目录」这个**真实默认
    情形**一条用例都没覆盖——外层把前提挪走了，内层永远到不了。
    """
    import datetime as dt
    import io

    from app import backup as mod

    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(mod, "_default_dir", lambda: backups)   # ← 与快照同一个目录

    # 稳定态：正好 30 份合规命名的快照。最旧那份是真快照（下面要恢复它）。
    oldest, _ = mod.make_backup(backups, stream=io.StringIO(), keep=0)
    renamed = backups / "soroban-20200101-000000.db"
    oldest.rename(renamed)
    for i in range(1, 30):
        d = dt.date(2026, 1, 1) + dt.timedelta(days=i)
        (backups / f"soroban-{d:%Y%m%d}-030000.db").write_bytes(b"x")
    assert len(list(backups.glob("soroban-*.db"))) == 30, "前提没建立：应当正好 30 份"

    out = io.StringIO()
    try:
        rc = mod.restore(renamed, assume_yes=True, stream=out)
    except FileNotFoundError as e:
        # 真实行为就是裸抛（`tools/backup_db.py` 没有 try/except，用户看到的是 traceback）。
        # 这里把它翻译一遍：不翻译的话，下一个人看到的只是一句「路径不存在」，
        # 而**那正是这条守卫要证明的事**——文件三秒钟前还在，是这条命令自己删的。
        raise AssertionError(
            f"恢复最旧那份快照时它被自己删掉了，随后崩在读取那一步：{e}\n"
            f"屏幕输出（注意那行「已清理旧备份」）：\n{out.getvalue()}") from e

    assert renamed.exists(), (
        "恢复最旧那份快照时，它被这条命令自己的安全备份轮换删掉了——"
        f"而这正是它要读的文件。屏幕输出：\n{out.getvalue()}")
    assert rc == 0, f"恢复失败：\n{out.getvalue()}"

    # 反面：安全备份确实生成了（不能靠「压根没备份」来通过上面那条）
    safety = [p for p in backups.glob("soroban-*-pre-restore.db")]
    assert safety, f"安全备份没生成，上面那条断言是白过的：\n{out.getvalue()}"


def test_a_restore_never_evicts_anything_from_the_backup_directory(tmp_path, monkeypatch):
    """恢复**任何**一份快照，都不该顺手剪掉备份目录里的别的东西。

    上一条盯的是「崩溃」，这条盯的是**成功那次也在损失**：恢复第 5 新的那份会成功，
    而同一条命令的安全备份照样驱逐最旧那份，保留窗口每恢复一次就静默少一天。
    用户拿到的是一次成功的恢复，完全没有理由回头去数文件。

    README 里写的 `--keep 60` 让这件事更贵：安全备份用的是模块默认的 30，
    61 份里 `snaps[30:]` 是**一次删 31 份**。
    """
    import datetime as dt
    import io

    from app import backup as mod

    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(mod, "_default_dir", lambda: backups)

    real, _ = mod.make_backup(backups, stream=io.StringIO(), keep=0)
    target = backups / "soroban-20260601-030000.db"
    real.rename(target)
    # 按 README 的 --keep 60 配的目录：60 份
    for i in range(59):
        d = dt.date(2025, 1, 1) + dt.timedelta(days=i)
        (backups / f"soroban-{d:%Y%m%d}-030000.db").write_bytes(b"x")
    before = {p.name for p in backups.iterdir()}
    assert len([n for n in before if n.startswith("soroban-")]) == 60

    out = io.StringIO()
    assert mod.restore(target, assume_yes=True, stream=out) == 0, out.getvalue()

    after = {p.name for p in backups.iterdir()}
    gone = before - after
    assert not gone, (
        f"一次恢复顺手删掉了 {len(gone)} 份备份：{sorted(gone)[:5]}…\n"
        "轮换是备份策略的一部分，该由例行备份执行；恢复只该**增加**一份退路")
