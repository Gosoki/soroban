"""集运单「显示」与「选择」是两件事——这里钉住它们的分工。

下拉选项有条数上限（200，否则 DOM 会炸：曾实测 nodes=46312、longtask 22–33s）。
上限本身没问题，问题是**显示**曾经也依赖它：挂在第 200 名之外的订单显示成 `#101`
这种查无此单的内部 id，且在编辑面板里选框直接空白——看起来像「这单没挂集运」。

分工：
  - 显示：真相在订单行自带的 `shipment_no` 上，与下拉里有没有这一张无关；
  - 选择：下拉给最近 200 张 + 按单号远程搜索，覆盖任意早的单。
"""
import re
from pathlib import Path

from tests.test_queries import count_queries

_REPO = Path(__file__).resolve().parents[2]


# --- 一、订单行自带集运单号 ---------------------------------------------------

def _mk(client, order_no, ship_no):
    """建一张挂靠好的订单。**order_no 必须逐用例唯一**——client 夹具在模块内共享库，
    撞了唯一索引后建单会被拒，断言就会悄悄查到上一个用例留下的行（本文件已经栽过一次）。"""
    s = client.post("/api/shipment", json={"date": "2026-12-01", "shipment_no": ship_no}).json()
    r = client.post("/api/orders", json={
        "date": "2026-12-01", "order_no": order_no, "platform": "淘宝",
        "shipment_order_id": s["id"],
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]})
    assert r.status_code == 200, f"建单失败（多半是 order_no 撞了）：{r.status_code} {r.text}"
    return s, r.json()


def test_order_carries_shipment_no(client):
    s, o = _mk(client, "PICK-CARRY", "SH-CARRY")
    assert o["shipment_no"] == "SH-CARRY"
    assert o["shipment_order_id"] == s["id"]


def test_unattached_order_has_no_shipment_no(client):
    o = client.post("/api/orders", json={
        "date": "2026-12-01", "order_no": "PICK-2", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]}).json()
    assert o["shipment_no"] is None


def test_soft_deleted_shipment_falls_back_to_none(client, session):
    """与 effective_status 同一条软删判断：那张单界面上已经没了，
    再显示它的单号会是个查无此处的幽灵值。

    ⚠️ 这个状态**走 API 造不出来**：`DELETE /api/shipment/{id}` 会先把子订单全部释放
    （`shipment_order_id=None`），走到的是「没挂靠」而不是「挂着一张已删的」。
    照 API 写这条测试会假绿——把守卫拆掉它照样过（本文件第一版就是这样）。
    所以这里直接在库里造出那个状态：迁移脚本、以及「删单」与「挂靠」之间的并发窗口
    都可能留下它，守卫正是为它而设。
    """
    from app.models import Order, ShipmentOrder

    s, o = _mk(client, "PICK-GHOST", "SH-GHOST")
    ship = session.get(ShipmentOrder, s["id"])
    ship.is_delete = True                      # 只删单，**不**释放子订单
    session.add(ship)
    session.commit()

    row = session.get(Order, o["id"])
    assert row.shipment_order_id == s["id"], "前提没造出来：订单已经不挂在这张单上了"
    assert row.shipment_no is None
    assert row.fulfillment_status == row.purchase_status  # 两者必须一起回落，否则半挂半不挂


def test_shipment_no_costs_no_extra_query(client):
    """它复用 effective_status 已经要求的 selectinload(Order.shipment_order)，
    不该因此多发查询——否则 200 行订单页就是 200 次懒加载。"""
    for i in range(8):
        s = client.post("/api/shipment", json={"date": "2026-12-02", "shipment_no": f"SN-{i}"}).json()
        client.post("/api/orders", json={
            "date": "2026-12-02", "order_no": f"PQ-{i}", "platform": "淘宝",
            "shipment_order_id": s["id"],
            "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]})
    with count_queries() as a:
        client.get("/api/orders", params={"limit": 1})
    with count_queries() as b:
        client.get("/api/orders", params={"limit": 100})
    assert b["n"] <= a["n"] + 2, (
        f"取 shipment_no 引入了随行数增长的查询（1 行 {a['n']} → 100 行 {b['n']}）\n"
        + "\n".join(b["sql"])
    )


# --- 二、按单号搜索能翻出上限之外的单 -----------------------------------------

def test_search_reaches_beyond_the_dropdown_cap(client):
    """这条是**上限之外仍可选中**的行为证明，不是对 q 参数的形式检查。

    造 205 张集运单（超过前端 limit=200），最早那张必然落在默认那批之外；
    按它的单号搜必须能搜到——否则挂在它上面的订单就再也换不了集运单。
    """
    for i in range(205):
        client.post("/api/shipment", json={"date": "2026-12-03", "shipment_no": f"OLD-{i:04d}"})
    default_batch = client.get("/api/shipment", params={"limit": 200, "brief": True}).json()["items"]
    assert len(default_batch) == 200
    nos = {j["shipment_no"] for j in default_batch}
    missing = [f"OLD-{i:04d}" for i in range(205) if f"OLD-{i:04d}" not in nos]
    assert missing, "造数没能超出上限，这条测试就没在测东西"

    hit = client.get("/api/shipment", params={"q": missing[0], "limit": 50, "brief": True}).json()
    assert [j["shipment_no"] for j in hit["items"]] == [missing[0]]


def test_search_result_is_brief(client):
    """搜索走的也是下拉，必须同样跳过子订单展开——否则搜一次就把 1.1MB 那个坑再挖一遍。"""
    s = client.post("/api/shipment", json={"date": "2026-12-04", "shipment_no": "BR-1"}).json()
    client.post("/api/orders", json={
        "date": "2026-12-04", "order_no": "BR-O", "platform": "淘宝", "shipment_order_id": s["id"],
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]})
    got = client.get("/api/shipment", params={"q": "BR-1", "brief": True}).json()["items"][0]
    assert got["orders"] == []


# --- 三、前端两处下拉都得挂上搜索 ---------------------------------------------

_PICKERS = [
    ("frontend/src/views/Orders/index.vue", "订单页的集运格子"),
    ("frontend/src/components/OrderEditPanel.vue", "编辑面板的「所属集运」"),
]


def test_both_shipment_pickers_are_remote_searchable():
    """只给其中一个加搜索是最容易犯的错：另一处依旧只能在前 200 张里找。

    判据取「同一个 el-select 标签内同时出现 remote 与 remote-method」，
    而不是全文里各自出现过——后者在文件里随便有个 remote 就能骗过去。
    """
    bad = []
    for rel, label in _PICKERS:
        src = (_REPO / rel).read_text(encoding="utf-8")
        tags = [m.group(0) for m in re.finditer(r"<el-select\b[^>]*>", src, re.S)
                if "shipment_order_id" in m.group(0)]
        if not tags:
            bad.append(f"{label}：没找到绑定 shipment_order_id 的 el-select（选择器漂了？）")
            continue
        for t in tags:
            if "remote" not in t or "remote-method" not in t:
                bad.append(f"{label}：该 el-select 没有远程搜索，上限之外的集运单选不到")
    assert not bad, "\n  ".join([""] + bad)


def test_orders_page_displays_shipment_no_from_the_row():
    """显示不能回头依赖下拉。订单页的 shipNo 必须先看行上的 shipment_no。"""
    src = (_REPO / "frontend/src/views/Orders/index.vue").read_text(encoding="utf-8")
    m = re.search(r"function shipNo\(([^)]*)\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "shipNo 不见了或签名变了"
    assert "row" in m.group(1), "shipNo 拿不到行，就只能回去查下拉（上限之外必然显示成 #id）"
    body = m.group(2)
    assert "shipment_no" in body.split("shipById")[0], "shipNo 应先取行上的 shipment_no，再回落查下拉"
