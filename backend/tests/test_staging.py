"""暂存 → 导入账本的全流程：写穿、镜像、原子门闸、删除一致性。"""
from decimal import Decimal


def mk_staging(client, **kw):
    r = client.post("/api/staging", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_derives_price_from_items(client):
    s = mk_staging(client, order_no="S-1", title="店",
                   items=[{"name": "a", "quantity": 2, "unit_price_cny": "10.00"}], postage_cny="5.00")
    assert Decimal(s["price_cny"]) == Decimal("25.00")


def test_staging_order_no_unique(client):
    mk_staging(client, order_no="S-DUP")
    r = client.post("/api/staging", json={"order_no": "S-DUP"})
    assert r.status_code == 409


def test_multiple_null_order_no_allowed(client):
    mk_staging(client, title="空单号1")
    r = client.post("/api/staging", json={"title": "空单号2"})
    assert r.status_code == 200


def test_import_creates_order_and_marks_row(client):
    s = mk_staging(client, order_no="S-IMP-1", title="店", platform="淘宝",
                   order_date="2026-05-01", purchase_status="待发货",
                   items=[{"name": "物品", "quantity": 2, "unit_price_cny": "30.00"}])
    r = client.post(f"/api/staging/{s['id']}/import")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["order_no"] == "S-IMP-1"
    assert o["platform"] == "淘宝"
    assert o["purchase_status"] == "待发货"
    assert o["created_via"] == "imported"
    assert Decimal(o["price_cny"]) == Decimal("60.00")
    row = client.get("/api/staging", params={"import_status": "已导入", "limit": 200}).json()["items"]
    assert any(x["id"] == s["id"] and x["imported_order_id"] == o["id"] for x in row)


def test_import_twice_conflicts(client):
    s = mk_staging(client, order_no="S-IMP-2")
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 200
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 409


def test_import_duplicate_order_no_conflicts(client):
    """账本里已有同号同来源 → 导入必须 409，且不留下孤儿订单。"""
    client.post("/api/orders", json={"date": "2026-05-01", "order_no": "S-CLASH", "platform": "淘宝"})
    s = mk_staging(client, order_no="S-CLASH", platform="淘宝")
    r = client.post(f"/api/staging/{s['id']}/import")
    assert r.status_code == 409
    got = client.get("/api/orders", params={"order_no": "S-CLASH"}).json()
    assert got["total"] == 1, "409 后应回滚，不该留下重复订单"


def test_imported_row_shows_ledger_values(client):
    s = mk_staging(client, order_no="S-MIRROR", title="原名",
                   items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": "改后的名"})
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    row = next(x for x in rows if x["id"] == s["id"])
    assert row["title"] == "改后的名", "已导入行应显示账本实时值"


def test_order_edit_mirrors_items_back_to_staging(client):
    s = mk_staging(client, order_no="S-MIRROR-2", title="店",
                   items=[{"name": "旧物品", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "新物品", "quantity": 2, "unit_price_cny": "7.00"}],
    })
    # 删掉账本单 → 暂存复位为待处理，且带的是镜像后的新物品
    client.delete(f"/api/orders/{o['id']}")
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    row = next(x for x in rows if x["id"] == s["id"])
    assert row["import_status"] == "待处理"
    assert row["imported_order_id"] is None
    assert [i["name"] for i in row["items"]] == ["新物品"]
    assert Decimal(row["price_cny"]) == Decimal("14.00")


def test_deleting_order_allows_reimport(client):
    s = mk_staging(client, order_no="S-REIMP")
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.delete(f"/api/orders/{o['id']}")
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 200


def test_cannot_delete_imported_staging(client):
    s = mk_staging(client, order_no="S-NODEL")
    client.post(f"/api/staging/{s['id']}/import")
    assert client.delete(f"/api/staging/{s['id']}").status_code == 409


def test_cannot_ignore_imported_staging(client):
    s = mk_staging(client, order_no="S-NOIGN")
    client.post(f"/api/staging/{s['id']}/import")
    assert client.post(f"/api/staging/{s['id']}/ignore").status_code == 409


def test_ignore_pending_ok(client):
    s = mk_staging(client, order_no="S-IGN")
    r = client.post(f"/api/staging/{s['id']}/ignore")
    assert r.status_code == 200 and r.json()["import_status"] == "已忽略"


def test_ignore_missing_is_404(client):
    assert client.post("/api/staging/999999/ignore").status_code == 404


def test_patch_imported_writes_through_to_ledger(client):
    s = mk_staging(client, order_no="S-WT", title="a",
                   items=[{"name": "x", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    cur = next(x for x in rows if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}", json={"version": cur["version"], "title": "写穿后"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/orders/{o['id']}").json()["title"] == "写穿后"


def test_staging_optimistic_lock(client):
    s = mk_staging(client, order_no="S-LOCK")
    v = s["version"]
    assert client.patch(f"/api/staging/{s['id']}", json={"version": v, "title": "a"}).status_code == 200
    assert client.patch(f"/api/staging/{s['id']}", json={"version": v, "title": "b"}).status_code == 409


def test_staging_bad_order_status_rejected(client):
    r = client.post("/api/staging", json={"order_no": "S-BADST", "purchase_status": "乱七八糟"})
    assert r.status_code == 422


def test_staging_search_escapes_wildcards(client):
    mk_staging(client, order_no="S-PCT", title="百分之%百")
    total_all = client.get("/api/staging", params={"limit": 1}).json()["total"]
    r = client.get("/api/staging", params={"q": "%"})
    assert r.json()["total"] < total_all
