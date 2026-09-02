"""双引擎契约测试：同一段业务操作，在 SQLite 与 MySQL 上必须给出**相同的可观测结果**。

为什么必须有这一层：本项目的业务库可以是 SQLite 也可以是 MySQL，还能运行期热切换，
但既有的几百条测试**全部跑在 SQLite 上**——「SQLite 全绿、切到 MySQL 才炸」的整类 bug
天然看不见。本轮审计确认的发散（排序规则 _ci vs BINARY、DATETIME 精度、DECIMAL 范围、
INSERT IGNORE 吞截断）没有一条能被纯 SQLite 的测试发现。

**默认自动跳过**，只有给了真 MySQL 才跑：

    SOROBAN_TEST_MYSQL_URL='mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4' \\
        .venv/bin/python -m pytest tests/test_mysql_contract.py

⚠️ 这些测试会**清空**目标库的业务表，只能指向专用测试库。
"""
from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlmodel import Session, delete, select

from app.database import build_engine, control_engine, control_url, get_engine, run_migrations, set_data_engine
from app.db import dialect
from app.models import FxRate, Order, ShipmentOrder, TagOption, User
from app.services import db_migrate

MYSQL_URL = os.getenv("SOROBAN_TEST_MYSQL_URL")

pytestmark = pytest.mark.skipif(
    not MYSQL_URL,
    reason="需要真 MySQL：设 SOROBAN_TEST_MYSQL_URL 后才跑（会清空目标库业务表）",
)


def _wipe(engine) -> None:
    with Session(engine) as s:
        for model in reversed(db_migrate.MIGRATION_ORDER):
            s.exec(delete(model))
        s.commit()


@pytest.fixture()
def mysql_engine():
    run_migrations(MYSQL_URL)                       # 幂等：顺带验证全部迁移能在真 MySQL 上跑通
    eng = build_engine(MYSQL_URL)
    _wipe(eng)
    yield eng
    _wipe(eng)
    eng.dispose()


@pytest.fixture()
def on_mysql(mysql_engine, client):
    """把**整个应用**的数据引擎临时切到 MySQL，然后照常打 HTTP 端点。

    这才是真正的契约测试：不是单独验几条 SQL，而是让同一份路由代码、同一个请求，
    在另一个引擎上跑一遍，比对可观测结果。get_session 每次都读全局数据引擎，
    所以换掉它就够了（热切换本来也是这么工作的）。"""
    # 鉴权走 session.get(User, sub)，MySQL 库里得有**同 id** 的用户，否则全部 401
    with Session(get_engine()) as src, Session(mysql_engine) as dst:
        for u in src.exec(select(User)).all():
            dst.add(User(**{c: getattr(u, c) for c in User.__table__.columns.keys()}))
        dst.commit()

    set_data_engine(mysql_engine, MYSQL_URL)
    try:
        yield client
    finally:
        # 测试库恒为 SQLite 模式，数据引擎就是控制引擎；直接复位即可
        set_data_engine(control_engine(), control_url())


# --- 排序规则：MySQL 侧必须与 SQLite 一样逐字节 -----------------------------------

@pytest.mark.parametrize("variant", ["alice", "ALICE", "ヤマダ", "ＡＬＩＣＥ", "Alicé"])
def test_case_and_accent_variants_are_distinct_tags(on_mysql, variant):
    """MySQL 默认的 utf8mb4_0900_ai_ci 会把这些全判成同一个值（实测连半角浊音假名都折叠）。
    迁移 f2a3b4c5d6e7 把键列改成 utf8mb4_0900_bin 之后，必须与 SQLite 一样各算各的。"""
    assert on_mysql.post("/api/tags/recipient", json={"value": "Alice"}).status_code == 200
    r = on_mysql.post("/api/tags/recipient", json={"value": variant})
    assert r.status_code == 200
    values = {t["value"] for t in r.json()}
    assert {"Alice", variant} <= values, f"{variant!r} 被 MySQL 当成 'Alice' 折叠掉了"


def test_tag_rename_does_not_touch_case_variants(on_mysql):
    """按值批量改名用 `WHERE col = value`。ci 排序规则下会连带改写所有变体行——
    改一个收货人，另一个只差大小写的收货人也被一起改掉了。"""
    for v in ("Alice", "alice"):
        on_mysql.post("/api/shipment", json={"date": "2029-05-01", "recipient": v})
    r = on_mysql.post("/api/tags/recipient/rename", params={"old": "Alice", "new": "Alicia"})
    assert r.status_code == 200, r.text

    rows = on_mysql.get("/api/shipment").json()["items"]
    names = sorted(s["recipient"] for s in rows if s["recipient"])
    assert names == ["Alicia", "alice"], f"改名误伤了大小写变体：{names}"


def test_active_unique_is_byte_exact(on_mysql):
    """集运单号的「活跃行唯一」在 MySQL 上是生成列 + 唯一键。生成列不带 COLLATE 时
    继承表默认的 ai_ci，于是 'jf-2606a' 与 'JF-2606A' 在 SQLite 共存、在 MySQL 撞 1062。"""
    assert on_mysql.post("/api/shipment",
                         json={"date": "2029-05-01", "shipment_no": "jf-2606a"}).status_code == 200
    r = on_mysql.post("/api/shipment", json={"date": "2029-05-01", "shipment_no": "JF-2606A"})
    assert r.status_code == 200, f"大小写不同的集运单号被 MySQL 判成重复：{r.text}"


def test_duplicate_order_no_still_rejected_on_mysql(on_mysql):
    """反向确认：改成二进制排序规则**不能**把唯一约束本身弄丢——完全相同的值仍要撞。"""
    body = {"date": "2029-05-01", "order_no": "SAME-1", "platform": "淘宝"}
    assert on_mysql.post("/api/orders", json=body).status_code == 200
    assert on_mysql.post("/api/orders", json=body).status_code == 409


# --- 时间精度 -------------------------------------------------------------------

def test_datetime_keeps_microseconds(on_mysql, mysql_engine):
    """DATETIME(0) 对小数秒是**四舍五入**且不报 warning：14:59:59.7 会变成 15:00:00，
    落在 UTC 日界附近还会跨日。"""
    stamp = dt.datetime(2026, 8, 5, 14, 59, 59, 700000, tzinfo=dt.timezone.utc)
    with Session(mysql_engine) as s:
        s.add(FxRate(date=dt.date(2099, 1, 1), rate=Decimal("20.0"), fetched_at=stamp))
        s.commit()
        got = s.exec(select(FxRate).where(FxRate.date == dt.date(2099, 1, 1))).one()
        assert got.fetched_at.microsecond == 700000, f"微秒被 MySQL 抹掉了：{got.fetched_at}"


# --- 金额范围 -------------------------------------------------------------------

def test_overflow_amount_rejected_the_same_way(on_mysql):
    """SQLite 会静默存下超范围的派生总价、之后整页 422；MySQL 则 commit 时 1264 → 裸 500。
    两边都该在入口就 422（guard_cny），且不留脏行。"""
    r = on_mysql.post("/api/staging", json={
        "order_no": "OVF-MY",
        "items": [{"name": "量大", "quantity": 1_000_000, "unit_price_cny": "10000.00"}]})
    assert r.status_code == 422, r.text
    assert on_mysql.get("/api/staging").status_code == 200      # 列表照常打得开


# --- 迁移往返 -------------------------------------------------------------------

def test_roundtrip_sqlite_mysql_sqlite_is_lossless(client, mysql_engine, tmp_path):
    """SQLite → MySQL → SQLite 往返，业务数据必须逐字节一致。
    这一条同时覆盖类型转换、精度、排序规则、生成列排除等一整串环节。"""
    client.post("/api/orders", json={
        "date": "2029-06-01", "order_no": "RT-1", "platform": "淘宝",
        "title": "往返测试 ★ 絵文字🎌", "platform_account": "Alice",
        "postage_cny": "12.34",
        "items": [{"name": "甲", "quantity": 3, "unit_price_cny": "19.99"}]})
    client.post("/api/tags/recipient", json={"value": "alice"})      # 与 Alice 只差大小写

    src = get_engine()
    assert db_migrate.preflight(src, mysql_engine) == []             # 干净数据不该报体检问题
    db_migrate.replace_data(src, mysql_engine)

    back_url = f"sqlite:///{tmp_path / 'back.db'}"
    run_migrations(back_url)
    back = build_engine(back_url)
    try:
        db_migrate.replace_data(mysql_engine, back)
        with Session(src) as a, Session(back) as b:
            def snapshot(session):
                o = session.exec(select(Order).where(Order.order_no == "RT-1")).one()
                return (o.title, o.platform_account, str(o.price_cny), str(o.postage_cny),
                        o.created_at, o.updated_at, o.version)
            assert snapshot(a) == snapshot(b), "往返之后订单内容变了"
            tags = lambda s: sorted(t.value for t in s.exec(          # noqa: E731
                select(TagOption).where(TagOption.field == "recipient")).all())
            assert tags(a) == tags(b), "往返之后标签变了（大小写变体被折叠？）"
    finally:
        back.dispose()


def test_preflight_catches_rows_mysql_would_reject(client, mysql_engine, session):
    """SQLite 不检查 VARCHAR 长度，历史脏行会让拷贝在 MySQL 侧撞 1406、整批回滚。
    体检要在拷之前就把它指出来（哪张表、哪一行、哪个字段）。"""
    session.add(ShipmentOrder(date=dt.date(2029, 7, 1), shipment_no="X" * 200))
    session.commit()
    try:
        problems = db_migrate.preflight(get_engine(), mysql_engine)
        assert any("shipment_no" in p and "集运订单" in p for p in problems), problems
    finally:
        session.exec(delete(ShipmentOrder).where(ShipmentOrder.shipment_no == "X" * 200))
        session.commit()


# --- 环境本身 -------------------------------------------------------------------

def test_server_provides_expected_collations(mysql_engine):
    """本轮的修法建立在 utf8mb4_0900_bin 是 NO PAD 之上。换一台服务器先验这个前提。"""
    with mysql_engine.connect() as conn:
        assert dialect.bin_collation(conn) == dialect.BIN_COLLATION, \
            "该服务端没有 utf8mb4_0900_bin，已回退到 PAD SPACE 的 utf8mb4_bin（尾空格仍会折叠）"


def _declared_binstr_columns() -> set[tuple[str, str]]:
    """模型里**所有**声明成 `BinStr` 的列。

    从元数据推导，不写死名单。原先这里是一份手抄的 11 项集合，而模型上实际有 15 根——
    `miscexpense.category`（专门为它做过一次迁移 `e5f6a7b8c0d1`）与
    `pluginrecord` 的 `plugin_id`/`kind`/`key` 三根都不在名单里，
    等于插件私有存储的命名空间隔离在 MySQL 上**从来没被验过**。
    偏偏这套契约测试默认跳过（要真 MySQL 才跑），是全套里最少运行的一部分——
    手工名单落后了多久没人知道。

    `BinStr` 是工厂函数不是类型类，`isinstance` 认不出来；它的标记是
    mysql variant 上的 `collation`。"""
    from sqlmodel import SQLModel

    import app.models  # noqa: F401  触发全部表注册
    from app.db.dialect import BIN_COLLATION

    out = set()
    for tname, tbl in SQLModel.metadata.tables.items():
        for col in tbl.columns:
            variant = (getattr(col.type, "_variant_mapping", None) or {}).get("mysql")
            if getattr(variant, "collation", None) == BIN_COLLATION:
                out.add((tname, col.name))
    return out


def test_key_columns_actually_have_binary_collation(mysql_engine):
    """直接查 information_schema：模型声明与真实建出来的表可能脱节（比如老库漏跑迁移）。"""
    from sqlalchemy import text

    expected = _declared_binstr_columns()
    assert len(expected) >= 15, (
        f"只推导出 {len(expected)} 根 BinStr 列——探测方式多半已过期。"
        f"这个断言存在的意义是：推导要是悄悄返回空集，下面那句 `expected <= actual` 恒真，"
        f"整条测试会变成一句永远绿的废话。")
    with mysql_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND COLLATION_NAME IS NOT NULL")).all()
    actual = {(t, c) for t, c, coll in rows if coll.endswith("_bin")}
    assert expected <= actual, f"这些键列还是 ci 排序规则：{sorted(expected - actual)}"
    # 生成列同样得带 —— 唯一性是它说了算
    gen = {(t, c) for t, c, coll in rows if c.endswith("_active_key")}
    assert gen <= actual, f"生成列没带二进制排序规则：{sorted(gen - actual)}"


def test_binstr_search_stays_case_insensitive_on_mysql(on_mysql):
    """BinStr 列的**模糊搜索**在 MySQL 上必须与 SQLite 一样大小写不敏感。

    BinStr 让 `=` 逐字节（唯一性、等值批改要的就是这个），副作用是同列的 `LIKE`
    也跟着敏感——而 SQLite 的 LIKE 对 ASCII 本来就不敏感。`ci_contains` 就是为了
    把搜索口径拉回来，但它此前恒走非 MySQL 分支（`_name()` 认不出 Session），
    等于这层补偿从来没生效过。这条测的是结果，不是「代码里有没有调 ci_contains」。
    """
    assert on_mysql.post("/api/orders", json={
        "date": "2029-06-01", "title": "大小写探针", "order_no": "ABCdef123", "price_cny": 1,
    }).status_code == 200
    got = on_mysql.get("/api/orders", params={"q": "abcDEF", "limit": 50}).json()["items"]
    assert any(o["order_no"] == "ABCdef123" for o in got), \
        "MySQL 上按小写搜不到大写单号——ci_contains 没生效，与 SQLite 结果不一致"

def test_the_collation_downgrade_precheck_finds_case_only_duplicates(mysql_engine):
    """降级预检必须查得出「只差大小写」的重复值——那正是这条迁移放开的东西。

    `f2a3b4c5d6e7` 的降级第一步就把三处「活跃行唯一」连同 MySQL 生成列一起 DROP 掉，
    随后才跑它自己在 docstring 里承认「可能撞 1062」的那步（换回 ai_ci、重建唯一索引）。
    **MySQL 的 DDL 是隐式提交的**，而 `env.py` 开了 `transaction_per_migration`——
    失败之后：三处唯一约束全没了，版本号却被回滚、仍停在 `f2a3b4c5d6e7`。

    用户只看到一句 1062 原始报错，然后**一切看起来完全正常**：下次启动
    `upgrade head` 从这里往后照跑，应用正常打开，没有任何一处提示约束没了。
    真正的后果很久以后才显形——同一个订单号可以重复导入、重复建单，不再有 409，
    而**没有任何机制会再把那三条约束建回来**（后面的迁移都不碰它们）。

    **这里不跑真降级**：`command.downgrade` 会把这一条之后的十几条一起撤掉，
    而那条链在 MySQL 上本来就降不动（`ix_orderstaging_imported_order_id` 被外键需要），
    测一次就把库降成半截。要验的是判据本身——它查不出来，预检就是摆设。
    """
    import importlib.util

    from sqlalchemy import text

    from app.database import _ROOT                    # noqa: PLC2701 —— 迁移测试就该碰它

    path = _ROOT / "alembic" / "versions" / "f2a3b4c5d6e7_binary_collation_for_key_columns.py"
    spec = importlib.util.spec_from_file_location("_collation_mig", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    with mysql_engine.connect() as c:
        clean = mig._conflicts_after_downgrade(c)
        assert not any("标签取值" in x for x in clean), (
            f"库里本来就有只差大小写的标签值，这条用例的前提不成立：{clean}")

    with mysql_engine.begin() as c:
        c.execute(text("DELETE FROM tagoption WHERE value IN ('EMS-DG', 'ems-dg')"))
        c.execute(text("INSERT INTO tagoption (field, value) VALUES ('platform', 'EMS-DG')"))
        c.execute(text("INSERT INTO tagoption (field, value) VALUES ('platform', 'ems-dg')"))
    try:
        with mysql_engine.connect() as c:
            found = mig._conflicts_after_downgrade(c)
        assert any("标签取值" in x for x in found), (
            f"预检没查出只差大小写的标签值：{found}。"
            "降级会照跑下去，先删掉三处唯一约束再撞 1062——它们此后永远建不回来")
        assert any("EMS-DG" in x or "ems-dg" in x for x in found), (
            f"查出来了却没说是哪个值，用户没法照着清理：{found}")
    finally:
        with mysql_engine.begin() as c:
            c.execute(text("DELETE FROM tagoption WHERE value IN ('EMS-DG', 'ems-dg')"))


def test_the_whole_chain_survives_a_real_round_trip_on_mysql(mysql_engine):
    """`head → base → head` 在**真 MySQL** 上必须整条跑通。

    README 写着「全部迁移在真 MySQL 上跑通 upgrade→downgrade→upgrade」，
    而 2026-09-01 实测：**跑不通**。27 条降级在第 9 条
    （`a9b0c1d2e3f4`，删 `ix_orderstaging_imported_order_id`）倒在

        (1553, "Cannot drop index ...: needed in a foreign key constraint")

    InnoDB 要求外键列上有一根以它打头的索引。而 **MySQL 的 DDL 隐式提交** ⇒
    倒下之前那 8 条已经全部落地且不可回滚：`pluginrecord` 整张表被 DROP、
    `pluginconfig` 五列被删、`d2e3f4a5b6c7` 的降级还 `DELETE FROM fxrate`。
    也就是说，**照着仓库自己的文档做一次回滚，会永久丢掉插件私有存储和汇率历史，
    而且停在一个既不是旧版也不是新版的状态**。

    `test_no_downgrade_drops_an_index_on_a_table_it_also_drops` 从静态那一侧钉住
    同一件事，跑得快、到处都跑；这一条是端到端的，只有给了真 MySQL 才跑。
    两条都要，因为静态那条看不见 `a9b0c1d2e3f4` 这种「不 drop 表、只 drop 索引」的形状。

    收尾必须回到 head：这个库是别的契约测试的共用夹具。
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", MYSQL_URL.replace("%", "%%"))

    def tables() -> int:
        with mysql_engine.connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM information_schema.TABLES "
                                  "WHERE TABLE_SCHEMA = DATABASE()")).scalar()

    before = tables()
    assert before > 10, f"夹具没把 schema 建起来（只有 {before} 张表），前提不成立"
    try:
        command.downgrade(cfg, "base")
        left = tables()
        assert left <= 1, f"降到 base 之后还剩 {left} 张表（只该剩 alembic_version 或空）"
    finally:
        command.upgrade(cfg, "head")          # 别的用例还要用这个库
    assert tables() == before, f"往返之后表数变了：{before} → {tables()}"


def test_the_migration_chain_is_a_straight_line(mysql_engine):
    """迁移链必须是一条直线——没有分叉、没有多个 head。

    这条几乎是零成本的自检，但它是上面那条往返测试的**前提**：
    链上要是有两个 head，`downgrade base` 只会走其中一条，
    另一条上的表原样留着，而上面那句 `left <= 1` 会红得莫名其妙。
    与其让人去猜，不如让这条先说清楚。
    """
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, f"迁移链有 {len(heads)} 个 head：{heads}"



def test_fuzzy_search_agrees_on_ascii_and_document_where_it_does_not(mysql_engine):
    """`ci_contains` 的双引擎口径：**ASCII 一致**；全角/重音**不一致，且这是已知的**。

    `dialect.ci_contains` 的 docstring 说它「把搜索口径拉回原状」、
    「消灭 BinStr 那一版想消灭的那类发散」。2026-09-02 实测下来，
    那句话**只对 ASCII 成立**：

    | 存的值 | 搜 | SQLite | MySQL |
    |---|---|---|---|
    | `Hello` | `hello` / `HELLO` | ✓ | ✓ |
    | `ＳＦ１２３` | `ｓｆ`（全角小写） | ✗ | ✓ |
    | `ＳＦ１２３` | `SF`（**半角**） | ✗ | ✓ |
    | `Café Latte` | `cafe` | ✗ | ✓ |
    | `Straße` | `strasse` | ✗ | ✗ |

    MySQL 的 `utf8mb4_0900_ai_ci` 连**全角↔半角**与**重音**都不敏感，SQLite 的 LIKE
    一样都不做（它只对 ASCII 折叠大小写）。淘宝商品标题里全角字母与重音字母都真实存在
    （`ＳＫ－Ⅱ`、`L'Oréal`），所以这不是构造出来的边角。

    **本条不是要求两边一致**——让 SQLite 做 Unicode 折叠需要 ICU 构建，代价远大于收益，
    而发散的方向是「MySQL 多搜到几条」，不是搜错或漏搜关键数据（去重/等值批改走的是
    二进制 `=`，不受影响）。本条要求的是：
      ① **ASCII 那一半必须一致**——那是 `ci_contains` 真正的职责，坏了就是回归；
      ② 非 ASCII 的**现状被钉住**——哪天有人「顺手统一」了，这条会红，
         提醒他先去改 `ci_contains` 与本测试的 docstring，而不是让那句
         「消灭发散」的话继续说得比实际做到的大。
    """
    from sqlmodel import select as _select

    from app.db.dialect import ci_contains
    from app.models import ShipmentOrder as SO

    CASES = [
        ("普通 ASCII", "Hello",      ["hello", "HELLO"],            [True, True],  [True, True]),
        ("全角字母",   "ＳＦ１２３",  ["ｓｆ", "ＳＦ", "SF"],        [False, True, False], [True, True, True]),
        ("重音字母",   "Café Latte", ["cafe", "café", "CAFÉ"],      [False, True, False], [True, True, True]),
        ("德语 ß",     "Straße",     ["strasse", "straße"],         [False, True], [False, True]),
        ("中文",       "顺丰速运",    ["顺丰"],                      [True],        [True]),
    ]

    def probe(engine):
        out = {}
        with Session(engine) as s:
            for i, (_lab, val, _n, _e1, _e2) in enumerate(CASES):
                s.add(SO(date=dt.date(2027, 1, 1), shipment_no=f"CI-{i}", recipient=val,
                         shipment_status="待发出"))
            s.commit()
            for lab, val, needles, _e1, _e2 in CASES:
                out[lab] = [s.exec(_select(SO).where(
                    SO.recipient == val, ci_contains(SO.recipient, n, s))).first() is not None
                    for n in needles]
            for row in s.exec(_select(SO)).all():
                s.delete(row)
            s.commit()
        return out

    sqlite_got = probe(get_engine())          # 测试套件默认就跑在 SQLite 上
    mysql_got = probe(mysql_engine)

    for lab, _val, needles, want_sqlite, want_mysql in CASES:
        assert sqlite_got[lab] == want_sqlite, (
            f"SQLite 侧「{lab}」的行为变了：搜 {needles} 期望 {want_sqlite}，实际 {sqlite_got[lab]}")
        assert mysql_got[lab] == want_mysql, (
            f"MySQL 侧「{lab}」的行为变了：搜 {needles} 期望 {want_mysql}，实际 {mysql_got[lab]}")

    assert sqlite_got["普通 ASCII"] == mysql_got["普通 ASCII"], (
        f"**ASCII 大小写不敏感在两个引擎上不一致了**——这正是 ci_contains 的职责："
        f"SQLite {sqlite_got['普通 ASCII']} vs MySQL {mysql_got['普通 ASCII']}")
    assert sqlite_got["中文"] == mysql_got["中文"], "中文搜索在两个引擎上不一致了"
