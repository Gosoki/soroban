"""边界与「行为契约」测试：把容易被后续改动踩坏的隐性规则钉住。
若某条断言看起来不合直觉，注释里写了它为什么是当前的正确/已知行为。"""
from decimal import Decimal

import pytest


def mk_order(client, **kw):
    body = {"date": "2027-02-01"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- 种子价 → 物品单价的折算 --------------------------------------------------

def test_seed_split_across_quantity(client):
    """只有总价、物品无单价时：总价折成第一条的**单价** = 总价/数量。"""
    o = mk_order(client, price_cny="30.00",
                 items=[{"name": "a", "quantity": 3}, {"name": "b", "quantity": 1}])
    assert Decimal(o["items"][0]["price_cny"]) == Decimal("10.00")
    assert Decimal(o["items"][1]["price_cny"]) == Decimal("0.00")
    assert Decimal(o["price_cny"]) == Decimal("30.00")
    assert all(i["auto"] for i in o["items"])          # 全部标 auto，待人工拆分


def test_seed_split_rounding_may_shift_cents(client):
    """数量除不尽时单价取整到分，总价会差几分——已知且被 backfill 工具文档化的行为。"""
    o = mk_order(client, price_cny="10.00", items=[{"name": "a", "quantity": 3}])
    assert Decimal(o["items"][0]["price_cny"]) == Decimal("3.33")
    assert Decimal(o["price_cny"]) == Decimal("9.99")


def test_partially_priced_items_zero_the_rest(client):
    """只要有**任一**物品带价，就按原样采用；无价的记 0 并标 auto（灰显=待补价）。"""
    o = mk_order(client, price_cny="99.00",
                 items=[{"name": "a", "quantity": 1, "price_cny": "5.00"}, {"name": "b", "quantity": 1}])
    assert Decimal(o["items"][0]["price_cny"]) == Decimal("5.00")
    assert Decimal(o["items"][1]["price_cny"]) == Decimal("0.00")
    assert o["items"][0]["auto"] is False and o["items"][1]["auto"] is True
    assert Decimal(o["price_cny"]) == Decimal("5.00")   # 种子价被物品明细覆盖


def test_clearing_all_prices_zeroes_them(client):
    """清空全部单价 = 「单价未知」，一律记 0 + auto（灰显待补价）——与「只清空部分单价」同口径。
    （旧行为是把旧总价整个折到第一条上，同一个动作两种结果，已修。）"""
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "price_cny": "20.00"}])
    assert Decimal(o["price_cny"]) == Decimal("30.00")
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "a", "quantity": 1}, {"name": "b", "quantity": 1}]}).json()
    assert [Decimal(i["price_cny"]) for i in body["items"]] == [Decimal("0.00"), Decimal("0.00")]
    assert all(i["auto"] for i in body["items"])
    assert Decimal(body["price_cny"]) == Decimal("0.00")


def test_clearing_one_price_zeroes_only_that_one(client):
    """对照组：只清空一条单价时的行为——两者必须一致，否则就是「同一动作两种结果」。"""
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "price_cny": "20.00"}])
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "a", "quantity": 1, "price_cny": "10.00"},
                  {"name": "b", "quantity": 1}]}).json()
    assert [Decimal(i["price_cny"]) for i in body["items"]] == [Decimal("10.00"), Decimal("0.00")]


def test_postage_change_with_unpriced_items_does_not_rebase_goods(client):
    """同一次 PATCH 既改邮费又送「无单价」物品：无单价就是无单价，记 0；
    不再拿「旧总价 − 新邮费」倒推货款（那会让总价看着没变、货款却悄悄改了）。"""
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "price_cny": "100.00"}])
    assert Decimal(o["price_cny"]) == Decimal("110.00")
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "postage_cny": "20.00",
        "items": [{"name": "a", "quantity": 1}]}).json()
    assert Decimal(body["items"][0]["price_cny"]) == Decimal("0.00")
    assert Decimal(body["price_cny"]) == Decimal("20.00")     # 只剩邮费


def test_explicit_seed_price_still_splits(client):
    """显式给种子价（爬虫/OCR 的用法）仍然折算——这是种子路径唯一的入口。"""
    o = mk_order(client, items=[{"name": "a", "quantity": 2, "price_cny": "10.00"}])
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "price_cny": "60.00",
        "items": [{"name": "a", "quantity": 2}]}).json()
    assert Decimal(body["items"][0]["price_cny"]) == Decimal("30.00")
    assert Decimal(body["price_cny"]) == Decimal("60.00")


def test_postage_only_change_adds_on_top(client):
    """只改邮费（不动物品）时：货款不变、总价 = 货款 + 新邮费。这才是前端的真实路径。"""
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "price_cny": "100.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "postage_cny": "20.00"})
    assert Decimal(r.json()["price_cny"]) == Decimal("120.00")


def test_clearing_postage_means_free_shipping(client):
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "price_cny": "100.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "postage_cny": None})
    assert Decimal(r.json()["price_cny"]) == Decimal("100.00")


def test_patch_price_alone_is_ignored_price_is_derived(client):
    """price_cny 是派生列：单发它不会改价（前端 OCR 合并曾踩过这个坑）。"""
    o = mk_order(client, shop="某商品")             # 无价 → 自动占位物品，价 0
    assert Decimal(o["price_cny"]) == Decimal("0.00")
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "price_cny": "188.00"})
    assert Decimal(r.json()["price_cny"]) == Decimal("0.00")


def test_patch_price_with_unpriced_items_reprices(client):
    """正确的补价姿势：成交价当种子 + 一份不带单价的物品 → 后端按建单同一套规则折成单价。
    这正是 Orders 页 OCR「按订单号合并」采用的写法。"""
    o = mk_order(client, shop="某商品")
    r = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "price_cny": "188.00",
        "items": [{"name": "某商品", "quantity": 1, "price_cny": None, "auto": True}]})
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["price_cny"]) == Decimal("188.00")
    assert Decimal(r.json()["items"][0]["price_cny"]) == Decimal("188.00")


# --- 唯一约束在 PATCH 上同样生效 ---------------------------------------------

def test_patch_order_no_into_duplicate_conflicts(client):
    mk_order(client, order_no="DUP-A", platform="淘宝")
    b = mk_order(client, order_no="DUP-B", platform="淘宝")
    r = client.patch(f"/api/orders/{b['id']}", json={"version": b["version"], "order_no": "DUP-A"})
    assert r.status_code == 409


def test_patch_platform_into_duplicate_conflicts(client):
    mk_order(client, order_no="DUP-C", platform="淘宝")
    b = mk_order(client, order_no="DUP-C", platform="闲鱼")
    r = client.patch(f"/api/orders/{b['id']}", json={"version": b["version"], "platform": "淘宝"})
    assert r.status_code == 409


# --- 汇率 -------------------------------------------------------------------

def test_create_fills_todays_rate(client, fx_today):
    o = mk_order(client, price_cny="10.00")
    assert o["fx_rate"] is not None
    assert o["jpy_settled"] is not None


def test_explicit_rate_wins_over_auto_fill(client, fx_today):
    o = mk_order(client, price_cny="10.00", fx_rate="30")
    assert Decimal(o["fx_rate"]) == Decimal("30.0000")
    assert o["jpy_settled"] == 300


def test_rate_for_date_prefers_that_days_rate(client, session):
    import datetime as dt

    from sqlmodel import select

    from app.models import FxRate
    from app.services.fx import rate_for_date

    d = dt.date(2020, 5, 5)
    if session.exec(select(FxRate).where(FxRate.date == d)).first() is None:
        session.add(FxRate(date=d, rate=Decimal("11.1111")))
        session.commit()
    assert rate_for_date(session, d) == Decimal("11.1111")


def test_rate_for_date_falls_back_to_latest(client, session, fx_today):
    import datetime as dt

    from app.services.fx import current_rate, rate_for_date
    assert rate_for_date(session, dt.date(1999, 1, 1)) == current_rate(session)


def test_import_uses_staging_rate(client):
    s = client.post("/api/staging", json={"order_no": "FX-IMP", "fx_rate": "25",
                                          "items": [{"name": "a", "quantity": 1, "price_cny": "10"}]}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    assert Decimal(o["fx_rate"]) == Decimal("25.0000")
    assert o["jpy_settled"] == 250


# --- 暂存 ↔ 账本的联动边界 ----------------------------------------------------

def test_write_through_fails_when_linked_order_deleted(client):
    """账本单被删后，暂存行的 imported 指针已被清空 → 该行退回「编辑自身副本」，不再写穿。"""
    s = client.post("/api/staging", json={"order_no": "WT-DEL", "shop": "原"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.delete(f"/api/orders/{o['id']}")
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}", json={"version": row["version"], "shop": "改后"})
    assert r.status_code == 200 and r.json()["shop"] == "改后"


def test_staging_items_replaced_wholesale(client):
    s = client.post("/api/staging", json={"order_no": "ST-REP", "items": [
        {"name": "a", "quantity": 1, "price_cny": "1"},
        {"name": "b", "quantity": 1, "price_cny": "2"}]}).json()
    r = client.patch(f"/api/staging/{s['id']}", json={
        "version": s["version"], "items": [{"name": "c", "quantity": 1, "price_cny": "5"}]})
    assert [i["name"] for i in r.json()["items"]] == ["c"]
    assert Decimal(r.json()["price_cny"]) == Decimal("5.00")


def test_staging_empty_items_gets_placeholder(client):
    s = client.post("/api/staging", json={"order_no": "ST-EMPTY", "shop": "店名"}).json()
    assert len(s["items"]) == 1 and s["items"][0]["name"] == "店名"


# --- 分页 -------------------------------------------------------------------

def test_offset_pagination_does_not_repeat_rows(client):
    for i in range(5):
        mk_order(client, order_no=f"PG-{i}", platform="淘宝", date="2027-03-01")
    p1 = client.get("/api/orders", params={"limit": 2, "offset": 0, "q": "PG-"}).json()
    p2 = client.get("/api/orders", params={"limit": 2, "offset": 2, "q": "PG-"}).json()
    assert p1["total"] == p2["total"] >= 5
    assert not ({r["id"] for r in p1["items"]} & {r["id"] for r in p2["items"]})


def test_offset_past_end_returns_empty(client):
    r = client.get("/api/orders", params={"limit": 10, "offset": 100000}).json()
    assert r["items"] == [] and r["total"] >= 0


# --- 健康检查 / 未知路由 ------------------------------------------------------

def test_health_is_public(anon):
    r = anon.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_unknown_api_route_404(client):
    assert client.get("/api/definitely-not-a-route").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("put", "/api/orders"), ("post", "/api/orders/1"), ("delete", "/api/dashboard"),
])
def test_wrong_method_405(client, method, path):
    assert getattr(client, method)(path).status_code in (404, 405)
