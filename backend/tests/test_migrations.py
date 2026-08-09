"""迁移链本身的测试：从零建库、从中途升级、旧库接管，以及有数据时的数据迁移正确性。

这些跑在**各自独立的临时库**上（不用 conftest 那个共享库），因为要控制起始 revision。
"""
import datetime as dt

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.database import _ROOT, build_engine, run_migrations


def _cfg(url: str) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture()
def fresh_url(tmp_path):
    return f"sqlite:///{tmp_path / 'mig.db'}"


def test_single_head():
    """多个 head 会让 `alembic upgrade head` 直接报错、应用起不来。"""
    heads = ScriptDirectory.from_config(_cfg("sqlite://")).get_heads()
    assert len(heads) == 1, f"迁移链分叉了：{heads}"


def test_revision_chain_is_linear():
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    revs = list(script.walk_revisions())
    for r in revs:
        assert not isinstance(r.down_revision, tuple), f"{r.revision} 是合并点"
    assert len({r.revision for r in revs}) == len(revs), "有重复 revision id"


def test_upgrade_from_scratch_creates_all_tables(fresh_url):
    run_migrations(fresh_url)
    e = build_engine(fresh_url)
    try:
        names = set(inspect(e).get_table_names())
    finally:
        e.dispose()
    from sqlmodel import SQLModel
    assert set(SQLModel.metadata.tables) <= names


def test_upgrade_is_idempotent(fresh_url):
    run_migrations(fresh_url)
    run_migrations(fresh_url)      # 再跑一次不该炸
    e = build_engine(fresh_url)
    try:
        with e.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar() == 1
    finally:
        e.dispose()


def test_downgrade_then_upgrade_roundtrip(fresh_url):
    """每个迁移的 downgrade 都要能跑通——不然出事时没法回退。"""
    run_migrations(fresh_url)
    cfg = _cfg(fresh_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    e = build_engine(fresh_url)
    try:
        names = set(inspect(e).get_table_names())
    finally:
        e.dispose()
    assert "orders" in names


# --- d0e1f2a3b4c5：单号归一 + 标题放宽 ---------------------------------------

def test_migration_normalizes_existing_codes(fresh_url):
    """升级到该版本前写入的脏单号（小写/带空格/空串），升级后必须被归一。"""
    cfg = _cfg(fresh_url)
    command.upgrade(cfg, "c9d0e1f2a3b4")          # 停在上一版
    e = build_engine(fresh_url)
    try:
        with e.begin() as c:
            c.execute(text(
                "INSERT INTO orders (date, status, source, version, is_delete, created_at,"
                " updated_at, express_no, order_no)"
                " VALUES (:d,'待发货','manual',1,0,:t,:t,'  sf123456789  ','A1')"),
                {"d": dt.date(2028, 1, 1), "t": dt.datetime(2028, 1, 1)})
            c.execute(text(
                "INSERT INTO orders (date, status, source, version, is_delete, created_at,"
                " updated_at, express_no, order_no)"
                " VALUES (:d,'待发货','manual',1,0,:t,:t,'','A2')"),
                {"d": dt.date(2028, 1, 1), "t": dt.datetime(2028, 1, 1)})
            c.execute(text(
                "INSERT INTO shipmentorder (date, status, source, version, is_delete,"
                " created_at, updated_at, intl_tracking_no)"
                " VALUES (:d,'打包中','manual',1,0,:t,:t,' eb99887766cn ')"),
                {"d": dt.date(2028, 1, 1), "t": dt.datetime(2028, 1, 1)})

        command.upgrade(cfg, "head")              # 跑本次新增的迁移

        with e.connect() as c:
            rows = dict(c.execute(text("SELECT order_no, express_no FROM orders")).all())
            assert rows["A1"] == "SF123456789", "小写+空格未被归一"
            assert rows["A2"] is None, "空串未归成 NULL"
            intl = c.execute(text("SELECT intl_tracking_no FROM shipmentorder")).scalar()
            assert intl == "EB99887766CN"
    finally:
        e.dispose()


def test_migration_preserves_long_titles(fresh_url):
    """标题列放宽为 TEXT + 随后改名 shop→title：既有长标题一路原样保留，不被截断也不丢。

    注意插入语句用的是**当时那一版**的列名（shop）——这正是「从旧库升上来」要验的场景。"""
    cfg = _cfg(fresh_url)
    command.upgrade(cfg, "c9d0e1f2a3b4")
    title = "很长的商品标题 / " * 50
    e = build_engine(fresh_url)
    try:
        with e.begin() as c:
            c.execute(text(
                "INSERT INTO orders (date, status, source, version, is_delete, created_at,"
                " updated_at, shop) VALUES (:d,'待发货','manual',1,0,:t,:t,:s)"),
                {"d": dt.date(2028, 1, 1), "t": dt.datetime(2028, 1, 1), "s": title})
        command.upgrade(cfg, "head")
        with e.connect() as c:
            # 升到 head 后列已改名 shop→title（迁移 e1f2a3b4c5d6）
            assert c.execute(text("SELECT title FROM orders")).scalar() == title
    finally:
        e.dispose()


def test_pre_alembic_db_is_adopted(tmp_path):
    """Alembic 之前建的旧库（有完整 baseline 业务表、但没有 alembic_version）应被 stamp 到
    baseline 再往上升，而不是当成全新库从头建（那会撞已存在的表）。

    造法：先正常升到 baseline，再把 alembic_version 删掉——这正是 `create_all` 时代那种库的样子。

    ⚠️ 注意这条启发式的前提是「有业务表 ⇒ 有**完整的** baseline schema」。若某个库只建了一部分表
    （例如首次启动建到一半崩了），照样会被 stamp 到 baseline，随后的 ALTER 会因表不存在而失败。
    见审计报告「四、优化方向」。"""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    cfg = _cfg(url)
    command.upgrade(cfg, "53b7e33debd0")           # 建出 pre-Alembic 时代的完整 schema
    e = build_engine(url)
    try:
        with e.begin() as c:
            c.execute(text("DROP TABLE alembic_version"))   # 抹掉版本记录 = 旧库的样子
        run_migrations(url)                        # 应自动 stamp 到 baseline 再升到 head
        with e.connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM alembic_version")).scalar() == 1
            assert "orders" in set(inspect(e).get_table_names())   # 已跑完改名迁移
    finally:
        e.dispose()


def test_control_tables_do_not_trigger_legacy_adoption(tmp_path):
    """全新业务库 + 已存在控制表（app_db_config）不能被误判成旧库——否则会 stamp 到 baseline
    而不建业务表，全新部署直接起不来。"""
    from app.db import control
    url = f"sqlite:///{tmp_path / 'ctrl.db'}"
    e = build_engine(url)
    try:
        control.ensure_schema(e)                   # 只建控制表
        run_migrations(url)
        assert "orders" in set(inspect(e).get_table_names())
    finally:
        e.dispose()


def test_every_create_table_pins_engine_and_charset():
    """每个 `op.create_table` 都必须带 `mysql_engine` + `mysql_charset`。

    现有迁移测试全跑在 SQLite 上，这类**MySQL-only 的缺失一条都拦不住**——
    `b0c1d2e3f4a5` 就是全链 14 处里唯一漏掉的，本地怎么跑都绿。
    这条是纯静态的，不需要连 MySQL 也能守住。
    """
    import ast
    from pathlib import Path

    bad = []
    for f in sorted((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create_table"):
                continue
            kw = {k.arg for k in node.keywords if k.arg}
            starred = any(k.arg is None for k in node.keywords)   # **_MYSQL 这种展开
            if starred:
                continue
            missing = {"mysql_engine", "mysql_charset"} - kw
            if missing:
                bad.append(f"{f.name}:{node.lineno} 缺 {sorted(missing)}")
    assert not bad, ("这些建表没钉死引擎/字符集，表的默认排序规则会跟着目标库走：\n  "
                     + "\n  ".join(bad))
