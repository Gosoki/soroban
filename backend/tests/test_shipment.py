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
    两条挂靠路径都不改状态：国际段由集运单表达，挂上自动跟随、释放自动回落。"""
    s = mk_ship(client, shipment_no="JF-ST1")
    o = mk_order(client, status="待收货")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["status"] == "待收货"


def test_attach_inherits_status_and_detach_restores(client):
    """订单只记国内段；挂上集运单后**显示**跟随那张单，释放后回落到自己的状态。

    关键是挂靠期间 `status` 必须原样保留——曾经自动挂靠会把「集运中」写进 status，
    那样一释放，回落到的就是被覆盖过的值，而不是真实的国内段状态。"""
    s = mk_ship(client, shipment_no="JF-ST2")          # 新建集运单默认「打包中」
    o = mk_order(client, status="已签收")

    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["status"] == "已签收", "挂靠不该动订单自己的国内段状态"
    assert got["effective_status"] == "打包中", "挂靠后显示的应是集运单的状态"

    client.delete(f"/api/shipment/{s['id']}/order/{o['id']}")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["status"] == got["effective_status"] == "已签收", "释放后应回落到自身状态"


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


# --- 状态继承：订单只记国内段，国际段跟随所挂集运单 -------------------------------

def test_status_filter_matches_what_is_displayed(client):
    """筛选口径必须与显示口径一致。曾经的隐患：列表里显示的是集运单状态（继承来的），
    筛选却按订单自身状态查——于是「界面上一排『已发出』，筛『已发出』一条也搜不到」。"""
    s = mk_ship(client, shipment_no="JF-FLT1")
    o = mk_order(client, status="已签收")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.patch(f"/api/shipment/{s['id']}", json={
        "version": client.get(f"/api/shipment/{s['id']}").json()["version"], "status": "已发出"})

    hit = client.get("/api/orders", params={"status": "已发出", "limit": 200}).json()["items"]
    assert any(x["id"] == o["id"] for x in hit), "按继承来的集运状态筛不到该订单"

    miss = client.get("/api/orders", params={"status": "已签收", "limit": 200}).json()["items"]
    assert all(x["id"] != o["id"] for x in miss), "挂靠中的订单不该再被自身国内段状态筛出来"


def test_effective_status_falls_back_when_shipment_soft_deleted(client):
    """集运单被软删后界面上已经看不见它了，再拿它的状态显示就是个查无此处的幽灵值。"""
    s = mk_ship(client, shipment_no="JF-SD1")
    o = mk_order(client, status="已签收")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.delete(f"/api/shipment/{s['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["effective_status"] == "已签收"


def test_unattached_order_effective_status_equals_own(client):
    o = mk_order(client, status="待收货")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["status"] == got["effective_status"] == "待收货"


def test_ocr_auto_attach_does_not_write_status():
    """自动挂靠（「内含快递」截图）曾把「集运中」写进订单 status——那会污染国内段状态，
    一旦释放，回落到的是被覆盖过的值。现在两条挂靠路径都只写外键、不动状态。"""
    import inspect

    from app.routers import shipment as mod

    src = inspect.getsource(mod.ocr_attach_express)
    assert '"status"' not in src and "OrderStatus" not in src, \
        "自动挂靠又开始写订单状态了"


def test_order_status_enum_is_domestic_only():
    from app.models import OrderStatus

    assert {s.value for s in OrderStatus} == {
        "待付款", "待发货", "待收货", "已签收", "退款", "交易关闭"}
