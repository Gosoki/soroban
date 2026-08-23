"""数据库管理层：迁移表清单完整性、整库拷贝、方言助手、端点守卫。

不需要真 MySQL——拷贝走 SQLite→SQLite（replace_data 与方向无关），MySQL 专属逻辑只做纯函数校验。
"""
import datetime as dt
import re
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

from app.database import build_engine, run_migrations
from app.db import control
from app.models import Order, OrderItem, ShipmentOrder, User
from app.services import db_migrate

_REPO_BACKEND = Path(__file__).resolve().parents[1]

# 控制表恒留在 SQLite、不属于 SQLModel.metadata，故不参与业务迁移
_CONTROL_TABLES = {"app_db_config", "db_connection", "alembic_version"}


def test_migration_order_covers_every_business_table():
    """漏一张表 = 迁到 MySQL 后那张表静默为空（数据丢失且不报错）。"""
    declared = {m.__tablename__ for m in db_migrate.MIGRATION_ORDER}
    actual = set(SQLModel.metadata.tables) - _CONTROL_TABLES
    assert declared == actual, f"迁移清单与实际表不一致：缺 {actual - declared}，多 {declared - actual}"


def test_standalone_script_matches_service_order():
    """scripts/migrate_sqlite_to_mysql.py 里另有一份 MIGRATION_ORDER，两份必须同表同序。"""
    text = (_REPO_BACKEND / "scripts" / "migrate_sqlite_to_mysql.py").read_text(encoding="utf-8")
    m = re.search(r"MIGRATION_ORDER\s*=\s*\[(.*?)\]", text, re.S)
    assert m
    names = [n for n in re.findall(r"\b([A-Z]\w+)\b", m.group(1))]
    assert names == [c.__name__ for c in db_migrate.MIGRATION_ORDER]


def test_migration_order_respects_foreign_keys():
    """按外键依赖排序：任何表的被引用方必须排在它前面（否则拷贝时插子行先于父行）。"""
    pos = {m.__tablename__: i for i, m in enumerate(db_migrate.MIGRATION_ORDER)}
    for model in db_migrate.MIGRATION_ORDER:
        for fk in model.__table__.foreign_keys:
            target = fk.column.table.name
            if target == model.__tablename__:          # 自引用，跳过
                continue
            assert pos[target] < pos[model.__tablename__], (
                f"{model.__tablename__} 依赖 {target}，但排在它前面")


@pytest.fixture()
def dst_engine(tmp_path):
    """一个建好 schema 的空目标库。"""
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    run_migrations(url)
    e = build_engine(url)
    yield e
    e.dispose()


def test_copy_data_roundtrip(client, dst_engine):
    from app.database import get_engine

    client.post("/api/orders", json={
        "date": "2027-01-01", "order_no": "CP-1", "platform": "淘宝", "title": "拷贝测试",
        "items": [{"name": "甲", "quantity": 2, "unit_price_cny": "3.00"}]})
    counts = db_migrate.replace_data(get_engine(), dst_engine)
    assert counts["orders"] > 0 and counts["orderitem"] > 0
    with Session(dst_engine) as d:
        o = d.exec(select(Order).where(Order.order_no == "CP-1")).one()
        assert o.title == "拷贝测试"
        assert [i.name for i in d.exec(select(OrderItem).where(OrderItem.order_id == o.id)).all()] == ["甲"]
        assert d.exec(select(User)).first() is not None      # 用户表也搬（含密码哈希）


def test_copy_data_is_idempotent_overwrite(client, dst_engine):
    """整表覆盖：连拷两次，目标不该翻倍。"""
    from app.database import get_engine

    first = db_migrate.replace_data(get_engine(), dst_engine)
    second = db_migrate.replace_data(get_engine(), dst_engine)
    assert first == second
    with Session(dst_engine) as d:
        assert len(d.exec(select(Order)).all()) == second["orders"]


def test_migrating_onto_a_non_empty_target_needs_an_explicit_confirmation(
        client, dst_engine, monkeypatch):
    """目标库里已有的数据会被 `replace_data` 逐表 delete 掉，所以**必须先说清楚要删什么**。

    在此之前全仓没有任何一处比较源库与目标库谁更新：`_is_same_as_active` 只回答
    「目标是不是当前正在用的那个」，`switch` 里那道指纹闸只回答「源库比上次迁移时新不新」
    ——**看不见反方向**。于是有一条全程走本应用自己指引的路会毁掉整个账本：

        MySQL 连不上 → 按 database.py 的提示 `--use-local-db` 退回本地（停在切走那天）
        → 停机期间在本地补记几单（rescue.py 正是这么建议的）
        → MySQL 恢复后点「切换」→ 源库有改动 ⇒ 409
        → 弹窗的**默认按钮**「重新迁移再切换」⇒ 直接调 migrate（那条路原先一句确认都没有）
        → 用几个月前的本地快照覆盖掉 MySQL 上几个月的账本，单事务提交、无备份可退。

    `_resolve_target` 是这里唯一被替换的东西（把目标指到临时库），
    migrate 的判定逻辑本身跑的是真代码。
    """
    from app.database import get_engine
    from app.routers import dbadmin as mod

    # **自己造一行商品订单**，别指望别的测试文件先建过。
    # 第一版没有这一句：整套跑是绿的（`test_edge_cases` 等先建了订单），
    # 单跑就红——detail 里只有「用户 1 条」。而「有没有把对面的东西列出来」
    # 恰恰是这条测试真正想钉的那一半。
    client.post("/api/orders", json={"date": "2027-04-01", "title": "覆盖闸的证据"})
    db_migrate.replace_data(get_engine(), dst_engine)       # 让目标库变成非空
    monkeypatch.setattr(mod, "_resolve_target",
                        lambda t: ("sqlite", "sqlite:///dst", dst_engine, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, url: False)
    monkeypatch.setattr(mod, "run_migrations", lambda url: None)

    r = client.post("/api/db/migrate", json={"backend": "sqlite"})
    assert r.status_code == 409, f"目标非空却直接开拷了：{r.status_code} {r.text[:200]}"
    detail = r.json()["detail"]
    assert "无法撤销" in detail
    assert "商品订单" in detail, f"没把「对面现在有什么」列出来，用户没法判断值不值得覆盖：{detail}"

    # **反面一**：用户明确确认之后必须放行，否则这道闸就成了死路。
    r2 = client.post("/api/db/migrate", json={"backend": "sqlite", "confirm_overwrite": True})
    assert r2.status_code == 200, r2.text


def test_the_overwrite_409_is_recognisable(client, dst_engine, monkeypatch):
    """前端要靠 detail 里这句话把「目标库里已经有数据」与另一种 409
    （「已有另一项维护操作在进行」）分开——不分的话，用户会在一条讲维护中的消息上
    点下「仍然覆盖」，而重试带上 `confirm_overwrite: true` 正好**跳过了这道闸本身**。
    所以这句话是一份跨前后端的契约，钉在这里。
    """
    from app.database import get_engine
    from app.routers import dbadmin as mod

    client.post("/api/orders", json={"date": "2027-04-02", "title": "证据"})
    db_migrate.replace_data(get_engine(), dst_engine)
    monkeypatch.setattr(mod, "_resolve_target",
                        lambda t: ("sqlite", "sqlite:///dst", dst_engine, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, url: False)
    monkeypatch.setattr(mod, "run_migrations", lambda url: None)

    r = client.post("/api/db/migrate", json={"backend": "sqlite"})
    assert r.status_code == 409
    assert "目标库里已经有数据" in r.json()["detail"], (
        "前端认的就是这句话（Database/index.vue 的 migrateWithOverwriteGuard），"
        "改了要一起改")


def test_migrating_onto_an_empty_target_is_not_interrupted(client, dst_engine, monkeypatch):
    """**反面二**：目标是空库时没有任何东西可丢，不该拿一次确认去打断用户。

    少了这一条，把闸写成「一律要 confirm_overwrite」也能过上面那条。
    """
    from app.routers import dbadmin as mod

    monkeypatch.setattr(mod, "_resolve_target",
                        lambda t: ("sqlite", "sqlite:///dst", dst_engine, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, url: False)
    monkeypatch.setattr(mod, "run_migrations", lambda url: None)
    assert db_migrate.is_target_empty(dst_engine) is True, "夹具不该是非空的"

    r = client.post("/api/db/migrate", json={"backend": "sqlite"})
    assert r.status_code == 200, r.text


def test_preflight_catches_integers_mysql_cannot_store(client, dst_engine, monkeypatch):
    """`preflight` 原先只体检 VARCHAR 长度与 DECIMAL 范围，**漏了整数列**。

    SQLite 是弱类型，`jpy_override` 之类能静默存下超过 2^31 的值；MySQL 的 INT 是
    4 字节，插入时抛 `1264 Out of range`。而 `replace_data` 是**单事务**——一行踩雷
    整批回滚，用户只看到一句「拷贝「集运订单」表时失败：(1264, ...)」，
    既不知道是哪一行也不知道该改什么。那正是 preflight 存在的理由。

    入口层的卡口（`schemas._bounded_jpy`）只保证**今后**写不进越界值，
    对那道卡口存在**之前**就躺在库里的历史行无能为力。
    """
    from app.database import get_engine

    with Session(get_engine()) as s:
        # table=True 的 SQLModel 不跑校验——这正是历史脏行的来路
        row = ShipmentOrder(date=dt.date(2027, 3, 3), jpy_override=3_000_000_000)
        s.add(row)
        s.commit()
        dirty_id = row.id

    # preflight 只在目标是 MySQL 时才干活；这里把目标的方言名改掉，
    # 判定逻辑本身跑的是真代码（查询照常打在 SQLite 源库上）。
    monkeypatch.setattr(dst_engine.dialect, "name", "mysql")
    problems = db_migrate.preflight(get_engine(), dst_engine)
    assert any("jpy_override" in p and f"#{dirty_id}" in p for p in problems), \
        f"越界的整数没被体检出来，迁移会在拷到一半时炸：{problems}"

    # **反面**：把那行改回正常值之后不许再报——判据不能写成「有整数列就报」。
    with Session(get_engine()) as s:
        r = s.get(ShipmentOrder, dirty_id)
        r.jpy_override = 12345
        s.add(r)
        s.commit()
    assert not [p for p in db_migrate.preflight(get_engine(), dst_engine)
                if "jpy_override" in p], "正常的整数也被报成越界"


def test_is_target_empty(tmp_path):
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    run_migrations(url)
    e = build_engine(url)
    try:
        assert db_migrate.is_target_empty(e) is True
        with Session(e) as s:
            # table=True 的 SQLModel 不跑校验，date 必须给真的 date 对象
            s.add(ShipmentOrder(date=dt.date(2027, 1, 1)))
            s.commit()
        assert db_migrate.is_target_empty(e) is False
    finally:
        e.dispose()


# --- 切换前的 schema 守门 -------------------------------------------------------

def _stamp_at(url: str, revision: str) -> None:
    """把一个库升到指定的中间版本（模拟「切走那天的 schema」）。"""
    from alembic import command

    from tests.test_migrations import _cfg

    command.upgrade(_cfg(url), revision)


def test_switch_upgrades_stale_target_schema(client, tmp_path, monkeypatch):
    """目标库停在旧 revision 时，switch 必须先把它升到 head。

    不升的后果：切过去之后 /api/orders 等全部 500（缺列 1054 / 缺表 1146，都不是
    IntegrityError/ValueError，没有对应 handler）。而用户最自然的自救动作是「迁移到此库」，
    那会用源库快照覆盖目标——把「schema 落后」升级成「数据被旧快照盖掉」。"""
    from sqlalchemy import inspect as sa_inspect

    from app.routers import dbadmin as mod

    url = f"sqlite:///{tmp_path / 'stale.db'}"
    _stamp_at(url, "b8c9d0e1f2a3")                   # 改名之前的老 schema
    e = build_engine(url)
    try:
        cols = {c["name"] for c in sa_inspect(e).get_columns("orders")}
        assert "shop" in cols and "title" not in cols, "夹具没造出「落后的 schema」"
        with Session(e) as s:                        # 塞一行，好让 is_target_empty 为 False
            s.add(User(username="u", password_hash="x"))
            s.commit()
    finally:
        e.dispose()

    # ⚠️ 这条测试原先**完全是空转的**，三处叠加：
    #   ① body 里那个 `sqlite_path` 根本不是 `Target` 的字段（pydantic 默认 extra=ignore）
    #      ⇒ 目标恒指向控制库 ⇒ `_is_same_as_active` 直接 400，switch 一个字都没执行到；
    #   ② 断言之前**测试自己**跑了一次 `run_migrations(url)` ⇒ 后面「已升到 head」必然成立；
    #   ③ 收尾是 `assert r.status_code in (200, 400)` ⇒ 等于没有断言。
    # 于是把 switch 里那句 run_migrations 整个删掉，这条照样绿。
    # 现在改成：把目标指到那个落后的库（只换解析目标，switch 的判定逻辑跑真代码），
    # 而且**绝不自己升级**——升没升全看 switch。
    tgt = build_engine(url)
    monkeypatch.setattr(mod, "_resolve_target", lambda t: ("sqlite", url, tgt, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, u: False)
    try:
        # `confirm_changed` 是**必须显式给**的：本文件里前面那条迁移用例成功之后会写下
        # 一条 `migrate_state` 指纹，而此后别的用例又往源库里写了行 ⇒ 这里会被
        # 「迁移之后源库又有改动」那道闸 409 掉。那道闸不是这条用例要测的东西，
        # 不给它就会变成「单跑绿、整套红」的用例间污染。
        r = client.post("/api/db/switch", json={"backend": "sqlite", "confirm_changed": True})
        assert r.status_code == 200, r.text
    finally:
        tgt.dispose()

    e = build_engine(url)
    try:
        cols = {c["name"] for c in sa_inspect(e).get_columns("orders")}
        assert "title" in cols and "shop" not in cols, \
            "switch 没有把落后的目标库升到 head —— 切过去之后全站 500，"\
            "而用户最自然的自救动作会把数据一起盖掉"
    finally:
        e.dispose()


def test_switching_to_an_unsupported_server_is_refused_before_any_ddl(
        client, dst_engine, monkeypatch):
    """版本闸原先只挂在 test 与 migrate 两处，`switch` 漏了——而它第一件事就是对目标库
    `run_migrations`。界面上三个按钮平级、没有任何东西强制「先测试」，于是跳过测试
    直接点「切换到此库」= 对一台 MariaDB / 5.7 跑完整 alembic 链。

    MySQL 的 DDL 是**隐式提交**的：跑到 `utf8mb4_0900_bin` 那条炸掉时，前面十几条
    已经落地，库停在半升级态，只能手工 DROP DATABASE 才能重来。
    所以断言的重点不是「返回 400」，而是**一条 DDL 都还没跑**。
    """
    from app.routers import dbadmin as mod

    ddl = []
    monkeypatch.setattr(mod, "_resolve_target",
                        lambda t: ("mysql", "mysql://x", dst_engine, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, url: False)
    monkeypatch.setattr(mod, "_mysql_conn_fields", lambda t: ("h", 3306, "u", "p", "d"))
    monkeypatch.setattr(mod.db_migrate, "test_connection", lambda *a, **k: (True, "5.7.44"))
    monkeypatch.setattr(mod, "run_migrations", lambda url: ddl.append(url))

    r = client.post("/api/db/switch",
                    json={"backend": "mysql", "host": "h", "user": "u", "database": "d"})
    assert r.status_code == 400, r.text
    assert not ddl, "服务端版本不够却已经对目标库跑了 DDL —— 库会停在半升级态"

    # **反面**：版本够的时候不许被这道新闸拦住（否则等于把切换整个焊死）。
    monkeypatch.setattr(mod.db_migrate, "test_connection", lambda *a, **k: (True, "8.0.36"))
    r2 = client.post("/api/db/switch",
                     json={"backend": "mysql", "host": "h", "user": "u", "database": "d"})
    assert r2.status_code != 400 or "版本" not in r2.json().get("detail", "")
    assert ddl, "版本够却没往下走到建表那一步"


def test_connections_that_cannot_be_decrypted_are_flagged_not_hidden(monkeypatch):
    """SECRET_KEY 变过之后，已保存连接的 DSN 就解不开了。

    列表**照列**（跳过等于「记录凭空消失」，用户连删都删不掉），但迁移/切换会拿到
    404「连接不存在或无法解密」——「明明列在这里」却说「不存在」，是一条读不懂的死路。
    所以每条带上 `decryptable`，界面据此标出来并禁掉那两个按钮。
    （`list_connections` 的 docstring 原先写着「解不开的跳过」，而它根本不解密。）
    """
    from app.config import settings
    from app.database import control_engine

    e = control_engine()
    cid = control.upsert_connection(e, backend="mysql",
                                    url="mysql+pymysql://u:p@h:3306/flagdb",
                                    host="h", port=3306, user="u", database="flagdb")
    got = {r["id"]: r for r in control.list_connections(e)}
    assert got[cid]["decryptable"] is True, "刚存进去就说解不开"

    monkeypatch.setattr(settings, "SECRET_KEY", "a-completely-different-secret-key-value")
    after = {r["id"]: r for r in control.list_connections(e)}
    assert cid in after, "解不开就把记录藏起来了——用户连删都删不掉"
    assert after[cid]["decryptable"] is False


def test_switch_runs_migrations_before_emptiness_probe():
    """顺序护栏：run_migrations 必须排在 is_target_empty 之前。
    反过来的话，缺列时 is_target_empty 会抛异常、被兜底成「尚未建表/迁移」，
    正好把用户引向那个会覆盖数据的按钮。"""
    import ast
    import inspect
    import textwrap

    from app.routers.dbadmin import switch

    tree = ast.parse(textwrap.dedent(inspect.getsource(switch)))
    # 只看真实调用，不看注释/文档串——注释里也会提到这两个名字
    calls = sorted(
        (n.lineno, getattr(n.func, "attr", None) or getattr(n.func, "id", None))
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    )
    names = [name for _, name in calls if name in ("run_migrations", "is_target_empty")]
    assert names == ["run_migrations", "is_target_empty"], \
        f"switch 里 run_migrations 必须在 is_target_empty 之前，实际顺序：{names}"


# --- 纯函数：连接串构造 / 库名白名单 -----------------------------------------

def test_build_mysql_url_escapes_credentials():
    url = db_migrate.build_mysql_url("h", 3306, "u@ser", "p@ss:w/ord", "db")
    assert "u%40ser" in url and "p%40ss%3Aw%2Ford" in url
    assert url.endswith("/db?charset=utf8mb4")


@pytest.mark.parametrize("bad", ["a b", "a;DROP", "a`b", "库名", "a-b", "", "a/b"])
def test_ensure_database_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        db_migrate.ensure_database("h", 3306, "u", "p", bad)


def test_ensure_database_accepts_good_name(monkeypatch):
    """合法库名应通过白名单（连接会失败，但不能是 ValueError）。"""
    with pytest.raises(Exception) as ei:
        db_migrate.ensure_database("127.0.0.1", 1, "u", "p", "soroban_ok")
    assert not isinstance(ei.value, ValueError)


# --- 控制库：DSN 加密 ---------------------------------------------------------

def test_dsn_roundtrip_encryption():
    plain = "mysql+pymysql://u:p%40ss@h:3306/db?charset=utf8mb4"
    assert control.decrypt(control.encrypt(plain)) == plain


def test_decrypt_garbage_returns_none():
    assert control.decrypt("not-a-fernet-token") is None
    assert control.decrypt("") is None


def test_decrypt_with_changed_secret_returns_none(monkeypatch):
    from app.config import settings
    token = control.encrypt("mysql+pymysql://u:p@h/db")
    monkeypatch.setattr(settings, "SECRET_KEY", "a-completely-different-secret-key-value")
    assert control.decrypt(token) is None


# --- 端点守卫 -----------------------------------------------------------------

def test_status_reports_sqlite(client):
    r = client.get("/api/db/status")
    assert r.status_code == 200
    assert r.json()["active"]["backend"] == "sqlite"


def test_migrate_to_current_db_rejected(client):
    r = client.post("/api/db/migrate", json={"backend": "sqlite"})
    assert r.status_code == 400 and "当前" in r.json()["detail"]


def test_switch_to_current_db_rejected(client):
    r = client.post("/api/db/switch", json={"backend": "sqlite"})
    assert r.status_code == 400


def test_mysql_target_missing_fields_rejected(client):
    r = client.post("/api/db/test", json={"backend": "mysql", "host": "h"})
    assert r.status_code == 400 and "参数不完整" in r.json()["detail"]


def test_unknown_connection_id_404(client):
    r = client.post("/api/db/test", json={"connection_id": 999999})
    assert r.status_code == 404


def test_sqlite_target_always_testable(client):
    r = client.post("/api/db/test", json={"backend": "sqlite"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_status_never_leaks_password(client):
    body = client.get("/api/db/status").text
    assert "password" not in body.lower()


# --- 方言助手 -----------------------------------------------------------------

def test_dialect_detection(session):
    from app.db.dialect import is_mysql, is_sqlite
    bind = session.get_bind()
    assert is_sqlite(bind) is True
    assert is_mysql(bind) is False


def test_upsert_and_insert_or_ignore_compile_on_sqlite(session):
    from app.db.dialect import insert_or_ignore, upsert
    from app.models import ColumnLayout
    bind = session.get_bind()
    stmt = insert_or_ignore(bind, ColumnLayout,
                            {"table_name": "orders", "columns_json": "[]"}, ["table_name"])
    assert "ON CONFLICT" in str(stmt.compile(bind))
    stmt2 = upsert(bind, ColumnLayout,
                   {"table_name": "orders", "columns_json": "[]"}, ["table_name"],
                   {"columns_json": "[]"})
    assert "ON CONFLICT" in str(stmt2.compile(bind))


def test_mysql_variants_compile():
    """MySQL 分支也要能编译（否则切到 MySQL 才在运行期炸）。"""
    from sqlalchemy.dialects import mysql

    from app.db.dialect import insert_or_ignore, upsert
    from app.models import ColumnLayout

    d = mysql.dialect()
    s1 = insert_or_ignore(d, ColumnLayout, {"table_name": "orders", "columns_json": "[]"},
                          ["table_name"])
    assert "IGNORE" in str(s1.compile(dialect=d))
    s2 = upsert(d, ColumnLayout, {"table_name": "orders", "columns_json": "[]"},
                ["table_name"], {"columns_json": "[]"})
    assert "ON DUPLICATE KEY UPDATE" in str(s2.compile(dialect=d))


def test_undecryptable_mysql_url_degrades_visibly(tmp_path, monkeypatch):
    """SECRET_KEY 变了 → MySQL 连接串解不开 → 退回本地 SQLite。**这件事必须可见。**

    用户看到的现象是打开应用「账本全空了」，而数据好端端在 MySQL 里。
    最容易踩到的路径正是升级：`Releases\\<VERSION>` 换了目录、exe 搬过去了、
    `.env` 忘了搬 → SECRET_KEY 变 → Fernet 解不开 → 静默退回空的本地库。
    原先这条路上只有一行 `log.error`，而分发版的用户不会去看日志。
    """
    from sqlmodel import create_engine

    from app.config import settings
    from app.db import control

    eng = create_engine(f"sqlite:///{tmp_path}/ctl.db")
    control.ensure_schema(eng)
    control.write_config(eng, "mysql", "mysql+pymysql://u:pw@10.9.9.9:3306/ledger")

    good = control.read_config(eng)
    assert good["backend"] == "mysql" and good["degraded"] == ""

    monkeypatch.setattr(settings, "SECRET_KEY", "another-key-" + "x" * 40)
    bad = control.read_config(eng)
    assert bad["backend"] == "sqlite", "解不开就该退回本地库（不能让应用起不来）"
    assert bad["mysql_url"] is None
    assert bad["degraded"], "降级了却一声不吭——用户只会看到「账本全空了」"
    assert "SECRET_KEY" in bad["degraded"] and "没丢" in bad["degraded"]


@pytest.mark.parametrize("version,rejected", [
    ("8.0.36", False),
    ("8.4.0", False),
    ("9.7.0", False),
    ("5.7.44", True),
    ("5.6.51", True),
    ("10.11.6-MariaDB-1:10.11.6+maria~ubu2204", True),
    ("11.4.2-MariaDB", True),
    ("", False),                 # 认不出就放行：不该因为格式没见过而挡住人
    ("weird-build", False),
])
def test_unsupported_server_is_rejected_at_the_door(version, rejected):
    """版本不够要**在门口拒绝**，不能跑到迁移中途才炸。

    MySQL 的 DDL 是隐式提交的：迁移链跑到一半失败时，前面几条已经落地、后面的没跑，
    库停在一个既不是旧版也不是新版的半升级态，而用户看到的只有一句驱动层英文报错。

    README 曾承诺「MariaDB 会自动回退」——回退逻辑确实在 `bin_collation()` 里，
    但建表走的 `BinStr` 硬写了 `utf8mb4_0900_bin`，绕过了它。承诺是假的。
    """
    from app.services.db_migrate import unsupported_server

    why = unsupported_server(version)
    assert bool(why) is rejected, f"{version!r} → {why!r}"
    if rejected:
        assert "8.0" in why, "拒绝了却没说清要什么版本，用户不知道下一步做什么"


def test_readme_does_not_promise_mariadb():
    """README 里那句「MariaDB 会自动回退」是假的，不能再出现。

    文档里的假承诺比代码 bug 更贵：它会让人**照着做**，然后掉进一个半升级的库。
    """
    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
    assert "MariaDB 会自动回退" not in readme
    assert "MariaDB 不支持" in readme, "得明说不支持，光删掉旧句子等于没说"


def test_switching_holds_the_read_only_barrier_over_the_critical_section(
        client, tmp_path, monkeypatch):
    """切换的**临界段**必须挂只读屏障：指纹检查 → 引擎交换之间不许有写入落进旧库。

    落在那个窗口里的写会提交进**旧库**，切换之后既不在新库里、也没有任何提示——
    一张已经 200 的订单凭空消失。窗口只有几毫秒，但后果是静默丢账；
    而 `migrate()` 本来就挂着屏障，两个同级操作没有理由一个挂一个不挂。

    `run_migrations(目标)` 刻意**不**在屏障里（它写的是目标库，且可能要跑几十秒），
    所以这条只断言临界段。判据是**交换引擎的那一刻屏障举着没有**。
    """
    import app.routers.dbadmin as mod
    from app.database import build_engine, get_engine, run_migrations, set_data_engine
    from app.maintenance import barrier
    from app.services import db_migrate

    # 造一个已经有数据的目标库（switch 要求目标非空）
    url = f"sqlite:///{tmp_path / 'switch-target.db'}"
    run_migrations(url)
    tgt = build_engine(url)
    db_migrate.replace_data(get_engine(), tgt)

    seen = []
    real_swap = mod.set_data_engine

    def _spy(engine, u):
        seen.append(barrier.blocked_reason())      # 交换的那一刻，屏障举着吗
        return real_swap(engine, u)

    monkeypatch.setattr(mod, "_resolve_target", lambda t: ("sqlite", url, tgt, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, u: False)
    monkeypatch.setattr(mod, "set_data_engine", _spy)

    origin = get_engine()
    try:
        # `confirm_changed`：本文件别的用例会留下 migrate_state 指纹，那道闸不是这条要测的
        r = client.post("/api/db/switch", json={"backend": "sqlite", "confirm_changed": True})
        assert r.status_code == 200, r.text
    finally:
        set_data_engine(origin, str(origin.url))   # 切回去，别影响后面的用例
        tgt.dispose()

    assert seen, "根本没走到 set_data_engine"
    assert seen[-1], "交换引擎时**没有**举着只读屏障——那几毫秒里的写会静默落进旧库"


def test_the_non_empty_409_discloses_that_the_schema_was_already_upgraded(
        client, dst_engine, monkeypatch):
    """「目标非空」那句 409 必须说清：**表结构已经升过了，只是数据没动。**

    顺序是被迫的——`target_rows` 要 select 业务表，表还没建就数不出行数，
    所以 `run_migrations(目标)` 只能排在前面（反过来排会把「尚未建表」误报成
    「目标是空的」，把用户推向覆盖）。改顺序会更糟，所以这条不改顺序。

    但用户看到 409 点了取消，会以为**什么都没发生**。而目标库的 `alembic_version`
    已经前进了：另一台还在用旧版 soroban 的机器连同一个库时，下次启动就会撞上
    「库比代码新」。数据没动是真的，schema 动了也是真的，**两句都要说**。
    """
    from app.database import get_engine
    from app.routers import dbadmin as mod

    client.post("/api/orders", json={"date": "2027-04-02", "title": "让对面非空"})
    db_migrate.replace_data(get_engine(), dst_engine)        # 目标库变成非空
    monkeypatch.setattr(mod, "_resolve_target",
                        lambda t: ("sqlite", "sqlite:///dst", dst_engine, False))
    monkeypatch.setattr(mod, "_is_same_as_active", lambda backend, url: False)

    r = client.post("/api/db/migrate", json={"backend": "sqlite"})
    assert r.status_code == 409, f"没走到「目标非空」那道闸：{r.status_code} {r.text[:200]}"
    detail = r.json()["detail"]
    assert "无法撤销" in detail, detail
    assert "表结构" in detail and "数据一个字都没动" in detail, \
        f"没说清「schema 已经升了、数据没动」：{detail}"


def test_preflight_catches_text_columns_that_overflow_mysql(tmp_path):
    """`preflight` 必须捞出**超长的 TEXT**（note / title / url / params_json）。

    这一支原先整个不存在：`if limit:` 对 TEXT 是 False（它的 `length` 是 None），
    后面两支也不认它。而 SQLite 照单全收任意长度、MySQL 的 TEXT 在 STRICT_TRANS_TABLES 下
    抛 1406——`replace_data` 是**单事务**，一行超长就让整次迁移回滚，
    用户只看到「拷贝「商品订单」表时失败：<SQLAlchemy 原始串>」，
    既不知道是哪一行、也不知道该怎么办。preflight 的职责恰恰是提前把这种行捞出来。

    **上限数的是字节不是字符**：MySQL 的 TEXT = 65535 字节，一个汉字 3 字节，
    所以 21845 个汉字就顶满了。按字符判会把合法的中文误放过去。

    目标引擎**不需要真的连上**——`preflight` 只读它的方言名，查询全打在源库上。
    """
    import datetime as dt

    from sqlmodel import Session

    from app.models import Order

    url = f"sqlite:///{tmp_path / 'src.db'}"
    run_migrations(url)
    src = build_engine(url)
    try:
        with Session(src) as s:
            long_note = Order(date=dt.date(2027, 1, 1), title="超长备注", order_no="LONG-1",
                              purchase_status="待收货", note="备" * 30000)   # 90000 字节
            long_note.compute_money()
            ok = Order(date=dt.date(2027, 1, 1), title="正常", order_no="OK-1",
                       purchase_status="待收货", note="备" * 100)            # 300 字节
            ok.compute_money()
            s.add(long_note)
            s.add(ok)
            s.commit()

        mysql_dst = build_engine("mysql+pymysql://u:p@127.0.0.1:3306/nope?charset=utf8mb4")
        try:
            problems = db_migrate.preflight(src, mysql_dst)
        finally:
            mysql_dst.dispose()
    finally:
        src.dispose()

    hits = [p for p in problems if "note" in p]
    assert hits, f"超长 TEXT 没被捞出来：{problems}"
    assert "90000" in hits[0], f"报的不是字节数（一个汉字 3 字节）：{hits[0]}"
    assert len(hits) == 1, f"把正常长度的那条也报了：{hits}"
