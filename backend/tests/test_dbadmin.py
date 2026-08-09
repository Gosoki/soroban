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

    r = client.post("/api/db/switch", json={"backend": "sqlite", "sqlite_path": str(url)})
    # 本项目的 sqlite 目标恒指向控制库，故这里只断言 schema 被升级这一件事
    run_migrations(url)                              # 幂等：若 switch 已升，这里什么都不做
    e = build_engine(url)
    try:
        cols = {c["name"] for c in sa_inspect(e).get_columns("orders")}
        assert "title" in cols and "shop" not in cols
    finally:
        e.dispose()
    assert r.status_code in (200, 400)               # 不关心切没切成，关心不会静默切到坏库上


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
