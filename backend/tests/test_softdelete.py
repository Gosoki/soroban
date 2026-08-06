"""软删的乐观锁纪律：删除同样是一次写，必须推进 version / updated_at。"""
from sqlmodel import Session, select

from app.database import get_engine
from app.models import MiscExpense, Order, ShipmentOrder


def _row(model, oid):
    with Session(get_engine()) as s:
        return s.exec(select(model).where(model.id == oid)).one()


def test_order_soft_delete_bumps_version(client):
    o = client.post("/api/orders", json={"date": "2026-12-20", "title": "sd1"}).json()
    client.delete(f"/api/orders/{o['id']}")
    row = _row(Order, o["id"])
    assert row.is_delete is True
    assert row.version == o["version"] + 1
    assert row.updated_at is not None


def test_shipment_soft_delete_bumps_version(client):
    s = client.post("/api/shipment", json={"date": "2026-12-20"}).json()
    client.delete(f"/api/shipment/{s['id']}")
    assert _row(ShipmentOrder, s["id"]).version == s["version"] + 1


def test_misc_soft_delete_bumps_version(client):
    m = client.post("/api/misc", json={"date": "2026-12-20", "name": "sd"}).json()
    client.delete(f"/api/misc/{m['id']}")
    assert _row(MiscExpense, m["id"]).version == m["version"] + 1


def test_batch_and_single_delete_agree(client):
    """按账号批量软删与单条删除必须同语义（都置 is_delete + bump version）。"""
    import tempfile
    from pathlib import Path

    from app.config import settings
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "soroban-scraper-taobao"
    (d / ".state").mkdir(parents=True)
    (d / "plugin.toml").write_text('id = "taobao"\nplatform = "taobao"\nstate_dir = ".state"\n',
                                   encoding="utf-8")
    old = settings.SCRAPER_DIR
    settings.SCRAPER_DIR = str(tmp)
    try:
        client.post("/api/plugins/taobao/account", params={"name": "sdacct"})
        a = client.post("/api/orders", json={"date": "2026-12-21", "platform_account": "sdacct"}).json()
        b = client.post("/api/orders", json={"date": "2026-12-21", "platform_account": "sdacct"}).json()
        client.delete(f"/api/orders/{a['id']}")                                    # 单条
        client.delete("/api/plugins/taobao/account/orders", params={"account": "sdacct"})  # 批量
        ra, rb = _row(Order, a["id"]), _row(Order, b["id"])
        assert ra.is_delete and rb.is_delete
        assert ra.version == a["version"] + 1 and rb.version == b["version"] + 1
    finally:
        settings.SCRAPER_DIR = old


def test_soft_deleted_row_still_frees_unique_key(client):
    """bump version 不能影响「软删后订单号可复用」这条既有语义。"""
    o = client.post("/api/orders", json={"date": "2026-12-22", "order_no": "SDU-1",
                                         "platform": "淘宝"}).json()
    client.delete(f"/api/orders/{o['id']}")
    r = client.post("/api/orders", json={"date": "2026-12-22", "order_no": "SDU-1",
                                         "platform": "淘宝"})
    assert r.status_code == 200
