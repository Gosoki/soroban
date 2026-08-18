"""商品订单 CRUD：物品派生价、邮费、乐观锁、软删、唯一约束、集运挂靠校验。"""
from decimal import Decimal

import pytest

from app.models import PurchaseStatus


def mk_order(client, **kw):
    body = {"date": "2026-03-01"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- 基础：物品为最小单位，订单价由物品派生 ---------------------------------

def test_create_seeds_one_item_when_none_given(client):
    o = mk_order(client, title="某商品", price_cny="100.00")
    assert len(o["items"]) == 1
    assert o["items"][0]["auto"] is True
    assert o["items"][0]["name"] == "某商品"
    assert Decimal(o["price_cny"]) == Decimal("100.00")


def test_create_with_items_derives_price(client):
    o = mk_order(client, items=[{"name": "a", "quantity": 3, "unit_price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "unit_price_cny": "5.50"}])
    assert Decimal(o["price_cny"]) == Decimal("35.50")


def test_postage_added_on_top_of_items(client):
    o = mk_order(client, postage_cny="8.00",
                 items=[{"name": "a", "quantity": 2, "unit_price_cny": "10.00"}])
    assert Decimal(o["price_cny"]) == Decimal("28.00")


def test_seed_price_excludes_postage_no_double_count(client):
    """只给总价+邮费（无物品明细）：货款 = 总价-邮费，sync 再加邮费 → 总价不变。"""
    o = mk_order(client, title="x", price_cny="100.00", postage_cny="10.00")
    assert Decimal(o["price_cny"]) == Decimal("100.00")
    assert Decimal(o["items"][0]["unit_price_cny"]) == Decimal("90.00")


def test_postage_greater_than_total_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-03-01", "price_cny": "10", "postage_cny": "20"})
    assert r.status_code == 422


def test_price_cny_not_directly_writable(client):
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "price_cny": "9999"})
    assert r.status_code == 200
    assert Decimal(r.json()["price_cny"]) == Decimal("10.00")   # 仍由物品派生


def test_empty_items_list_backfills_placeholder(client):
    o = mk_order(client, title="s", items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "items": []})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1       # ≥1 物品的不变量


# --- 乐观锁 -----------------------------------------------------------------

def test_optimistic_lock_conflict(client):
    o = mk_order(client, title="a")
    v = o["version"]
    assert client.patch(f"/api/orders/{o['id']}", json={"version": v, "title": "b"}).status_code == 200
    r = client.patch(f"/api/orders/{o['id']}", json={"version": v, "title": "c"})
    assert r.status_code == 409


def test_version_increments_on_patch(client):
    o = mk_order(client, title="a")
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": "b"})
    assert r.json()["version"] == o["version"] + 1


def test_patch_missing_version_is_422(client):
    o = mk_order(client)
    assert client.patch(f"/api/orders/{o['id']}", json={"title": "x"}).status_code == 422


# --- 软删 -------------------------------------------------------------------

def test_soft_delete_hides_from_list_and_get(client):
    o = mk_order(client, title="tobedeleted")
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
    mk_order(client, title="店A", items=[{"name": "独特物品名XYZ", "quantity": 1, "unit_price_cny": "1"}])
    r = client.get("/api/orders", params={"q": "独特物品名XYZ"})
    assert r.json()["total"] >= 1


def test_search_like_wildcards_are_escaped(client):
    """搜索串里的 % 和 _ 必须当字面量（autoescape），否则 '%' 会匹配所有行。

    **判据不能是「命中数 < 全库行数」**：那要求库里恰好还有别的行，而这条测试
    自己只造一行 ⇒ `all_total` 就是 1 ⇒ `1 < 1` 恒假。它一直绿只是因为
    前面的用例留下了订单，单独 `-k` 跑立刻红——而转义其实是好的。
    （逐条单跑全套时抓到的。）

    改成自足的一对：两行都带同一个随机 tag，只有一行的标题里**字面含 `%tag`**。
    搜 `%tag` 时——转义生效只会命中 A；不生效则 `%` 是通配符，
    `LIKE '%%tag%'` 把 B 也拉进来。判据因此只看「B 在不在结果里」，
    既不依赖全库状态，也不受分页影响（能匹配的本来就只有这两行）。
    """
    import uuid

    tag = uuid.uuid4().hex[:8]
    a, b = f"通配测试%{tag}", f"通配测试X{tag}"
    mk_order(client, title=a)
    mk_order(client, title=b)
    items = client.get("/api/orders", params={"q": f"%{tag}", "limit": 200}).json()["items"]
    titles = [it["title"] for it in items]
    assert a in titles, f"转义之后，字面含 % 的那行应该还搜得到：{titles}"
    assert b not in titles, "LIKE 通配符未被转义，'%' 把不含它的行也匹配进来了"


def test_search_underscore_escaped(client):
    """`_` 是 LIKE 的**单字符通配符**，同样必须当字面量。

    **判据必须有诱饵行。** 原先只造 `a_b_c 测试下划线` 一行、再断言
    「结果里每行都含 a_b」——而库里根本没有能被未转义的 `a_b` 通配到的行，
    于是把 `autoescape=True` 整个去掉，这条**照样绿**（实测）。
    诱饵 `aXb_c` 恰好落在「`_` 当通配符才会命中」的位置上：
    转义生效 → 模式要求第 2 个字符**字面是** `_`，`X` 对不上，诱饵不该出现。
    """
    import uuid

    tag = uuid.uuid4().hex[:8]
    hit, bait = f"a_b_c-{tag}", f"aXb_c-{tag}"
    mk_order(client, title=hit)
    mk_order(client, title=bait)
    items = client.get("/api/orders", params={"q": f"a_b_c-{tag}", "limit": 200}).json()["items"]
    titles = [it["title"] for it in items]
    assert hit in titles, f"字面匹配的那行反而搜不到：{titles}"
    assert bait not in titles, "`_` 未被转义，它当成了单字符通配符"


def test_exact_order_no_filter(client):
    mk_order(client, order_no="EXACT-123", platform="京东")
    r = client.get("/api/orders", params={"order_no": "EXACT-123"})
    assert r.json()["total"] == 1


def test_pagination_bounds(client):
    assert client.get("/api/orders", params={"limit": 0}).status_code == 422
    assert client.get("/api/orders", params={"limit": 201}).status_code == 422
    assert client.get("/api/orders", params={"offset": -1}).status_code == 422


# --- 状态白名单 ---------------------------------------------------------------

@pytest.mark.parametrize("status", [s.value for s in PurchaseStatus])
def test_all_enum_statuses_accepted(client, status):
    r = client.post("/api/orders", json={"date": "2026-03-01", "purchase_status": status})
    assert r.status_code == 200, f"{status}: {r.text}"


def test_bogus_status_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-03-01", "purchase_status": "不存在的状态"})
    assert r.status_code == 422
