"""并发/交错写：直接在 Session 层复现「读—判断—写」竞态，验证 DB 层守卫真的守住了。

不用线程（SQLite 单写者会变成锁竞争而非逻辑竞态），而是开两个 Session 手工交错，
这正是 guarded_bump / 原子门闸 想防的那类交错。
"""
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.database import get_engine
from app.models import Order, OrderStaging, ShipmentOrder, StagingStatus
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
                .values(import_status=StagingStatus.imported.value, imported_order_id=fake_order_id,
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
