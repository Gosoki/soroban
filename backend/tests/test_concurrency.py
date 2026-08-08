"""并发/交错写：直接在 Session 层复现「读—判断—写」竞态，验证 DB 层守卫真的守住了。

不用线程（SQLite 单写者会变成锁竞争而非逻辑竞态），而是开两个 Session 手工交错，
这正是 guarded_bump / 原子门闸 想防的那类交错。
"""
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.database import get_engine
from app.models import Order, OrderStaging, ShipmentOrder, ImportStatus
from app.routers.common import guarded_bump


def test_guarded_bump_only_one_winner(client):
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "cc1"}).json()
    with Session(get_engine()) as a, Session(get_engine()) as b:
        assert guarded_bump(a, Order, o["id"], o["version"]) is True
        a.commit()
        # b 拿着同一个旧 version 再来一次 → 必须失败
        assert guarded_bump(b, Order, o["id"], o["version"]) is False
        b.rollback()


def test_guarded_bump_refuses_soft_deleted(client):
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "cc2"}).json()
    client.delete(f"/api/orders/{o['id']}")
    with Session(get_engine()) as s:
        assert guarded_bump(s, Order, o["id"], o["version"]) is False


def test_import_gate_claims_only_once(client):
    """原子门闸：imported_order_id IS NULL 只让一次导入成功。"""
    from sqlalchemy import update as sa_update

    from app.models import utcnow
    s = client.post("/api/staging", json={"order_no": "CC-GATE"}).json()
    with Session(get_engine()) as a, Session(get_engine()) as b:
        def claim(sess, fake_order_id):
            return sess.execute(
                sa_update(OrderStaging)
                .where(OrderStaging.id == s["id"], OrderStaging.imported_order_id.is_(None))
                .values(import_status=ImportStatus.imported.value, imported_order_id=fake_order_id,
                        version=OrderStaging.version + 1, updated_at=utcnow())
            ).rowcount

        o = client.post("/api/orders", json={"date": "2027-04-01"}).json()
        assert claim(a, o["id"]) == 1
        a.commit()
        assert claim(b, o["id"]) == 0          # 已被认领
        b.rollback()


def test_attach_gate_blocks_double_attach(client):
    """挂靠守卫：shipment_order_id IS NULL 让并发的第二次挂靠 rowcount=0。"""
    from sqlalchemy import update as sa_update

    from app.models import utcnow
    s1 = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S1"}).json()
    s2 = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S2"}).json()
    o = client.post("/api/orders", json={"date": "2027-04-01"}).json()

    def attach(sess, ship_id):
        return sess.execute(
            sa_update(Order)
            .where(Order.id == o["id"], Order.shipment_order_id.is_(None), Order.is_delete.is_(False))
            .values(shipment_order_id=ship_id, version=Order.version + 1, updated_at=utcnow())
        ).rowcount

    with Session(get_engine()) as a, Session(get_engine()) as b:
        assert attach(a, s1["id"]) == 1
        a.commit()
        assert attach(b, s2["id"]) == 0
        b.rollback()
    assert client.get(f"/api/orders/{o['id']}").json()["shipment_order_id"] == s1["id"]


def test_attach_after_shipment_deleted_is_rejected(client):
    """EXISTS 守卫：集运单在极小窗口内被软删 → 挂靠必须失败，不留悬空外键。"""
    s = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S3"}).json()
    o = client.post("/api/orders", json={"date": "2027-04-01"}).json()
    client.delete(f"/api/shipment/{s['id']}")
    r = client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert r.status_code == 404
    assert client.get(f"/api/orders/{o['id']}").json()["shipment_order_id"] is None


def test_stale_patch_after_someone_else_wrote_is_409(client):
    """两个页面各持一份表单：先提交的赢，后提交的必须 409 而不是覆盖。"""
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "初始"}).json()
    tab_a = dict(o)
    tab_b = dict(o)
    assert client.patch(f"/api/orders/{o['id']}",
                        json={"version": tab_a["version"], "title": "A 改的"}).status_code == 200
    r = client.patch(f"/api/orders/{o['id']}", json={"version": tab_b["version"], "title": "B 改的"})
    assert r.status_code == 409
    assert client.get(f"/api/orders/{o['id']}").json()["title"] == "A 改的"


def test_staging_write_through_bumps_both_versions(client):
    """暂存写穿账本：两边 version 都要推进，否则另一页拿旧版本能悄悄覆盖。"""
    s = client.post("/api/staging", json={"order_no": "CC-WT", "title": "a"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    client.patch(f"/api/staging/{s['id']}", json={"version": row["version"], "title": "b"})
    assert client.get(f"/api/orders/{o['id']}").json()["version"] > o["version"]
    row2 = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                if x["id"] == s["id"])
    assert row2["version"] > row["version"]


def test_order_edit_bumps_mirrored_staging_version(client):
    """账本改动镜像回暂存时也必须推进暂存 version（否则暂存页旧表单不会 409）。"""
    s = client.post("/api/staging", json={"order_no": "CC-MIR", "title": "a"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    before = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                  if x["id"] == s["id"])["version"]
    client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": "改"})
    after = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                 if x["id"] == s["id"])["version"]
    assert after > before


# --- 跨表写的锁序必须全仓一致 -------------------------------------------------

def test_multi_table_writers_take_locks_in_one_order():
    """同时写 orders 与 orderstaging 的函数，必须**先 orders 后 orderstaging**。

    反向就是 AB-BA 锁环：同一对「暂存行 ↔ 已导入订单」被两条路径并发写时，
    MySQL/InnoDB 报 1213 死锁回滚一方，而 main.py 只挂了 IntegrityError / ValueError
    两个 handler，OperationalError(1213) 会直接逃成裸 500。

    这条只能静态查：**SQLite 是单写者串行，死锁在本地测试里永远复现不出来**，
    而项目支持双引擎。上一轮的教训正是「只在 MySQL 上炸」的问题 SQLite 测不到。
    """
    import ast
    import inspect
    from pathlib import Path

    _ORDER, _STAGING = "Order", "OrderStaging"
    bad = []
    for path in sorted((Path(__file__).resolve().parents[1] / "app" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            seq = []
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "guarded_bump"):
                    continue
                if len(node.args) < 2:
                    continue
                model = getattr(node.args[1], "id", None)
                if model in (_ORDER, _STAGING):
                    seq.append((node.lineno, model))
            seq.sort()
            models = [m for _, m in seq]
            if _ORDER in models and _STAGING in models and models.index(_STAGING) < models.index(_ORDER):
                bad.append(f"{path.name}::{fn.name} 先锁 {_STAGING} 后锁 {_ORDER}（行 {[l for l, _ in seq]}）")
    assert not bad, ("跨表写的锁序不一致，MySQL 上会死锁：\n  " + "\n  ".join(bad)
                     + "\n统一成 orders → orderstaging（orders 是共享字段的唯一真源）。")
