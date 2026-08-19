"""集运订单：挂靠/解除的原子性、软删联动、特殊费、金额。"""
import uuid
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
    o = mk_order(client, purchase_status="待收货")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["purchase_status"] == "待收货"


def test_attach_inherits_status_and_detach_restores(client):
    """订单只记国内段；挂上集运单后**显示**跟随那张单，释放后回落到自己的状态。

    关键是挂靠期间 `status` 必须原样保留——曾经自动挂靠会把「集运中」写进 status，
    那样一释放，回落到的就是被覆盖过的值，而不是真实的国内段状态。"""
    s = mk_ship(client, shipment_no="JF-ST2")          # 新建集运单默认「打包中」
    o = mk_order(client, purchase_status="已签收")

    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["purchase_status"] == "已签收", "挂靠不该动订单自己的国内段状态"
    assert got["fulfillment_status"] == "打包中", "挂靠后显示的应是集运单的状态"

    client.delete(f"/api/shipment/{s['id']}/order/{o['id']}")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["purchase_status"] == got["fulfillment_status"] == "已签收", "释放后应回落到自身状态"


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
    """非法集运状态必须被**白名单校验器**拒掉，而不是被别的什么东西顺手拒掉。

    只断言 422 是不够的：写模型是 `extra="forbid"`，任何写错的键同样返回 422，
    于是改名之后这条测试会拿着 `extra_forbidden` 继续绿，而它本该守的枚举校验器
    早已无人过问（本条就这么失守过一轮）。断言拒绝的**理由**，让它改名即红。
    """
    r = client.post("/api/shipment", json={"date": "2026-06-01", "shipment_status": "无此状态"})
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["type"] == "value_error" and err["loc"][-1] == "shipment_status", \
        f"422 来自旁路而非状态白名单校验器：{err}"


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
    o = mk_order(client, purchase_status="已签收")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.patch(f"/api/shipment/{s['id']}", json={
        "version": client.get(f"/api/shipment/{s['id']}").json()["version"], "shipment_status": "已发出"})

    hit = client.get("/api/orders", params={"fulfillment_status": "已发出", "limit": 200}).json()["items"]
    assert any(x["id"] == o["id"] for x in hit), "按继承来的集运状态筛不到该订单"

    # 两个筛选参数现在语义分明，各自都要对：
    # `fulfillment_status` = 界面显示的那个（挂靠期间是集运单的），所以按「已签收」筛不到它；
    # `purchase_status`    = 订单**自身**的采购段状态，挂靠期间原样保留，所以能筛到。
    # 原先只有一个叫 `status` 的参数，它比的却是 coalesce 出来的值——名字没说清筛的是哪个。
    miss = client.get("/api/orders", params={"fulfillment_status": "已签收", "limit": 200}).json()["items"]
    assert all(x["id"] != o["id"] for x in miss), "挂靠中的订单显示的是集运状态，不该被「已签收」筛出来"
    own = client.get("/api/orders", params={"purchase_status": "已签收", "limit": 200}).json()["items"]
    assert any(x["id"] == o["id"] for x in own), "订单自身的国内段状态在挂靠期间必须原样保留、且可单独筛"


def test_fulfillment_status_falls_back_when_shipment_soft_deleted(client):
    """集运单被软删后界面上已经看不见它了，再拿它的状态显示就是个查无此处的幽灵值。"""
    s = mk_ship(client, shipment_no="JF-SD1")
    o = mk_order(client, purchase_status="已签收")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    client.delete(f"/api/shipment/{s['id']}")
    assert client.get(f"/api/orders/{o['id']}").json()["fulfillment_status"] == "已签收"


def test_unattached_order_fulfillment_status_equals_own(client):
    o = mk_order(client, purchase_status="待收货")
    got = client.get(f"/api/orders/{o['id']}").json()
    assert got["purchase_status"] == got["fulfillment_status"] == "待收货"


def test_ocr_auto_attach_does_not_write_status(client, mk, monkeypatch):
    """自动挂靠（「内含快递」截图）曾把「集运中」写进订单状态——那会污染国内段状态，
    一旦释放，回落到的是被覆盖过的值。现在两条挂靠路径都只写外键、不动状态。

    **这条原先是假绿**：它 grep 源码里有没有 `'"status"'`（带引号）。
    列名改成 `purchase_status` 之后，`"purchase_status"` 里根本不含 `"status"` 这个带引号的串，
    断言永远不可能触发。改成实际跑一次挂靠、比对状态。
    """
    from app.routers import shipment as mod

    j = mk("/api/shipment", {"date": "2028-03-01", "recipient": "OCR状态"})
    o = mk("/api/orders", {"date": "2028-03-01", "title": "被挂的单",
                           "express_no": "SF9001234567", "price_cny": 10,
                           "purchase_status": "已签收"})

    async def fake_ocr(file, fn):
        return {"express_nos": ["SF9001234567"]}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    r = client.post(f"/api/shipment/{j['id']}/ocr-express",
                    files={"file": ("x.png", b"not-a-real-png", "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["attached"], "没挂上，下面的断言测不出东西"

    got = next(x for x in client.get("/api/orders", params={"limit": 200}).json()["items"]
               if x["id"] == o["id"])
    assert got["shipment_order_id"] == j["id"], "外键没写上"
    assert got["purchase_status"] == "已签收", \
        f"自动挂靠动了国内段状态：已签收 → {got['purchase_status']}"


def test_ocr_auto_attach_is_idempotent_for_the_optimistic_lock_too(client, mk, monkeypatch):
    """同一张截图重传，**已经挂在本单上的订单一个字节都不写**。

    原先 WHERE 里放行了「已挂本单」以求幂等，但 SET 里带着 `version + 1`——
    于是幂等只对挂靠关系成立、对乐观锁不成立：重传一次，这些订单的 version 就 +1，
    正在编辑其中某单的人下一次保存直接 409，而他什么都没做错。
    """
    from app.routers import shipment as mod

    j = mk("/api/shipment", {"date": "2028-03-02", "recipient": "重传"})
    o = mk("/api/orders", {"date": "2028-03-02", "title": "重传的单",
                           "express_no": "SF9007654321", "price_cny": 10})

    async def fake_ocr(file, fn):
        return {"express_nos": ["SF9007654321"]}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    url = f"/api/shipment/{j['id']}/ocr-express"
    files = {"file": ("x.png", b"not-a-real-png", "image/png")}
    assert client.post(url, files=files).status_code == 200

    def ver():
        return next(x for x in client.get("/api/orders", params={"limit": 200}).json()["items"]
                    if x["id"] == o["id"])["version"]

    v1 = ver()
    assert client.post(url, files={"file": ("x.png", b"not-a-real-png", "image/png")}).status_code == 200
    assert ver() == v1, "重传同一张截图把 version 顶高了，正在编辑这单的人会莫名 409"


def test_order_status_enum_is_domestic_only():
    from app.models import PurchaseStatus

    assert {s.value for s in PurchaseStatus} == {
        "待付款", "待发货", "待收货", "已签收", "退款", "交易关闭"}


def test_short_tracking_number_is_never_auto_attached(client, session, monkeypatch):
    """短到不像快递单号的，绝不拿去自动挂靠。

    OCR 会把一个长号断成两截（`SF1234 56789012` → 取到后半截）。ocr.py 里已有一道
    「旁边紧邻另一段数字」的判据，但它依赖排版，挡不住所有情形——而**不可逆后果**在这一端：
    半截号拿去 `Order.express_no == no` 精确匹配，匹配不上只是漏一单，
    万一撞上别人的单号，就是把货自动挂到一张无关订单上。
    """
    from app.routers import shipment as mod

    ship = client.post("/api/shipment", json={
        "date": "2026-03-01", "shipment_no": "SHORT-GUARD-1"}).json()
    # 造一张订单，它的快递号**恰好**等于那截半截号——正是最坏的那种巧合
    od = client.post("/api/orders", json={
        "date": "2026-03-01", "title": "无关订单", "express_no": "56789012"}).json()

    async def fake_ocr(file, recognizer):
        return {"express_nos": ["56789012"], "unreadable": 0}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    r = client.post(f"/api/shipment/{ship['id']}/ocr-express",
                    files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200, r.text
    got = r.json()
    assert [o["id"] for o in got["attached"]] == [], "半截号把货挂到了无关订单上"
    assert "56789012" in got["unmatched"], "被静默丢弃了——用户看不到、也没法手动挂"
    assert client.get(f"/api/orders/{od['id']}").json()["shipment_order_id"] is None


def test_full_length_tracking_number_still_attaches(client, session, monkeypatch):
    """反面：正常长度的单号必须照常挂上去，否则这道闸就是把功能关掉了。"""
    from app.routers import shipment as mod

    ship = client.post("/api/shipment", json={
        "date": "2026-03-01", "shipment_no": "SHORT-GUARD-2"}).json()
    od = client.post("/api/orders", json={
        "date": "2026-03-01", "title": "该挂上的订单", "express_no": "SF1234567890123"}).json()

    async def fake_ocr(file, recognizer):
        return {"express_nos": ["SF1234567890123"], "unreadable": 0}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    got = client.post(f"/api/shipment/{ship['id']}/ocr-express",
                      files={"file": ("x.png", b"\x89PNG", "image/png")}).json()
    assert [o["id"] for o in got["attached"]] == [od["id"]], "正常单号也挂不上了"


def test_ocr_attach_reports_orders_already_on_another_shipment(client, session, monkeypatch):
    """「已经挂在别张集运单上的订单，不许被抢走、且必须回报给用户」——`skipped` 桶。

    这条规则**全仓零断言**：`skipped` 这个键在 tests 里只出现在插件那两处
    （按账号清空暂存的计数、插件卡片计数），与集运挂靠无关。
    改坏了不会红——把这些订单塞进 `attached` 而不是 `skipped`，
    前端只会照数量弹一句绿色的「已关联 N 单」，而那 N 单里有几单一个字节都没写。
    对照：同一条语义在 `attach_order` 那个入口有 `test_attach_already_attached_elsewhere_rejected`
    钉着——两个入口，只有一半有守卫。
    """
    from app.routers import shipment as mod

    # **单号要自足。** 用 `SF1234567890` 这种「大家都在用」的号会让整套跑时
    # 匹配到别的用例建的订单（那条没挂集运单 ⇒ 会被正常挂上 ⇒ attached 非空），
    # 于是这条测试单跑绿、整套红。随机后缀让它只匹配自己造的那一行。
    no = "SFSKIP" + uuid.uuid4().hex[:10].upper()
    a = client.post("/api/shipment", json={"date": "2026-03-02", "shipment_no": "SKIP-A"}).json()
    b = client.post("/api/shipment", json={"date": "2026-03-02", "shipment_no": "SKIP-B"}).json()
    od = client.post("/api/orders", json={
        "date": "2026-03-02", "title": "已挂在 A 上", "express_no": no,
        "shipment_order_id": a["id"]}).json()
    assert client.get(f"/api/orders/{od['id']}").json()["shipment_order_id"] == a["id"]

    async def fake_ocr(file, recognizer):
        return {"express_nos": [no], "unreadable": 0}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    r = client.post(f"/api/shipment/{b['id']}/ocr-express",
                    files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200, r.text
    got = r.json()
    assert [o["id"] for o in got["attached"]] == [], "把别张集运单的货抢过来了"
    assert od["id"] in [o["id"] for o in got["skipped"]], (
        f"没回报给用户：skipped={got['skipped']}，用户只会看到一句绿色的「已关联 0 单」")
    # 库里没被动
    assert client.get(f"/api/orders/{od['id']}").json()["shipment_order_id"] == a["id"]
