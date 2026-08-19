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


def test_a_newer_database_gets_its_own_message(caplog, monkeypatch):
    """库里的版本比代码新时，要给一条**能照做**的中文指引，不是一行英文 CommandError。

    这条路径落在**分发版唯一的形态**（SQLite）上：用户装过新版（库被 upgrade 到新
    revision），又换回旧版 exe。原先只有 `current_backend() == "mysql"` 分支有中文指引，
    SQLite 走的是裸 `raise` → `alembic.util.exc.CommandError: Can't locate revision
    identified by 'xxx'`。用户既不知道数据有没有事（其实完好无损），
    也不知道该往前装还是往后退。

    **按异常类型 + revision 特征判，不按整句文案**：alembic 的措辞会随版本变，
    而错认成「连不上数据库」会给出南辕北辙的指引（去查 MySQL 有没有起）。
    """
    import logging

    from alembic.util.exc import CommandError

    from app import database as db

    def boom(url):
        raise CommandError("Can't locate revision identified by 'deadbeef'")

    monkeypatch.setattr(db, "run_migrations", boom)
    monkeypatch.setattr(db.control, "ensure_schema", lambda *a, **k: None)
    with caplog.at_level(logging.ERROR), pytest.raises(CommandError):
        db.create_db_and_tables()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "更新版本的 soroban" in text, f"没认出「库比代码新」：{text[:300]}"
    assert "你的数据没有问题" in text, "没告诉用户数据是好的——那是他此刻最想知道的"
    assert "备份" in text, "给了删库的建议却没强调先备份"
    assert "MySQL" not in text, "错认成了连不上数据库，给出南辕北辙的指引"


@pytest.mark.parametrize("exc", [
    OSError("Connection refused"),                       # 连不上
    __import__("alembic.util.exc", fromlist=["x"]).CommandError("Target database is not up to date."),
])
def test_other_failures_do_not_claim_a_newer_database(exc):
    """反面：别的迁移失败不许被认成「库比代码新」。

    没有这一条，把判据写成「只要是 CommandError 就算」也能让上面那条绿——
    而那会在真正连不上数据库时告诉用户「你的数据没问题，去装新版」。
    """
    from app.database import _looks_like_newer_db

    assert not _looks_like_newer_db(exc), f"{type(exc).__name__} 被误认成了新库"


def test_mysql_users_are_not_told_to_delete_the_control_db(monkeypatch, caplog):
    """MySQL 后端遇到「库比代码新」时，**不许**建议删 soroban.db。

    「库比代码新」和「后端是什么」是两个正交的维度，原先却串成一条链
    （先认 newer，认出来就走 SQLite 文案）。而 MySQL 上这个报错很常见——
    另一台机器上的新版 soroban 连的是同一个库。

    照那条建议做的后果特别恶劣：soroban.db 是**控制库**，里面只有 Fernet 加密的
    MySQL 连接串，业务数据一行都不在其中。删了它 = 业务数据毫发无伤，
    但再也连不回那个 MySQL，而用户以为自己是在「重建账本」。
    """
    import logging

    import pytest
    from alembic.util.exc import CommandError

    from app import database as db

    def boom(url):
        raise CommandError("Can't locate revision identified by 'deadbeef'")

    monkeypatch.setattr(db, "run_migrations", boom)
    monkeypatch.setattr(db.control, "ensure_schema", lambda *a, **k: None)
    monkeypatch.setattr(db, "current_backend", lambda: "mysql")
    with caplog.at_level(logging.ERROR), pytest.raises(CommandError):
        db.create_db_and_tables()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "更新版本的 soroban" in text, f"没认出「库比代码新」：{text[:300]}"
    assert "你的数据没有问题" in text, "没告诉用户数据是好的——那是他此刻最想知道的"
    # 判据：凡是同时提到「删」和 soroban.db 的行，必须是**劝阻**而不是指示。
    # 钉语义不钉措辞——第一版把排除写成 `"不要删" not in ln`，
    # 结果被自己那句「**不要**删本地的 soroban.db」误伤（中间隔着 markdown 星号）。
    negations = ("不要", "别", "无需", "不用", "并不")
    offenders = [ln for ln in text.splitlines()
                 if "删" in ln and "soroban.db" in ln and not any(n in ln for n in negations)]
    assert not offenders, \
        f"给 MySQL 用户出了删控制库的主意（业务数据不在里面，删了只会连不回去）：\n" + "\n".join(offenders)
    assert "use-local-db" in text, "没给出「暂时改用本地账本」这条真正能照做的出路"


def test_the_only_drop_table_in_the_chain_refuses_to_eat_data(fresh_url):
    """`b0c1d2e3f4a5` 里那条 `drop_table` 是全链 25 个 `upgrade()` 中**唯一**的一条
    （其余 12 处全在 `downgrade()` 里）。它要收拾的是「MySQL 上一次失败的迁移留下的空壳」
    ——那条路径上表必然是空的，因为建完就炸、没有任何写入者跑过。

    但代码原先不区分「空壳残留」和「有数据」：只要库里有一张同名表而 `alembic_version`
    还没走到这里，它就会连同全部插件私有数据一起删掉，**没有日志、没有备份、没有计数**。
    （`database.py` 的 pre-Alembic 收养逻辑——丢了 `alembic_version` 就 stamp 回 baseline
    重跑全链——理论上能走到这里。）
    """
    e = build_engine(fresh_url)
    try:
        command.upgrade(_cfg(fresh_url), "a9b0c1d2e3f4")     # 停在这条迁移的前一版
        with e.begin() as c:
            # 手工造一张「有数据的同名表」——就是那种绝不该被静默删掉的情形
            c.execute(text("CREATE TABLE pluginrecord (id INTEGER PRIMARY KEY, k TEXT)"))
            c.execute(text("INSERT INTO pluginrecord (id, k) VALUES (1, '轨迹去重状态')"))

        with pytest.raises(Exception) as ei:                  # alembic 会把它包一层
            command.upgrade(_cfg(fresh_url), "b0c1d2e3f4a5")
        assert "pluginrecord" in str(ei.value), str(ei.value)

        # 数据必须原封不动
        with e.begin() as c:
            assert c.execute(text("SELECT COUNT(*) FROM pluginrecord")).scalar() == 1
    finally:
        e.dispose()


def test_the_rebuild_still_clears_an_empty_leftover(fresh_url):
    """**反面**：空壳照常清掉，否则这条重建就等于被关掉了——
    而它存在的理由正是「修好问题再跑不该撞 Table already exists」。
    """
    e = build_engine(fresh_url)
    try:
        command.upgrade(_cfg(fresh_url), "a9b0c1d2e3f4")
        with e.begin() as c:
            c.execute(text("CREATE TABLE pluginrecord (id INTEGER PRIMARY KEY, k TEXT)"))
        command.upgrade(_cfg(fresh_url), "b0c1d2e3f4a5")      # 不该抛
        cols = {c["name"] for c in inspect(e).get_columns("pluginrecord")}
        assert {"plugin_id", "kind", "key", "data"} <= cols, f"空壳没被换成真表：{cols}"
    finally:
        e.dispose()
