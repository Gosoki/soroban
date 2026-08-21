"""筛选与搜索：能不能用**手上真有的那个东西**把一张单找回来。

这些筛选的失败形态不是报错，而是「点了没反应 / 少几条」——没有任何提示，
用户只会以为记录不存在。所以每条都成对断言：该出现的出现了，**不该出现的没出现**。
"""
from __future__ import annotations

_seq = iter(range(1, 10_000))


def _ship(client, **kw):
    body = {"date": "2026-08-02", "shipment_no": f"SF-{next(_seq)}", "shipment_status": "打包中"}
    body.update(kw)
    r = client.post("/api/shipment", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _order(client, **kw):
    body = {"date": "2026-08-02", "title": "货", "purchase_status": "待收货"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_shipment_search_finds_by_international_tracking_no(client):
    """手上只有一张国际运单号时，也要能把集运单找回来。"""
    mine = _ship(client, intl_tracking_no="LX998877665CN")
    _ship(client, intl_tracking_no="OTHER111")

    got = client.get("/api/shipment", params={"q": "998877"}).json()
    ids = [x["id"] for x in got["items"]]
    assert mine["id"] in ids, "按国际运单号搜不到"
    assert len(ids) == 1, f"把别的单也搜出来了：{ids}"


def test_shipment_search_finds_by_recipient(client):
    """只记得收件人名字时也要能找回来。"""
    mine = _ship(client, recipient="山田太郎")
    _ship(client, recipient="佐藤花子")

    got = client.get("/api/shipment", params={"q": "山田"}).json()
    ids = [x["id"] for x in got["items"]]
    assert mine["id"] in ids and len(ids) == 1, f"{ids}"


def test_shipment_can_be_filtered_by_recipient(client):
    mine = _ship(client, recipient="收件甲")
    other = _ship(client, recipient="收件乙")

    got = client.get("/api/shipment", params={"recipient": "收件甲"}).json()
    ids = [x["id"] for x in got["items"]]
    assert ids == [mine["id"]], f"{ids}"
    assert other["id"] not in ids


def test_orders_can_be_filtered_by_the_recipient_of_their_shipment(client):
    """按**所属集运单的收货人**筛订单。收货人在集运表上，走子查询。

    刻意不给订单加一列冗余的收货人：那样要在挂靠 / 解挂 / 改集运单收货人三处同步，
    漏一处就是两张表说法不一——而这种不一致不会报错，只会让筛选悄悄少几单。
    """
    s1 = _ship(client, recipient="张三")
    s2 = _ship(client, recipient="李四")
    a = _order(client, title="给张三的")
    b = _order(client, title="给李四的")
    c = _order(client, title="还没挂靠的")
    client.post(f"/api/shipment/{s1['id']}/order/{a['id']}")
    client.post(f"/api/shipment/{s2['id']}/order/{b['id']}")

    got = client.get("/api/orders", params={"recipient": "张三"}).json()
    titles = [x["title"] for x in got["items"]]
    assert titles == ["给张三的"], titles
    assert "还没挂靠的" not in titles, "没挂靠的单不该出现在按收货人的筛选里"
    assert got["total"] == 1


def test_filtering_orders_by_recipient_ignores_deleted_shipments(client, session):
    """指向已软删集运单的订单，不该被它的收货人筛出来。

    **今天的 API 造不出这个状态**：`DELETE /api/shipment/{id}` 会先把子订单解挂
    （`shipment_order_id=None`）再软删。所以这里直接在库里造出「订单还指着一张已软删的
    集运单」，也就是**将来某条忘了解挂的删除路径**会留下的样子——
    子查询里那句 `is_delete.is_(False)` 防的正是它。

    不这么造的话，这条测试会因为「订单早就被解挂了」而绿，
    与它声称在验的东西无关——把守卫删掉照样绿。
    """
    from sqlmodel import select

    from app.models import Order, ShipmentOrder

    s = _ship(client, recipient="王五")
    o = _order(client, title="王五的货")
    client.post(f"/api/shipment/{s['id']}/order/{o['id']}")

    row = session.exec(select(ShipmentOrder).where(ShipmentOrder.id == s["id"])).one()
    row.is_delete = True                      # 只软删，**不解挂**
    session.add(row)
    session.commit()
    still = session.exec(select(Order).where(Order.id == o["id"])).one()
    assert still.shipment_order_id == s["id"], "前提没造出来：订单已经不指着那张单了"

    got = client.get("/api/orders", params={"recipient": "王五"}).json()
    assert got["total"] == 0, f"已软删的集运单还在参与筛选：{got}"


def test_renaming_a_misc_category_bumps_the_row_version(client, session):
    """改「杂项分类」这个标签要推进受影响行的 version——它是这次新接进标签体系的字段。

    不推进的话：甲把分类从「手续费」改成「银行手续费」，乙手上那行还停在旧 version，
    乙一保存就把改名**顶掉**，而且不会 409（乙的 version 仍然匹配）。
    这正是「2–3 个人同时编辑」下最难查的那种覆盖。
    """
    from sqlmodel import select

    from app.models import MiscExpense

    r = client.post("/api/misc", json={"date": "2026-08-03", "name": "转账", "category": "手续费"})
    assert r.status_code in (200, 201), r.text
    before = session.exec(select(MiscExpense).where(MiscExpense.id == r.json()["id"])).one().version

    rn = client.post("/api/tags/category/rename",
                     params={"old": "手续费", "new": "银行手续费"})
    assert rn.status_code == 200, rn.text

    session.expire_all()
    row = session.exec(select(MiscExpense).where(MiscExpense.id == r.json()["id"])).one()
    assert row.category == "银行手续费", "改名没落到数据行上"
    assert row.version > before, "改名没推进 version——并发保存会把改名顶掉且不报 409"
