"""商品订单 CRUD：物品派生价、邮费、乐观锁、软删、唯一约束、集运挂靠校验。"""
from decimal import Decimal

import pytest

from app.models import OrderStatus


def mk_order(client, **kw):
    body = {"date": "2026-03-01"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- 基础：物品为最小单位，订单价由物品派生 ---------------------------------

def test_create_seeds_one_item_when_none_given(client):
    o = mk_order(client, shop="某商品", price_cny="100.00")
    assert len(o["items"]) == 1
    assert o["items"][0]["auto"] is True
    assert o["items"][0]["name"] == "某商品"
    assert Decimal(o["price_cny"]) == Decimal("100.00")


def test_create_with_items_derives_price(client):
    o = mk_order(client, items=[{"name": "a", "quantity": 3, "price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "price_cny": "5.50"}])
    assert Decimal(o["price_cny"]) == Decimal("35.50")


def test_postage_added_on_top_of_items(client):
    o = mk_order(client, postage_cny="8.00",
                 items=[{"name": "a", "quantity": 2, "price_cny": "10.00"}])
    assert Decimal(o["price_cny"]) == Decimal("28.00")


def test_seed_price_excludes_postage_no_double_count(client):
    """只给总价+邮费（无物品明细）：货款 = 总价-邮费，sync 再加邮费 → 总价不变。"""
    o = mk_order(client, shop="x", price_cny="100.00", postage_cny="10.00")
    assert Decimal(o["price_cny"]) == Decimal("100.00")
    assert Decimal(o["items"][0]["price_cny"]) == Decimal("90.00")


def test_postage_greater_than_total_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-03-01", "price_cny": "10", "postage_cny": "20"})
    assert r.status_code == 422


def test_price_cny_not_directly_writable(client):
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "price_cny": "10.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "price_cny": "9999"})
    assert r.status_code == 200
    assert Decimal(r.json()["price_cny"]) == Decimal("10.00")   # 仍由物品派生


def test_empty_items_list_backfills_placeholder(client):
    o = mk_order(client, shop="s", items=[{"name": "a", "quantity": 1, "price_cny": "10.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "items": []})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1       # ≥1 物品的不变量


# --- 乐观锁 -----------------------------------------------------------------

def test_optimistic_lock_conflict(client):
    o = mk_order(client, shop="a")
    v = o["version"]
    assert client.patch(f"/api/orders/{o['id']}", json={"version": v, "shop": "b"}).status_code == 200
    r = client.patch(f"/api/orders/{o['id']}", json={"version": v, "shop": "c"})
    assert r.status_code == 409


def test_version_increments_on_patch(client):
    o = mk_order(client, shop="a")
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "shop": "b"})
    assert r.json()["version"] == o["version"] + 1


def test_patch_missing_version_is_422(client):
    o = mk_order(client)
    assert client.patch(f"/api/orders/{o['id']}", json={"shop": "x"}).status_code == 422


# --- 软删 -------------------------------------------------------------------

def test_soft_delete_hides_from_list_and_get(client):
    o = mk_order(client, shop="tobedeleted")
    assert client.delete(f"/api/orders/{o['id']}").status_code == 200
    assert client.get(f"/api/orders/{o['id']}").status_code == 404
    ids = [x["id"] for x in client.get("/api/orders", params={"limit": 200}).json()["items"]]
    assert o["id"] not in ids


def test_delete_twice_is_404(client):
    o = mk_order(client)
    client.delete(f"/api/orders/{o['id']}")
    assert client.delete(f"/api/orders/{o['id']}").status_code == 404


def test_patch_deleted_order_is_404(client):
    o = mk_order(client)
    client.delete(f"/api/orders/{o['id']}")
    assert client.patch(f"/api/orders/{o['id']}", json={"version": o["version"]}).status_code == 404


# --- 唯一性：(order_no, platform) 活跃行唯一 ---------------------------------

def test_duplicate_order_no_same_platform_conflicts(client):
    mk_order(client, order_no="U-1", platform="淘宝")
    r = client.post("/api/orders", json={"date": "2026-03-01", "order_no": "U-1", "platform": "淘宝"})
    assert r.status_code == 409


def test_duplicate_order_no_different_platform_ok(client):
    mk_order(client, order_no="U-2", platform="淘宝")
    r = client.post("/api/orders", json={"date": "2026-03-01", "order_no": "U-2", "platform": "闲鱼"})
    assert r.status_code == 200


def test_duplicate_order_no_null_platform_conflicts(client):
    """来源未填时也不许重复（COALESCE(platform,'') 参与索引）。"""
    mk_order(client, order_no="U-3")
    r = client.post("/api/orders", json={"date": "2026-03-01", "order_no": "U-3"})
    assert r.status_code == 409


def test_soft_deleted_frees_order_no(client):
    o = mk_order(client, order_no="U-4", platform="淘宝")
    client.delete(f"/api/orders/{o['id']}")
    r = client.post("/api/orders", json={"date": "2026-03-01", "order_no": "U-4", "platform": "淘宝"})
    assert r.status_code == 200


# --- 集运挂靠校验 -------------------------------------------------------------

def test_attach_nonexistent_shipment_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-03-01", "shipment_order_id": 999999})
    assert r.status_code == 422


def test_patch_attach_nonexistent_shipment_rejected(client):
    o = mk_order(client)
    r = client.patch(f"/api/orders/{o['id']}",
                     json={"version": o["version"], "shipment_order_id": 999999})
    assert r.status_code == 422


def test_attach_soft_deleted_shipment_rejected(client):
    sh = client.post("/api/shipment", json={"date": "2026-03-01"}).json()
    client.delete(f"/api/shipment/{sh['id']}")
    r = client.post("/api/orders", json={"date": "2026-03-01", "shipment_order_id": sh["id"]})
    assert r.status_code == 422


# --- 筛选 / 搜索 ---------------------------------------------------------------

def test_search_matches_item_name(client):
    mk_order(client, shop="店A", items=[{"name": "独特物品名XYZ", "quantity": 1, "price_cny": "1"}])
    r = client.get("/api/orders", params={"q": "独特物品名XYZ"})
    assert r.json()["total"] >= 1


def test_search_like_wildcards_are_escaped(client):
    """搜索串里的 % 和 _ 必须当字面量（autoescape），否则 '%' 会匹配所有行。"""
    mk_order(client, shop="含百分号%的商品")
    all_total = client.get("/api/orders", params={"limit": 1}).json()["total"]
    r = client.get("/api/orders", params={"q": "%"})
    assert r.json()["total"] < all_total, "LIKE 通配符未被转义，'%' 匹配到了全部行"


def test_search_underscore_escaped(client):
    mk_order(client, shop="a_b_c 测试下划线")
    r = client.get("/api/orders", params={"q": "a_b"})
    for it in r.json()["items"]:
        assert "a_b" in (it["shop"] or "")


def test_exact_order_no_filter(client):
    mk_order(client, order_no="EXACT-123", platform="京东")
    r = client.get("/api/orders", params={"order_no": "EXACT-123"})
    assert r.json()["total"] == 1


def test_pagination_bounds(client):
    assert client.get("/api/orders", params={"limit": 0}).status_code == 422
    assert client.get("/api/orders", params={"limit": 201}).status_code == 422
    assert client.get("/api/orders", params={"offset": -1}).status_code == 422


# --- 状态白名单 ---------------------------------------------------------------

@pytest.mark.parametrize("status", [s.value for s in OrderStatus])
def test_all_enum_statuses_accepted(client, status):
    r = client.post("/api/orders", json={"date": "2026-03-01", "status": status})
    assert r.status_code == 200, f"{status}: {r.text}"


def test_bogus_status_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-03-01", "status": "不存在的状态"})
    assert r.status_code == 422
