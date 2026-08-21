"""到岸成本：一张集运单真正花了多少钱 = 里面商品单的货款 + 这张单自己的国际运费。

这个数最容易出的错不是算错，而是**算漏了却看不出来**：
`SUM` 对 NULL 视而不见，缺汇率的单会让合计静默变小而笔数照旧。
所以每条断言都成对出现——数对了，而且「没算进去的有几条」也说得出来。
"""
from __future__ import annotations

_seq = iter(range(1, 10_000))


def _mk_shipment(client, **kw):
    body = {"date": "2026-08-01", "shipment_no": f"SP-LANDED-{next(_seq)}",
            "shipment_status": "打包中"}
    body.update(kw)
    r = client.post("/api/shipment", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _mk_order(client, **kw):
    body = {"date": "2026-08-01", "title": "货", "purchase_status": "待收货"}
    body.update(kw)
    r = client.post("/api/orders", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _get(client, sid):
    r = client.get(f"/api/shipment/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def test_shipment_read_derived_fields_are_registered():
    """`ShipmentRead` 里每个字段，要么是集运表上的列，要么登记在 `_DERIVED_FIELDS` 里。

    构造响应时会挨个 `getattr(集运行, 字段名)`——漏登记一个就是运行时 AttributeError，
    而且只在这一条路径上炸。这条把它提前到测试期。
    """
    from app.models import ShipmentOrder
    from app.routers.shipment import _DERIVED_FIELDS
    from app.schemas import ShipmentRead

    missing = [k for k in ShipmentRead.model_fields
               if k not in _DERIVED_FIELDS and not hasattr(ShipmentOrder, k)]
    assert not missing, (
        f"这些字段既不在集运表上、也没登记为派生字段：{missing}\n"
        "（是本表的列就加列；是算出来的就加进 _DERIVED_FIELDS 并在 _landed 里算）")


def test_landed_cost_is_goods_plus_international_shipping(client):
    """到岸合计 = 子订单货款 + 本单运费。"""
    s = _mk_shipment(client, jpy_override=3000)
    a = _mk_order(client, jpy_override=10000)
    b = _mk_order(client, jpy_override=5000)
    for o in (a, b):
        assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    got = _get(client, s["id"])
    assert got["orders_jpy"] == 15000
    assert got["landed_jpy"] == 18000, "到岸合计没把本单的国际运费算进去"
    assert got["unconverted"] == 0


def test_cancelled_orders_do_not_count_toward_landed_cost(client):
    """退款/关闭的单没花钱，不该算进这张集运单的成本。

    排除规则从 `Order.ledger_exclusions()` 上取，与看板同一套——另抄一份状态清单
    正是这类合计出错的常见方式。
    """
    from app.models.order.order import PURCHASE_EXCLUDED

    s = _mk_shipment(client, jpy_override=1000)
    good = _mk_order(client, jpy_override=8000)
    dead = _mk_order(client, jpy_override=9999, purchase_status=sorted(PURCHASE_EXCLUDED)[0])
    for o in (good, dead):
        assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    got = _get(client, s["id"])
    assert got["orders_jpy"] == 8000, "把不计入的单算进来了"
    assert got["landed_jpy"] == 9000


def test_an_order_with_no_rate_is_counted_as_unconverted_not_as_zero(client, session):
    """有货款、却缺汇率没折算的单，必须**数出来**，不能悄悄当 0。

    这是这个功能最危险的失败形态：合计变小、单数不变，界面上没有任何异常。
    """
    from sqlmodel import select

    from app.models import Order

    s = _mk_shipment(client, jpy_override=1000)
    o = _mk_order(client, price_cny="500")
    assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    # 把这单的汇率抹掉，模拟「当时没有汇率」
    row = session.exec(select(Order).where(Order.id == o["id"])).one()
    row.fx_rate = None
    row.jpy_auto = None
    row.jpy_settled = None
    session.add(row)
    session.commit()

    got = _get(client, s["id"])
    assert got["unconverted"] == 1, f"缺汇率的单没被数出来：{got}"
    assert got["orders_jpy"] == 0, "缺汇率的单不该凭空贡献金额"


def test_brief_reports_unknown_not_zero(client):
    """`brief=True` 不展开子订单，三个到岸字段必须是 **None**（不知道），不是 0（没花钱）。

    报 0 的话，下拉里一张挂着十几万日元的集运单会显示成「0 円」。
    """
    s = _mk_shipment(client, jpy_override=3000)
    o = _mk_order(client, jpy_override=10000)
    assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    brief = client.get("/api/shipment", params={"brief": True}).json()["items"]
    mine = next(x for x in brief if x["id"] == s["id"])
    assert mine["orders_jpy"] is None and mine["landed_jpy"] is None \
        and mine["unconverted"] is None, f"brief 下报了具体数字：{mine}"

    full = client.get("/api/shipment").json()["items"]
    mine = next(x for x in full if x["id"] == s["id"])
    assert mine["landed_jpy"] == 13000


def test_landed_cost_costs_no_extra_queries(client):
    """到岸成本从**已经在内存里**的子订单算，不许多打一条 SQL。

    列表页一屏 50 行，每行再查一次就是 50 条——这个接口批量化的全部意义就在于此。
    用「行数翻倍、查询数不涨」来判，而不是钉一个绝对条数：后者会因为任何无关改动而红。
    """
    from tests.test_queries import count_queries

    for _ in range(2):
        s = _mk_shipment(client, jpy_override=1000)
        for _ in range(2):
            o = _mk_order(client, jpy_override=100)
            client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    with count_queries() as few:
        client.get("/api/shipment", params={"limit": 2})

    for _ in range(6):
        s = _mk_shipment(client, jpy_override=1000)
        for _ in range(2):
            o = _mk_order(client, jpy_override=100)
            client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    with count_queries() as many:
        client.get("/api/shipment", params={"limit": 8})

    assert many["n"] <= few["n"] + 1, (
        f"到岸合计让查询数随行数增长（2 行 {few['n']} 条 → 8 行 {many['n']} 条）\n"
        + "\n".join(many["sql"]))


# --- 列表页脚合计 ---------------------------------------------------------------

def test_footer_sum_covers_the_whole_filter_not_just_the_page(client):
    """页脚合计求的是**当前筛选出的全部行**，不是屏幕上那一页。

    只合计当页的话，翻页时这个数会跟着变——那样它就没有任何用处了。
    """
    for i in range(5):
        _mk_order(client, jpy_override=1000, title=f"页脚-{i}", order_no=f"FOOT-{i}")

    page = client.get("/api/orders", params={"limit": 2, "q": "页脚"}).json()
    assert len(page["items"]) == 2, "分页没生效，这条测不到东西"
    assert page["total"] == 5
    assert page["sum_jpy"] == 5000, f"页脚只合计了当页：{page['sum_jpy']}"


def test_footer_reports_rows_that_have_money_but_no_yen(client, session):
    """有货款、却缺汇率没折算的行要**数出来**——否则合计静默变小而条数照旧。"""
    from sqlmodel import select

    from app.models import Order

    o = _mk_order(client, price_cny="500", title="没汇率", order_no="FOOT-NOFX")
    row = session.exec(select(Order).where(Order.id == o["id"])).one()
    row.fx_rate = row.jpy_auto = row.jpy_settled = None
    session.add(row)
    session.commit()

    got = client.get("/api/orders", params={"q": "没汇率"}).json()
    assert got["unconverted"] == 1, f"没折算的行没被数出来：{got}"


def test_footer_totals_cost_no_extra_query(client):
    """页脚三个数由**一条** SQL 算出，替换掉原先那条只数条数的查询。

    列表接口每多打一次库，都会在一屏 50 行、六个页面轮流刷新时被放大。
    """
    from tests.test_queries import count_queries

    for i in range(3):
        _mk_order(client, jpy_override=10, title=f"Q-{i}", order_no=f"QQ-{i}")
    with count_queries() as a:
        client.get("/api/orders", params={"limit": 3})
    with count_queries() as b:
        client.get("/api/misc", params={"limit": 3})
    # 订单要预加载两个关系，杂项没有关系可加载；两边都不该出现「数条数」和「求合计」两条
    joined = "\n".join(a["sql"] + b["sql"])
    assert joined.lower().count("count(*)") <= 2, f"页脚多打了查询：\n{joined}"


def test_items_deliberately_has_no_footer_sum(client):
    """物品页**刻意没有**日元合计——它是一条 join，按订单级金额求和会把同一笔钱数好几遍。

    一张订单有三件物品，join 出三行，`SUM(订单.jpy_settled)` 就是三倍。
    这不是「还没做」，是不能做，所以钉住它。
    """
    r = client.get("/api/items", params={"limit": 1})
    assert r.status_code == 200, r.text
    assert "sum_jpy" not in r.json(), \
        "物品页出现了日元合计——那是一条 join，订单级金额会被物品条数放大"


def test_the_footer_sum_is_not_multiplied_by_the_number_of_items(client):
    """一单三件物品，按物品名搜索时合计仍然是**一份钱**。

    这是这一栏最容易被放大的地方：`q` 会搜物品名。今天用的是 EXISTS 子查询
    （不改变结果行的形状），一旦有人把它改成 `join(OrderItem)`，
    每单会变成三行，`SUM(订单.jpy_settled)` 就是三倍——**条数也会一起变**，
    但没人会核对条数，只会看到账上的钱莫名其妙多了。

    `total` 与 `sum_jpy` 是同一条 SQL 出来的，所以这条同时钉住两者。
    """
    o = _mk_order(client, jpy_override=7000, title="三件套", order_no="MULT-1")
    r = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "螺丝刀甲", "quantity": 1, "unit_price_cny": "1"},
                  {"name": "螺丝刀乙", "quantity": 1, "unit_price_cny": "1"},
                  {"name": "螺丝刀丙", "quantity": 1, "unit_price_cny": "1"}],
    })
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 3

    got = client.get("/api/orders", params={"q": "螺丝刀"}).json()
    assert got["total"] == 1, f"一单被数成了 {got['total']} 条（物品把行数放大了）"
    assert len(got["items"]) == 1
    assert got["sum_jpy"] == got["items"][0]["jpy_settled"], \
        f"合计被物品条数放大了：{got['sum_jpy']} vs 单行 {got['items'][0]['jpy_settled']}"
