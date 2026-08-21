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
