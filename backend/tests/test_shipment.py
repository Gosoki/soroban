"""集运订单：挂靠/解除的原子性、软删联动、特殊费、金额。"""
from decimal import Decimal


def mk_ship(client, **kw):
    body = {"date": "2026-06-01"}
    body.update(kw)
    r = client.post("/api/shipment", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def mk_order(client, **kw):
    body = {"date": "2026-06-01"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_special_fee_included(client):
    s = mk_ship(client, price_cny="100.00", fx_rate="20", special_fee_jpy=1200)
    assert s["jpy_settled"] == 2000 + 1200


def test_shipment_no_unique(client):
    mk_ship(client, shipment_no="JF-U1")
    assert client.post("/api/shipment", json={"date": "2026-06-01", "shipment_no": "JF-U1"}).status_code == 409


def test_soft_deleted_frees_shipment_no(client):
    s = mk_ship(client, shipment_no="JF-U2")
    client.delete(f"/api/shipment/{s['id']}")
    assert client.post("/api/shipment", json={"date": "2026-06-01", "shipment_no": "JF-U2"}).status_code == 200


def test_attach_and_detach(client):
    s = mk_ship(client, shipment_no="JF-A1")
    o = mk_order(client, title="待挂靠")
    r = client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["orders"]] == [o["id"]]
    r = client.delete(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert r.json()["orders"] == []


def test_manual_attach_does_not_touch_status(client):
    """点选挂靠 = 「打算放进这个包裹」，货未必已到集运仓 → 不推进状态。
    与 /ocr-express（来自集运方装箱清单，推进到「集运中」）刻意不同，见 attach_order 文档串。"""
    s = mk_ship(client, shipment_no="JF-ST1")
    o = mk_order(client, status="待收货")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["status"] == "待收货"


def test_detach_does_not_touch_status(client):
    s = mk_ship(client, shipment_no="JF-ST2")
    o = mk_order(client, status="集运中")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.delete(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["status"] == "集运中"


def test_attach_is_idempotent(client):
    s = mk_ship(client, shipment_no="JF-A2")
    o = mk_order(client)
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200


def test_attach_already_attached_elsewhere_rejected(client):
    s1, s2 = mk_ship(client, shipment_no="JF-A3"), mk_ship(client, shipment_no="JF-A4")
    o = mk_order(client)
    client.post(f"/api/shipment/{s1['id']}/order/{o['id']}")
    r = client.post(f"/api/shipment/{s2['id']}/order/{o['id']}")
    assert r.status_code == 422


def test_attach_to_deleted_shipment_404(client):
    s = mk_ship(client)
    o = mk_order(client)
    client.delete(f"/api/shipment/{s['id']}")
    assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 404


def test_deleting_shipment_detaches_children(client):
    s = mk_ship(client, shipment_no="JF-D1")
    o = mk_order(client)
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.delete(f"/api/shipment/{s['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["shipment_order_id"] is None


def test_deleted_child_hidden_from_shipment(client):
    s = mk_ship(client, shipment_no="JF-D2")
    o = mk_order(client)
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.delete(f"/api/orders/{o['id']}")
    assert client.get(f"/api/shipment/{s['id']}").json()["orders"] == []


def test_shipment_optimistic_lock(client):
    s = mk_ship(client)
    v = s["version"]
    assert client.patch(f"/api/shipment/{s['id']}", json={"version": v, "weight": "1.5"}).status_code == 200
    assert client.patch(f"/api/shipment/{s['id']}", json={"version": v, "weight": "2.5"}).status_code == 409


def test_attach_bumps_order_version(client):
    s = mk_ship(client, shipment_no="JF-V1")
    o = mk_order(client)
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["version"] > o["version"]


def test_bad_shipment_status_rejected(client):
    assert client.post("/api/shipment", json={"date": "2026-06-01", "status": "无此状态"}).status_code == 422


def test_special_fee_negative_rejected(client):
    assert client.post("/api/shipment", json={"date": "2026-06-01", "special_fee_jpy": -1}).status_code == 422


def test_ocr_express_missing_shipment_404(client):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = client.post("/api/shipment/999999/ocr-express", files={"file": ("a.png", png, "image/png")})
    assert r.status_code == 404


def test_shipment_orders_expose_items(client):
    s = mk_ship(client, shipment_no="JF-IT")
    o = mk_order(client, items=[{"name": "内含物", "quantity": 3, "unit_price_cny": "5.00"}])
    r = client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    brief = r.json()["orders"][0]
    assert brief["items"][0]["name"] == "内含物"
    assert brief["items"][0]["quantity"] == 3


def test_money_recomputed_on_patch(client):
    s = mk_ship(client, price_cny="10.00", fx_rate="20")
    assert s["jpy_settled"] == 200
    r = client.patch(f"/api/shipment/{s['id']}", json={"version": s["version"], "fx_rate": "30"})
    assert r.json()["jpy_settled"] == 300
    assert Decimal(r.json()["price_cny"]) == Decimal("10.00")
