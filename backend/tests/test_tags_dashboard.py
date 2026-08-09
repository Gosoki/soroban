"""标签（增删改名/颜色/在用保护）+ 看板聚合 + 物品列表。"""
import datetime as dt
from decimal import Decimal

from sqlmodel import Session, delete, select

from app.database import get_engine
from app.models import EXCLUDED_STATUSES, MiscExpense, Order, OrderStaging, ShipmentOrder


def test_add_list_remove_tag(client):
    client.post("/api/tags/recipient", json={"value": "临时收货人"})
    vals = [t["value"] for t in client.get("/api/tags/recipient").json()]
    assert "临时收货人" in vals
    client.delete("/api/tags/recipient/临时收货人")
    assert "临时收货人" not in [t["value"] for t in client.get("/api/tags/recipient").json()]


def test_add_tag_is_idempotent(client):
    client.post("/api/tags/recipient", json={"value": "重复标签"})
    r = client.post("/api/tags/recipient", json={"value": "重复标签"})
    assert r.status_code == 200
    assert sum(1 for t in r.json() if t["value"] == "重复标签") == 1


def test_empty_tag_rejected(client):
    assert client.post("/api/tags/recipient", json={"value": "   "}).status_code == 422


def test_tag_in_use_cannot_be_deleted(client):
    client.post("/api/shipment", json={"date": "2026-07-01", "recipient": "在用收货人"})
    tags = client.get("/api/tags/recipient").json()
    assert any(t["value"] == "在用收货人" and t["in_use"] for t in tags), "数据里的值应自动登记且标记在用"
    assert client.delete("/api/tags/recipient/在用收货人").status_code == 409


def test_first_ten_tags_get_distinct_colors(client):
    """调色盘 10 色：某字段最先登记的 10 个标签必须各不相同（之后才允许回落到「用得最少」）。
    数据里出现过的值会被自动登记、同样占色位，故按登记顺序（id 序）取前 10 个来断言。"""
    with Session(get_engine()) as s:
        from app.models import TagOption
        s.exec(delete(TagOption).where(TagOption.field == "platform"))
        s.commit()
    tags = client.get("/api/tags/platform").json()      # 先自动登记数据里已有的值
    i = 0
    while len(tags) < 10:
        tags = client.post("/api/tags/platform", json={"value": f"色{i}"}).json()
        i += 1
    colors = [t["color"] for t in tags[:10]]
    assert len(set(colors)) == 10, f"前 10 个标签撞色：{colors}"


def test_set_tag_color(client):
    client.post("/api/tags/recipient", json={"value": "改色人"})
    r = client.put("/api/tags/recipient/color", params={"value": "改色人", "color": 7})
    assert r.status_code == 200
    assert next(t for t in r.json() if t["value"] == "改色人")["color"] == 7


def test_set_tag_color_out_of_range_rejected(client):
    assert client.put("/api/tags/recipient/color",
                      params={"value": "x", "color": 10}).status_code == 422
    assert client.put("/api/tags/recipient/color",
                      params={"value": "x", "color": -1}).status_code == 422


def test_rename_tag_migrates_data_and_keeps_color(client):
    sh = client.post("/api/shipment", json={"date": "2026-07-02", "recipient": "改名前"}).json()
    before = next(t for t in client.get("/api/tags/recipient").json() if t["value"] == "改名前")
    r = client.post("/api/tags/recipient/rename", params={"old": "改名前", "new": "改名后"})
    assert r.status_code == 200
    after = next(t for t in r.json() if t["value"] == "改名后")
    assert after["color"] == before["color"], "改名应保留颜色"
    assert client.get(f"/api/shipment/{sh['id']}").json()["recipient"] == "改名后"


def test_rename_to_occupied_conflicts(client):
    client.post("/api/tags/recipient", json={"value": "占位A"})
    client.post("/api/tags/recipient", json={"value": "占位B"})
    assert client.post("/api/tags/recipient/rename",
                       params={"old": "占位A", "new": "占位B"}).status_code == 409


def test_rename_missing_tag_404(client):
    assert client.post("/api/tags/recipient/rename",
                       params={"old": "根本没有这个", "new": "新名"}).status_code == 404


def test_rename_to_empty_rejected(client):
    client.post("/api/tags/recipient", json={"value": "待改空"})
    assert client.post("/api/tags/recipient/rename",
                       params={"old": "待改空", "new": "   "}).status_code == 422


def test_platform_account_rename_must_go_through_plugin(client):
    assert client.post("/api/tags/platform_account/rename",
                       params={"old": "a", "new": "b"}).status_code == 400


def test_rename_bumps_version(client):
    sh = client.post("/api/shipment", json={"date": "2026-07-03", "recipient": "版本前"}).json()
    client.post("/api/tags/recipient/rename", params={"old": "版本前", "new": "版本后"})
    assert client.get(f"/api/shipment/{sh['id']}").json()["version"] > sh["version"]


# --- 看板 -------------------------------------------------------------------

def _clear_ledger():
    with Session(get_engine()) as s:
        from app.models import OrderItem, StagingItem
        s.exec(delete(OrderItem))
        s.exec(delete(StagingItem))
        s.exec(delete(OrderStaging))
        s.exec(delete(Order))
        s.exec(delete(ShipmentOrder))
        s.exec(delete(MiscExpense))
        s.commit()


def test_dashboard_totals_and_exclusions(client, mk):
    _clear_ledger()
    # 计入
    mk("/api/orders", {"date": "2026-08-05", "price_cny": "100", "fx_rate": "20",
                       "purchase_status": "待发货"})
    mk("/api/shipment", {"date": "2026-08-05", "price_cny": "50", "fx_rate": "20"})
    mk("/api/misc", {"date": "2026-08-05", "name": "杂", "price_cny": "10", "fx_rate": "20"})
    # 不计入
    for st in ("退款", "交易关闭", "待付款"):
        mk("/api/orders", {"date": "2026-08-05", "price_cny": "999", "fx_rate": "20",
                           "purchase_status": st})
    mk("/api/shipment", {"date": "2026-08-05", "price_cny": "999", "fx_rate": "20",
                         "shipment_status": "已取消"})
    # 造数落地的元断言：排除类断言天然恒真——「被排除的行没建出来」与「排除逻辑正确」
    # 给出同一个数。不钉住这几行确实存在且带着钱，下面 6 条断言就是摆设（本文件出过这事）。
    assert client.get("/api/orders", params={"limit": 200}).json()["total"] == 4
    assert client.get("/api/shipment", params={"limit": 200}).json()["total"] == 2
    d = client.get("/api/dashboard").json()
    assert d["order_jpy"] == 2000
    assert d["shipment_jpy"] == 1000
    assert d["misc_jpy"] == 200
    assert d["total_jpy"] == 3200
    assert d["order_count"] == 1 and d["shipment_count"] == 1 and d["misc_count"] == 1
    m = next(x for x in d["by_month"] if x["month"] == "2026-08")
    assert m["jpy"] == 3200


def test_dashboard_excludes_soft_deleted(client):
    _clear_ledger()
    o = client.post("/api/orders", json={"date": "2026-09-01", "price_cny": "100", "fx_rate": "20"}).json()
    assert client.get("/api/dashboard").json()["order_jpy"] == 2000
    client.delete(f"/api/orders/{o['id']}")
    assert client.get("/api/dashboard").json()["order_jpy"] == 0


def test_dashboard_empty_is_zero(client):
    _clear_ledger()
    d = client.get("/api/dashboard").json()
    assert d["total_jpy"] == 0 and d["by_month"] == []


def test_excluded_statuses_are_all_real_enum_values(client):
    """EXCLUDED_STATUSES 里每个值都必须是某个枚举的真实值，否则排除规则静默失效。"""
    from app.models import PurchaseStatus, ShipmentStatus
    valid = {s.value for s in PurchaseStatus} | {s.value for s in ShipmentStatus}
    assert EXCLUDED_STATUSES <= valid


# --- 物品列表 ---------------------------------------------------------------

def test_items_list_amount_and_context(client):
    _clear_ledger()
    o = client.post("/api/orders", json={
        "date": "2026-10-01", "title": "店", "order_no": "IT-1", "platform": "淘宝",
        "items": [{"name": "甲", "quantity": 3, "unit_price_cny": "2.50"}],
    }).json()
    res = client.get("/api/items", params={"q": "甲"}).json()
    it = next(x for x in res["items"] if x["name"] == "甲")
    assert Decimal(it["amount_cny"]) == Decimal("7.50")
    assert it["order_id"] == o["id"] and it["order_no"] == "IT-1"


def test_items_list_hides_deleted_orders(client):
    _clear_ledger()
    o = client.post("/api/orders", json={"date": "2026-10-02",
                                         "items": [{"name": "乙", "quantity": 1, "unit_price_cny": "1"}]}).json()
    assert client.get("/api/items", params={"q": "乙"}).json()["total"] == 1
    client.delete(f"/api/orders/{o['id']}")
    assert client.get("/api/items", params={"q": "乙"}).json()["total"] == 0


def test_items_total_matches_rows(client):
    _clear_ledger()
    client.post("/api/orders", json={"date": "2026-10-03", "items": [
        {"name": "p1", "quantity": 1, "unit_price_cny": "1"},
        {"name": "p2", "quantity": 1, "unit_price_cny": "1"},
        {"name": "p3", "quantity": 1, "unit_price_cny": "1"},
    ]})
    res = client.get("/api/items", params={"limit": 500}).json()
    assert res["total"] == len(res["items"]) == 3


def test_dashboard_reports_rows_whose_money_was_swallowed(client, session, mk):
    """有货款、却缺汇率算不出日元的行，看板必须**报出来**。

    `SUM(jpy_settled)` 把 NULL 直接跳过 → 金额被吞、笔数照数：合计变小而单数不变，
    界面上没有任何一处显示异常。命中条件很实在：全新部署且汇率插件一次都没跑成
    （断网 / 源挂了 / 打包版跑不起来），而用户已经开始录单。
    """
    from decimal import Decimal

    from app.models import MiscExpense

    before = client.get("/api/dashboard").json()
    row = MiscExpense(date=dt.date(2026, 3, 1), name="缺汇率的一笔",
                      price_cny=Decimal("100.00"))
    row.compute_money()
    assert row.jpy_settled is None, "构造前提不成立：这一行居然算出了日元"
    session.add(row)
    session.commit()
    try:
        got = client.get("/api/dashboard").json()
        assert got["uncounted_count"] == before["uncounted_count"] + 1, \
            "被吞掉的行没有被数出来——看板会静默少一笔钱"
        assert Decimal(str(got["uncounted_cny"])) == \
            Decimal(str(before["uncounted_cny"])) + Decimal("100.00")
        assert got["total_jpy"] == before["total_jpy"], "用例前提变了：这一行不该进合计"
        assert got["misc_count"] == before["misc_count"] + 1, "笔数照数——这正是它的隐蔽之处"
    finally:
        session.delete(row)
        session.commit()


def test_zero_amount_rows_are_not_reported_as_swallowed(client, session):
    """货款显式填 0（预付/包邮）没有任何金额会被吞，报出来只是噪音。"""
    from decimal import Decimal

    from app.models import MiscExpense

    before = client.get("/api/dashboard").json()["uncounted_count"]
    row = MiscExpense(date=dt.date(2026, 3, 2), name="零元", price_cny=Decimal("0.00"))
    row.compute_money()
    session.add(row)
    session.commit()
    try:
        assert client.get("/api/dashboard").json()["uncounted_count"] == before
    finally:
        session.delete(row)
        session.commit()
