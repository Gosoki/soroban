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
    assert Decimal(o["items"][0]["unit_price_cny"]) == Decimal("10.00")
    assert Decimal(o["items"][1]["unit_price_cny"]) == Decimal("0.00")
    assert Decimal(o["price_cny"]) == Decimal("30.00")
    assert all(i["auto"] for i in o["items"])          # 全部标 auto，待人工拆分


def test_seed_split_is_exact_even_when_quantity_does_not_divide(client):
    """数量除不尽时：单价**向下**取整到分，余数单独成一条「金额尾差」，总价分毫不差。

    旧行为是 HALF_UP 取整、总价差几分，还被当成「已知行为」钉在这条测试里。它不是可接受的
    近似——订单价是 Σ(单价×数量) 派生的，单价一被舍入，账本金额就和爬虫抓到的实付金额对不上。
    量级也不止「几分」：ROUND_HALF_UP 在 ¥5.00/1000 件时会把单价舍成 0.01 → 总价 ¥10.00
    （**翻一倍**），¥0.40/1000 件时舍成 0.00 → 总价 **¥0**。见 test_seed_split_never_inflates。
    """
    o = mk_order(client, price_cny="10.00", items=[{"name": "a", "quantity": 3}])
    assert Decimal(o["items"][0]["unit_price_cny"]) == Decimal("3.33")
    assert o["items"][-1]["name"].endswith("（金额尾差）")
    assert Decimal(o["items"][-1]["unit_price_cny"]) == Decimal("0.01")
    assert Decimal(o["price_cny"]) == Decimal("10.00")


@pytest.mark.parametrize("price,qty", [("5.00", 1000), ("0.40", 1000), ("100.00", 7), ("0.01", 3)])
def test_seed_split_never_inflates_or_zeroes_the_order(client, price, qty):
    """**总价守恒**是硬约束：无论数量多大、除得尽除不尽，Σ(单价×数量) 必须等于种子价。

    这几组是旧的 HALF_UP 实现会崩掉的地方——5.00/1000 曾变成 10.00，0.40/1000 曾变成 0.00。
    """
    o = mk_order(client, price_cny=price, items=[{"name": "a", "quantity": qty}])
    assert Decimal(o["price_cny"]) == Decimal(price)
    recomputed = sum((Decimal(i["unit_price_cny"]) * i["quantity"] for i in o["items"]), Decimal("0"))
    assert recomputed == Decimal(price)


def test_partially_priced_items_zero_the_rest(client):
    """只要有**任一**物品带价，就按原样采用；无价的记 0 并标 auto（灰显=待补价）。"""
    o = mk_order(client, price_cny="99.00",
                 items=[{"name": "a", "quantity": 1, "unit_price_cny": "5.00"}, {"name": "b", "quantity": 1}])
    assert Decimal(o["items"][0]["unit_price_cny"]) == Decimal("5.00")
    assert Decimal(o["items"][1]["unit_price_cny"]) == Decimal("0.00")
    assert o["items"][0]["auto"] is False and o["items"][1]["auto"] is True
    assert Decimal(o["price_cny"]) == Decimal("5.00")   # 种子价被物品明细覆盖


def test_clearing_all_prices_zeroes_them(client):
    """清空全部单价 = 「单价未知」，一律记 0 + auto（灰显待补价）——与「只清空部分单价」同口径。
    （旧行为是把旧总价整个折到第一条上，同一个动作两种结果，已修。）"""
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "unit_price_cny": "20.00"}])
    assert Decimal(o["price_cny"]) == Decimal("30.00")
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "a", "quantity": 1}, {"name": "b", "quantity": 1}]}).json()
    assert [Decimal(i["unit_price_cny"]) for i in body["items"]] == [Decimal("0.00"), Decimal("0.00")]
    assert all(i["auto"] for i in body["items"])
    assert Decimal(body["price_cny"]) == Decimal("0.00")


def test_clearing_one_price_zeroes_only_that_one(client):
    """对照组：只清空一条单价时的行为——两者必须一致，否则就是「同一动作两种结果」。"""
    o = mk_order(client, items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"},
                                {"name": "b", "quantity": 1, "unit_price_cny": "20.00"}])
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10.00"},
                  {"name": "b", "quantity": 1}]}).json()
    assert [Decimal(i["unit_price_cny"]) for i in body["items"]] == [Decimal("10.00"), Decimal("0.00")]


def test_postage_change_with_unpriced_items_does_not_rebase_goods(client):
    """同一次 PATCH 既改邮费又送「无单价」物品：无单价就是无单价，记 0；
    不再拿「旧总价 − 新邮费」倒推货款（那会让总价看着没变、货款却悄悄改了）。"""
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "unit_price_cny": "100.00"}])
    assert Decimal(o["price_cny"]) == Decimal("110.00")
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "postage_cny": "20.00",
        "items": [{"name": "a", "quantity": 1}]}).json()
    assert Decimal(body["items"][0]["unit_price_cny"]) == Decimal("0.00")
    assert Decimal(body["price_cny"]) == Decimal("20.00")     # 只剩邮费


def test_explicit_seed_price_still_splits(client):
    """显式给种子价（爬虫/OCR 的用法）仍然折算——这是种子路径唯一的入口。"""
    o = mk_order(client, items=[{"name": "a", "quantity": 2, "unit_price_cny": "10.00"}])
    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "price_cny": "60.00",
        "items": [{"name": "a", "quantity": 2}]}).json()
    assert Decimal(body["items"][0]["unit_price_cny"]) == Decimal("30.00")
    assert Decimal(body["price_cny"]) == Decimal("60.00")


def test_postage_only_change_adds_on_top(client):
    """只改邮费（不动物品）时：货款不变、总价 = 货款 + 新邮费。这才是前端的真实路径。"""
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "unit_price_cny": "100.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "postage_cny": "20.00"})
    assert Decimal(r.json()["price_cny"]) == Decimal("120.00")


def test_clearing_postage_means_free_shipping(client):
    o = mk_order(client, postage_cny="10.00",
                 items=[{"name": "a", "quantity": 1, "unit_price_cny": "100.00"}])
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "postage_cny": None})
    assert Decimal(r.json()["price_cny"]) == Decimal("100.00")


def test_patch_price_alone_is_ignored_price_is_derived(client):
    """price_cny 是派生列：单发它不会改价（前端 OCR 合并曾踩过这个坑）。"""
    o = mk_order(client, title="某商品")             # 无价 → 自动占位物品，价 0
    assert Decimal(o["price_cny"]) == Decimal("0.00")
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "price_cny": "188.00"})
    assert Decimal(r.json()["price_cny"]) == Decimal("0.00")


def test_patch_price_with_unpriced_items_reprices(client):
    """正确的补价姿势：成交价当种子 + 一份不带单价的物品 → 后端按建单同一套规则折成单价。
    这正是 Orders 页 OCR「按订单号合并」采用的写法。"""
    o = mk_order(client, title="某商品")
    r = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"], "price_cny": "188.00",
        "items": [{"name": "某商品", "quantity": 1, "unit_price_cny": None, "auto": True}]})
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["price_cny"]) == Decimal("188.00")
    assert Decimal(r.json()["items"][0]["unit_price_cny"]) == Decimal("188.00")


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
                                          "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    assert Decimal(o["fx_rate"]) == Decimal("25.0000")
    assert o["jpy_settled"] == 250


# --- 暂存 ↔ 账本的联动边界 ----------------------------------------------------

def test_write_through_fails_when_linked_order_deleted(client):
    """账本单被删后，暂存行的 imported 指针已被清空 → 该行退回「编辑自身副本」，不再写穿。"""
    s = client.post("/api/staging", json={"order_no": "WT-DEL", "title": "原"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.delete(f"/api/orders/{o['id']}")
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}", json={"version": row["version"], "title": "改后"})
    assert r.status_code == 200 and r.json()["title"] == "改后"


def test_staging_items_replaced_wholesale(client):
    s = client.post("/api/staging", json={"order_no": "ST-REP", "items": [
        {"name": "a", "quantity": 1, "unit_price_cny": "1"},
        {"name": "b", "quantity": 1, "unit_price_cny": "2"}]}).json()
    r = client.patch(f"/api/staging/{s['id']}", json={
        "version": s["version"], "items": [{"name": "c", "quantity": 1, "unit_price_cny": "5"}]})
    assert [i["name"] for i in r.json()["items"]] == ["c"]
    assert Decimal(r.json()["price_cny"]) == Decimal("5.00")


def test_staging_empty_items_gets_placeholder(client):
    s = client.post("/api/staging", json={"order_no": "ST-EMPTY", "title": "店名"}).json()
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


def test_residual_row_does_not_pile_up_on_every_edit(client):
    """「金额尾差」行**不能**每改一次单就多一条。

    它是派生产物，而前端保存时会把服务端返回的 items 原样回传（物品编辑器、
    订单页展开面板都这样）。`build_items` 若不认识自己上一次生成的那行，
    就会把它当成「一条没有单价的物品」重新折算，**再补一条新的**。
    实测过：同一张单连改 3 次，物品从 2 条涨到 5 条（总价始终守恒，坏的是条数）。
    """
    o = mk_order(client, price_cny="10.00", items=[{"name": "a", "quantity": 3}])
    assert len(o["items"]) == 2 and o["items"][-1]["name"].endswith("（金额尾差）")

    for _ in range(3):
        body = {"version": o["version"], "price_cny": "10.00",
                "items": [{"name": i["name"], "quantity": i["quantity"],
                           "unit_price_cny": None, "auto": True} for i in o["items"]]}
        r = client.patch(f"/api/orders/{o['id']}", json=body)
        assert r.status_code == 200, r.text
        o = r.json()
        names = [i["name"] for i in o["items"]]
        assert names.count("a（金额尾差）") == 1, f"尾差行叠加了：{names}"
        assert Decimal(o["price_cny"]) == Decimal("10.00")
    assert len(o["items"]) == 2


def test_resaving_the_items_verbatim_does_not_shrink_the_order(client):
    """**原样存一次，订单价不许少一分钱。**

    上面那三条尾差测试全都把 `unit_price_cny` 置成 None 再回传，走的是「重新折算」
    那一支——而前端**从不这么发**：`OrderItemsEditor.toPayload` 是带着单价整体回传的，
    并且 `saveItems` 只发 `{version, items}`、不发 `price_cny`。于是走的是「带价原样采用」
    那一支，而剔尾差行原先放在所有分支之前 ⇒ 尾差被删掉、那笔钱没有任何地方加回去：

        建单 ¥100.00 / A×3 → [A@33.33, A（金额尾差）@0.01]
        在物品编辑器里随便改一格（改名、改数量、加一条）→ 触发整体保存 → 99.99

    200 OK、无日志、不可逆。误差上限是 数量×0.01（数量 1000 时是 9.99 元），
    而账本金额与爬虫抓到的实付金额从此对不上。
    """
    o = mk_order(client, price_cny="100.00", items=[{"name": "a", "quantity": 3}])
    assert Decimal(o["price_cny"]) == Decimal("100.00")
    assert any(i["name"].endswith("（金额尾差）") for i in o["items"]), "夹具没造出尾差行"

    # 逐字模拟 toPayload：带单价、带 auto，且**不发 price_cny**
    body = {"version": o["version"],
            "items": [{"name": i["name"], "quantity": i["quantity"],
                       "unit_price_cny": i["unit_price_cny"], "auto": i["auto"]}
                      for i in o["items"]]}
    r = client.patch(f"/api/orders/{o['id']}", json=body)
    assert r.status_code == 200, r.text
    o2 = r.json()
    assert Decimal(o2["price_cny"]) == Decimal("100.00"), \
        f"原样存一次就少了钱：{o['price_cny']} → {o2['price_cny']}"

    # **反面**：这一支不许把尾差行当成「要重算的输入」——它带着真实单价，
    # 留着才守恒；同时也不许因此就在这一支里再生成一条新的。
    assert [i["name"] for i in o2["items"]].count("a（金额尾差）") == 1

    # 再存一次仍然守恒（一次性丢钱与持续丢钱都要挡住）
    body2 = {"version": o2["version"],
             "items": [{"name": i["name"], "quantity": i["quantity"],
                        "unit_price_cny": i["unit_price_cny"], "auto": i["auto"]}
                       for i in o2["items"]]}
    o3 = client.patch(f"/api/orders/{o2['id']}", json=body2).json()
    assert Decimal(o3["price_cny"]) == Decimal("100.00")


def test_residual_row_is_recognised_even_with_a_very_long_item_name(client):
    """长物品名下，后缀必须仍在——否则下一次认不出它，又开始叠加。

    直接 `f"{name}（金额尾差）"[:255]` 会在名字接近 255 时把后缀截掉。
    """
    long_name = "长" * 250
    o = mk_order(client, price_cny="10.00", items=[{"name": long_name, "quantity": 3}])
    resid = o["items"][-1]["name"]
    assert resid.endswith("（金额尾差）"), f"后缀被截掉了：…{resid[-12:]}"

    body = {"version": o["version"], "price_cny": "10.00",
            "items": [{"name": i["name"], "quantity": i["quantity"],
                       "unit_price_cny": None, "auto": True} for i in o["items"]]}
    o2 = client.patch(f"/api/orders/{o['id']}", json=body).json()
    assert len([i for i in o2["items"] if i["name"].endswith("（金额尾差）")]) == 1


def test_user_named_item_ending_in_the_suffix_is_not_eaten(client):
    """用户自己起名叫「…（金额尾差）」的**真**物品不该被剔掉。

    判据要求三个条件同时成立（后缀 + auto=True + 数量 1）；用户手输的行 auto=False。
    """
    o = mk_order(client, items=[
        {"name": "手写（金额尾差）", "quantity": 1, "unit_price_cny": "7.00", "auto": False},
    ])
    assert [i["name"] for i in o["items"]] == ["手写（金额尾差）"]
    assert Decimal(o["price_cny"]) == Decimal("7.00")


@pytest.mark.parametrize("path,extra", [
    ("orders", {"date": "2026-08-18", "platform": "闲鱼"}),
    ("staging", {"platform": "闲鱼"}),
])
def test_postage_over_total_is_refused_on_patch_too(client, path, extra):
    """「邮费不能大于订单价」这条闸**改单时也得成立**，不能只挂在建单上。

    原先它只在 `OrderCreate` / `StagingCreate` 的 model_validator 上，于是同一份 body：
      · POST → 422（对）
      · PATCH → **200 OK**，而且悄悄把一张原价 200 的单改成 **100（就是邮费）**、
        物品单价 0.00。

    路径：`goods_seed(10, 100) = -90` → `build_items` 的 `if seed_goods < 0: seed_goods = 0`
    → 物品全记 0.00 → `sync_from_items` 得出「订单价 = 0 + 邮费」。
    **全程没有任何提示**，而这正是建单那条闸的注释里写着要拒绝的后果。

    生产者是真的：淘宝插件在「单价全解析失败」的降级分支下推
    `price_cny=实付` + 全部 `unit_price_cny=None`，未导入的暂存行走整体更新——
    只要实付 < 解析出的邮费（运费券、红包、部分退款），同一批抓取里
    **新单 422 进 failed 桶、老单被静默改成「订单价 = 邮费」**。

    闸下沉到 `goods_seed`（七个调用点唯一交汇处，两个值必然同时在手），
    所以建单/改单/导入/暂存四条路径一次覆盖。
    """
    body = dict(extra, order_no=f"POSTAGE-{path}",
                items=[{"name": "A", "quantity": 1, "unit_price_cny": "200"}])
    row = client.post(f"/api/{path}", json=body).json()
    assert row["price_cny"] == "200.00", row

    bad = {"price_cny": "10", "postage_cny": "100",
           "items": [{"name": "A", "quantity": 1}], "version": row["version"]}
    r = client.patch(f"/api/{path}/{row['id']}", json=bad)
    assert r.status_code == 422, \
        f"改单放行了邮费>总价：{r.status_code} → 价变成 {r.json().get('price_cny')}"
    assert "邮费" in r.text, r.text

    # 原来那张单一个字节都不许动
    after = client.get(f"/api/{path}?order_no=POSTAGE-{path}").json()["items"][0]
    assert after["price_cny"] == "200.00", f"被拒之后订单价还是被改了：{after}"


def test_postage_equal_to_total_is_still_allowed(client):
    """反面：邮费**等于**总价是合法的（纯运费单，货款为 0）。

    没有这一条，把判据写成 `>=` 也能让上面那条绿——而那会把「只付了运费」这种
    真实存在的单挡在门外。
    """
    r = client.post("/api/orders", json={
        "date": "2026-08-18", "order_no": "POSTAGE-EQ", "platform": "闲鱼",
        "price_cny": "100", "postage_cny": "100", "items": [{"name": "运费", "quantity": 1}]})
    assert r.status_code == 200, r.text
    assert r.json()["price_cny"] == "100.00", r.json()
