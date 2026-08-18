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


# 分派判据（`_plugin_owns_field`）在这两条里**显式打桩**：
# 测试环境的 PLUGIN_DIR 指向空目录，而真实环境看得到淘宝插件——
# 依赖「环境恰好发现了什么」的话，同一条断言在两处会得到相反结果，
# 而且哪一边都说不出自己在测分派本身。
def test_rename_is_refused_when_a_plugin_claims_the_column(client, monkeypatch):
    """有插件声明这一列时，改名要走插件端点（磁盘会话与插件配置得一起迁）。"""
    from app.routers import tags as mod

    client.post("/api/orders", json={"date": "2026-08-18", "order_no": "PLUGOWN-1",
                                     "platform": "淘宝", "platform_account": "插件管的号"})
    monkeypatch.setattr(mod, "_plugin_owns_field", lambda field: True)
    r = client.post("/api/tags/platform_account/rename",
                    params={"old": "插件管的号", "new": "新名"})
    assert r.status_code == 400, f"{r.status_code} {r.text[:200]}"
    assert "插件管理" in r.text, "报错没告诉用户该去哪儿改"


def test_the_same_column_can_be_renamed_locally_when_no_plugin_claims_it(client, monkeypatch):
    """**没有插件声明这一列时，必须能本地改名**——原先这里是一条死路。

    原判据是「列名 == platform_account 就拒绝」，而前端那条替代路径又把 `taobao`
    焊在 URL 里。于是插件目录不在时（源码安装、自定 PLUGIN_DIR），
    手工录单产生的账号名**既删不掉也改不了名**：
    DELETE 返回 409（in_use）、这里 400 让你走插件、插件端点 404「未发现插件: taobao」。
    而列头那颗改名笔对所有 tag 列无条件渲染——用户只会看到一句
    「未发现插件: taobao」，和他正在做的事毫无关系。

    判据改成「**有没有插件声明这个字段**」（清单的 `accounts_ledger_field`）。
    注意**不能**写成「插件缺失就跳过迁移」：那会在插件只是暂时没装好时，
    把账本改了而磁盘会话与插件配置留在旧名下，下一轮抓取又把旧名建回来。
    """
    from app.routers import tags as mod

    # 账号名带随机后缀 + 按 **id** 回查：整套跑时别的用例也在造订单，
    # 用固定值 + 列表查询会拿到别人的行（这一条第一次写就是这么红的，
    # 而红的信息是「platform_account is None」，完全看不出是拿错了行）。
    import uuid
    acct = f"手工录的-{uuid.uuid4().hex[:8]}"
    oid = client.post("/api/orders", json={"date": "2026-08-18", "order_no": f"NOPLUG-{acct}",
                                           "platform": "闲鱼", "platform_account": acct}).json()["id"]
    monkeypatch.setattr(mod, "_plugin_owns_field", lambda field: False)
    r = client.post("/api/tags/platform_account/rename", params={"old": acct, "new": acct + "-新"})
    assert r.status_code == 200, f"没有插件时仍然改不了名，用户被堵死：{r.status_code} {r.text[:200]}"

    got = client.get(f"/api/orders/{oid}").json()
    assert got["platform_account"] == acct + "-新", got


def test_the_dispatch_is_by_declaration_not_by_column_name():
    """分派判据必须是「有插件声明这个字段」，不是写死的列名。

    写死列名的话，将来第二个声明 `accounts_ledger_field` 的插件出现时，
    它管的那一列会被当成普通标签本地改名——账本改了、那个插件的磁盘会话没动。
    """
    import inspect

    from app.routers import tags as mod

    src = inspect.getsource(mod.rename_tag)
    assert "_plugin_owns_field(field)" in src, "分派没有走「按声明判」"
    assert 'field == "platform_account"' not in src, "还在按列名写死"

    owns = inspect.getsource(mod._plugin_owns_field)
    assert "_ledger_field(m) == field" in owns, "没有按清单的 accounts_ledger_field 判"


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
