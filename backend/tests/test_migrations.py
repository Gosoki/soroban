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
        db.migrate_to_latest()

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
        db.migrate_to_latest()

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


def test_running_a_migration_does_not_silence_caplog(tmp_path, caplog):
    """用例内跑过迁移之后，`caplog` 仍然抓得到日志。

    `alembic/env.py` 的 `fileConfig()` 会按 alembic.ini 的 `[handlers]` **重建 root 的
    handler 列表**（`disable_existing_loggers=False` 只保住 logger 不被禁用，管不到
    handler 被换掉）。pytest 装在 root 上的 caplog handler 于是被掀掉，
    `caplog.records` **恒为空**——而日志照常打印在终端上，所以断言失败会被读成
    「这条日志根本没打」。2026-08-22 写备份守卫时当场撞上，排查了半天。

    这条把它钉住：**先跑一次真迁移，再打一条日志，caplog 必须看得见。**
    判据不能是「env.py 里有没有那个 if」——那是源码 grep，换个写法就失效。
    """
    import logging

    from app.database import run_migrations

    run_migrations(f"sqlite:///{tmp_path / 'x.db'}")     # 真的跑一次 alembic

    with caplog.at_level(logging.WARNING):
        logging.getLogger("soroban.db").warning("迁移之后这条要抓得到")

    assert any("迁移之后这条要抓得到" in r.getMessage() for r in caplog.records), (
        "跑过迁移之后 caplog 什么都抓不到——alembic 的 fileConfig 把 root 的 handler 掀了。"
        f"当前记录：{[r.getMessage()[:40] for r in caplog.records]}")


def test_offline_sql_mode_fails_with_a_readable_message_not_a_traceback():
    """`alembic upgrade --sql` 撞上「要读数据」的迁移时，给一句话而不是 traceback。

    离线模式下 `op.get_bind()` 是 `None`，任何 `conn.execute(...)` 都以
    `AttributeError: 'NoneType' object has no attribute 'fetchall'` 收场——
    而且是在**前十条 revision 的 DDL 已经打印出来之后**才炸。
    那份半截输出看起来完全正常，照着执行会漏掉后面所有步骤。

    `--sql` 本身也确实产不出这类步骤的等价 SQL（它们要先读现有数据、按内容决定写什么），
    所以这不是「暂未支持」，是原理上做不到——该说的就是这句话。

    这条真跑 `alembic upgrade head --sql`，不是去 grep 源码里有没有那个调用。
    """
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
                       cwd=backend, capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode != 0, "离线模式居然成功了？那说明数据迁移步骤被静默跳过了"
    assert "无法在 `--sql` 离线模式下生成等价 SQL" in out, \
        f"给的不是那句人话：\n{out[-1200:]}"
    assert "AttributeError" not in out, f"还是抛了 traceback：\n{out[-1200:]}"


def test_a_downgrade_that_cannot_work_refuses_before_touching_the_schema(tmp_path):
    """降级撞上「本条迁移刚放开的数据」时，**在门口拒绝**，不许跑到一半才炸。

    `c2d3e4f5a6b7` 的 upgrade 让「不同来源下可以同号」（闲鱼/淘宝各一条）。
    降级要装回 `order_no` 单列唯一——而那正是升级之后**合法存在**的数据所违反的。

    不先查的话：`drop_active_unique` 已经把索引和生成列删掉了，随后建唯一索引时才撞车。
    **MySQL 的 DDL 是隐式提交的**，库会停在「新索引没了、旧索引也没建上」的半降级态——
    此后连唯一性都没人守了，而用户只拿到一句原始报错。

    判据有两半：**拒绝**，且**没动过任何东西**（旧索引仍在、数据仍在）。
    只断言「抛了异常」是不够的——跑到一半再抛同样是抛。
    """
    import datetime as dt

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text

    import app.database as db

    url = f"sqlite:///{tmp_path / 'down.db'}"
    cfg = Config(str(db._ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(db._ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "c2d3e4f5a6b7")

    eng = db.build_engine(url)
    try:
        with eng.connect() as c:
            # 造出本条迁移刚刚放开的那种数据：同号、不同来源
            for plat in ("淘宝", "闲鱼"):
                # 列名按**那个 revision 当时**的样子写（`source` 还没改名成 `created_via`，
                # `status` 还没改成 `purchase_status`）——迁移测试必须用历史 schema，
                # 不能照抄今天的模型。
                c.execute(text(
                    "INSERT INTO taobaoorder (date, order_no, platform, status, source,"
                    " created_at, updated_at, version) VALUES (:d, 'SAME-1', :p, '待收货', '手填',"
                    " :t, :t, 1)"),
                    {"d": "2026-05-01", "p": plat, "t": dt.datetime.now(dt.timezone.utc)})
            c.commit()
            before = {i["name"] for i in inspect(c).get_indexes("taobaoorder")}

        import pytest

        with pytest.raises(RuntimeError) as e:
            command.downgrade(cfg, "b1f2a3c4d5e6")
        assert "不同来源的同号订单" in str(e.value), str(e.value)
        assert "SAME-1" in str(e.value), f"没说出是哪个号，用户无从下手：{e.value}"

        with eng.connect() as c:
            after = {i["name"] for i in inspect(c).get_indexes("taobaoorder")}
            rows = c.execute(text("SELECT COUNT(*) FROM taobaoorder")).scalar()
        assert after == before, f"拒绝之前已经动过索引了：{before} → {after}"
        assert rows == 2, "数据被动过了"
    finally:
        eng.dispose()


def test_the_collation_downgrade_checks_before_it_drops_anything():
    """`f2a3b4c5d6e7` 的降级预检必须排在**第一次 drop 之前**。

    上一条（MySQL 契约层）验的是「判据查得出冲突」，这一条验的是**位置**——
    查得再准，排在 drop 后面也没用。两者缺一，另一半就能悄悄退化。

    为什么位置是要害：降级的第一步就把三处「活跃行唯一」连同 MySQL 生成列一起
    DROP 掉，而 **MySQL 的 DDL 是隐式提交的**；`env.py` 又开了
    `transaction_per_migration`，所以随后那步撞 1062 时，约束已经没了、
    版本号却回滚到原地。用户只看到一句原始报错，此后一切**看起来完全正常**
    （`upgrade head` 从这里往后照跑），而没有任何后续迁移会把那三条约束建回来。

    同一条规矩 `c2d3e4f5a6b7` 早就立过（「先查数据，再动 schema」），这条当时没跟上。
    """
    import ast
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "f2a3b4c5d6e7_binary_collation_for_key_columns.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "downgrade")

    def first_line(pred) -> int | None:
        hits = [n.lineno for n in ast.walk(fn) if pred(n)]
        return min(hits) if hits else None

    check = first_line(lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_conflicts_after_downgrade")
    drop = first_line(lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                      and n.func.id == "drop_active_unique")
    assert check is not None, "降级完全没有预检——撞 1062 时三处唯一约束已经删掉了"
    assert drop is not None, "找不到 drop_active_unique，这条守卫的前提变了"
    assert check < drop, (
        f"预检（第 {check} 行）排在了 drop_active_unique（第 {drop} 行）后面。"
        "MySQL 的 DDL 隐式提交，drop 之后再拦已经晚了——约束此后永远建不回来")


def test_no_downgrade_drops_an_index_on_a_table_it_also_drops():
    """`downgrade()` 里不许对**自己随后就要 drop 掉的表**再单独 drop 索引。

    那种写法是 alembic 自动生成的默认形状，在 SQLite 上只是冗余
    （`DROP TABLE` 本来就带走它全部的索引和生成列），**在 MySQL 上会把整条降级链卡死**：

        (1553, "Cannot drop index 'ix_stagingitem_staging_id':
                needed in a foreign key constraint")

    InnoDB 要求外键列上有一根以它打头的索引，全表只有那一根时就删不掉。
    而 **MySQL 的 DDL 是隐式提交的** —— 降级链在半路倒下时，前面每一条都已经落地
    且不可回滚：`pluginrecord` 整张表被 DROP、`pluginconfig` 五列被删、
    `d2e3f4a5b6c7` 的降级还 `DELETE FROM fxrate`。
    库停在一个既不是旧版也不是新版的半降级态，而这正是 README 写着
    「全部迁移在真 MySQL 上跑通 upgrade→downgrade→upgrade」的那条路。

    2026-09-01 实测：修掉 `a9b0c1d2e3f4` 那条之后 27 条里通了 26 条，
    最后一条（baseline → base）倒在同款错误上；删掉那 24 条冗余语句之后全通。

    判据走 AST，不按字符串——这条测试自己的说明里就写着 `drop_index`。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    files = sorted(root.glob("*.py"))
    assert len(files) >= 20, f"只扫到 {len(files)} 个迁移，探测方式可能已过期"

    offenders = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "downgrade"), None)
        if fn is None:
            continue
        dropped_tables, index_drops = set(), []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", ""))
            if name == "drop_table" and node.args and isinstance(node.args[0], ast.Constant):
                dropped_tables.add(node.args[0].value)
            elif name in ("drop_index", "drop_active_unique"):
                for kw in node.keywords:
                    if kw.arg in ("table_name", "table") and isinstance(kw.value, ast.Constant):
                        index_drops.append((kw.value.value, node.lineno))
        for tbl, line in index_drops:
            if tbl in dropped_tables:
                offenders.append(f"{f.name}:{line}  表 {tbl} 随后就被 drop_table 了")

    assert not offenders, (
        "这些 downgrade 在 drop 掉整张表之前还单独 drop 了它的索引：\n  "
        + "\n  ".join(offenders)
        + "\n在 SQLite 上只是冗余，在 MySQL 上会撞 1553 把整条降级链卡在半路（DDL 已隐式提交）。"
          "\n直接删掉那些 drop_index —— DROP TABLE 本来就带走它们。")

