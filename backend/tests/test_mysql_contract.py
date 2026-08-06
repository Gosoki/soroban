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


def test_key_columns_actually_have_binary_collation(mysql_engine):
    """直接查 information_schema：模型声明与真实建出来的表可能脱节（比如老库漏跑迁移）。"""
    from sqlalchemy import text

    expected = {("tagoption", "value"), ("tagoption", "field"), ("user", "username"),
                ("orders", "order_no"), ("orders", "platform"), ("orders", "platform_account"),
                ("orderstaging", "order_no"), ("orderstaging", "platform"),
                ("orderstaging", "platform_account"),
                ("shipmentorder", "shipment_no"), ("shipmentorder", "recipient")}
    with mysql_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND COLLATION_NAME IS NOT NULL")).all()
    actual = {(t, c) for t, c, coll in rows if coll.endswith("_bin")}
    assert expected <= actual, f"这些键列还是 ci 排序规则：{sorted(expected - actual)}"
    # 生成列同样得带 —— 唯一性是它说了算
    gen = {(t, c) for t, c, coll in rows if c.endswith("_active_key")}
    assert gen <= actual, f"生成列没带二进制排序规则：{sorted(gen - actual)}"
