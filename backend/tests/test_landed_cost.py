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


def test_a_zero_yuan_row_is_not_reported_as_unconverted(client, session):
    """显式填 0 的行（预付/包邮/全是赠品）**不算**「未折算」。

    0 元折算过去也是 0 円，没有任何金额被 `SUM` 吞掉，报出来只是噪音。
    看板的 `_uncounted` 判据里明确有 `price_cny != 0` 这一条并写了理由；
    页脚原先漏抄了它，于是全新部署、还没有汇率时记一笔 0 元，
    **页脚亮黄标「1 条未折算」而同一时刻看板说 0 条**——两个数字对同一件事给出相反结论，
    用户按告警去补汇率也消不掉它。

    这条同时钉住「两处判据必须一致」：真正缺汇率的行仍要被数出来。
    """
    from sqlmodel import select

    from app.models import MiscExpense

    r = client.post("/api/misc", json={"date": "2026-03-01", "name": "包邮预付", "price_cny": "0.00"})
    assert r.status_code in (200, 201), r.text
    row = session.exec(select(MiscExpense).where(MiscExpense.id == r.json()["id"])).one()
    row.fx_rate = row.jpy_auto = row.jpy_settled = None       # 模拟「库里还没有汇率」
    session.add(row)
    session.commit()

    got = client.get("/api/misc", params={"q": "包邮预付"}).json()
    assert got["total"] == 1
    assert got["unconverted"] == 0, f"0 元的行被误报成未折算：{got}"

    # 反向：真的有钱却缺汇率的行，仍然必须数出来
    r2 = client.post("/api/misc", json={"date": "2026-03-02", "name": "真缺汇率", "price_cny": "88"})
    row2 = session.exec(select(MiscExpense).where(MiscExpense.id == r2.json()["id"])).one()
    row2.fx_rate = row2.jpy_auto = row2.jpy_settled = None
    session.add(row2)
    session.commit()
    got2 = client.get("/api/misc", params={"q": "真缺汇率"}).json()
    assert got2["unconverted"] == 1, f"真正缺汇率的行没被数出来：{got2}"


def test_the_footer_and_the_dashboard_agree_on_what_unconverted_means(client, session):
    """页脚与看板对「未折算」的判据必须**逐条一致**。

    它们是同一个概念的两个出口。分叉的现象不是报错，而是「两个数字互相打脸」——
    用户没有任何办法判断该信哪个。

    用**增量**比较而不是比总数：会话共享库里还有别的用例造的行，
    比总数会因为无关数据而红（第一版就是这么写的，红在了一个假问题上）。
    """
    from sqlmodel import select

    from app.models import MiscExpense

    def _board_uncounted():
        b = client.get("/api/dashboard").json()
        key = next((k for k in b if "uncounted" in k and "count" in k), None)
        assert key, f"看板没有 uncounted 计数了，探测方式可能已过期：{sorted(b)}"
        return b[key]

    def _foot_uncounted():
        return client.get("/api/misc", params={"limit": 1}).json()["unconverted"]

    board0, foot0 = _board_uncounted(), _foot_uncounted()

    def _add(name, price):
        r = client.post("/api/misc", json={"date": "2026-03-03", "name": name, "price_cny": price})
        assert r.status_code in (200, 201), r.text
        row = session.exec(select(MiscExpense).where(MiscExpense.id == r.json()["id"])).one()
        row.fx_rate = row.jpy_auto = row.jpy_settled = None      # 模拟「库里还没有汇率」
        session.add(row)
        session.commit()

    _add("零元-一致性", "0")
    assert _board_uncounted() - board0 == 0, "看板把 0 元的行算成了未折算"
    assert _foot_uncounted() - foot0 == 0, "页脚把 0 元的行算成了未折算"

    _add("有钱-一致性", "50")
    assert _board_uncounted() - board0 == 1, "看板漏数了一条真正缺汇率的行"
    assert _foot_uncounted() - foot0 == 1, "页脚漏数了一条真正缺汇率的行"


def test_each_child_order_says_whether_it_counts_toward_the_total(client):
    """每条子订单都要自报「算不算进货款合计」。

    不给这个标记的话，前端只有两条路：抄一份「哪些状态不计入」的清单（两份迟早对不上），
    或者像对账单原先那样把所有行都列出来、下面写一个已经剔除过的合计——
    明细逐条加起来 21000 円、合计写 10,000 円，收到的人只能认为这单子是错的。
    """
    from app.models.order.order import PURCHASE_EXCLUDED

    s = _mk_shipment(client, jpy_override=2500)
    good = _mk_order(client, jpy_override=10000, title="算数的")
    dead = _mk_order(client, jpy_override=5000, title="不算的",
                     purchase_status=sorted(PURCHASE_EXCLUDED)[0])
    for o in (good, dead):
        assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    got = _get(client, s["id"])
    by_title = {o["title"]: o for o in got["orders"]}
    assert by_title["算数的"]["counted"] is True
    assert by_title["不算的"]["counted"] is False, "不计入的单没有被标出来"

    # 标记必须与合计**自洽**：把标了 counted 的加起来，正好等于 orders_jpy
    assert sum(o["jpy_settled"] or 0 for o in got["orders"] if o["counted"]) == got["orders_jpy"], \
        "counted 标记与 orders_jpy 对不上——两者必须出自同一份判据"


def test_a_zero_yuan_child_order_is_not_counted_as_unconverted(client, session):
    """集运到岸的「未折算」判据必须与页脚、看板**逐条一致**（含 `!= 0`）。

    一张全是赠品（¥0）的子订单折算过去也是 0 円，没有任何金额被吞——报出来只是噪音。
    而这一处原先自己写了一份判据、漏了 `!= 0`：**同一页的页脚说 0 条、这里说 1 条**。

    同一件事三个出口（看板 / 列表页脚 / 集运到岸），说法不一样就是互相打脸，
    用户没有任何办法判断该信哪个。
    """
    from sqlmodel import select

    from app.models import Order

    s = _mk_shipment(client, jpy_override=1000)
    gift = _mk_order(client, price_cny="0", title="全是赠品")
    row = session.exec(select(Order).where(Order.id == gift["id"])).one()
    row.fx_rate = row.jpy_auto = row.jpy_settled = None      # 模拟「库里还没有汇率」
    session.add(row)
    session.commit()
    assert client.post(f"/api/shipment/{s['id']}/order/{gift['id']}").status_code == 200

    got = _get(client, s["id"])
    assert got["unconverted"] == 0, f"0 元的子订单被报成未折算：{got}"

    # 反面：真的有钱却缺汇率的子订单，仍然要数出来
    paid = _mk_order(client, price_cny="88", title="真缺汇率")
    row2 = session.exec(select(Order).where(Order.id == paid["id"])).one()
    row2.fx_rate = row2.jpy_auto = row2.jpy_settled = None
    session.add(row2)
    session.commit()
    assert client.post(f"/api/shipment/{s['id']}/order/{paid['id']}").status_code == 200
    assert _get(client, s["id"])["unconverted"] == 1, "真正缺汇率的子订单没被数出来"


def test_the_three_unconverted_criteria_are_one_function():
    """「有钱却没折算」的判据全仓只许有**一份规则**，三个出口都得用它。

    历史上分叉过两次（§151.3 页脚、§169 集运到岸），每次都是漏抄 `!= 0`。
    分叉的现象是两个数字互相打脸，而两边各自都「看起来对」。

    规则一份、**形态两种**：看板与列表页脚在库里聚合，用 SQL 形态
    `unconverted_clause(model)`；集运到岸的子订单已经在内存里，用 Python 形态
    `is_unconverted(price, jpy)`。两个函数就住在彼此隔壁，改判据时一起改。

    判据是**正向的**（三处都调了共用函数），不是反向找「谁手写了判据」——
    第一版就是反向写的，它把 `demo.py` 的列名清单和这几处**调用本身**
    一起报了出来，又踩了「在代码里找名字」那个坑。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    expect = {
        "routers/dashboard.py": "unconverted_clause",     # 看板：SQL
        "routers/common.py": "unconverted_clause",        # 列表页脚：SQL
        "routers/shipment.py": "is_unconverted",          # 集运到岸：Python
    }
    missing = []
    for rel, fn in expect.items():
        src = (root / rel).read_text(encoding="utf-8")
        if f"{fn}(" not in src:
            missing.append(f"{rel} 没有调用 {fn}()")
    assert not missing, (
        "这些出口没有走共用判据（会各自漂）：\n  " + "\n  ".join(missing))

    # 两种形态必须同源：都在 models/base.py 里，且都带 `!= 0`
    base = (root / "models" / "base.py").read_text(encoding="utf-8")
    for fn in ("def is_unconverted", "def unconverted_clause"):
        assert fn in base, f"{fn} 不在 models/base.py 里"
    # 只看两个函数**各自的 return 语句**——不是数整段里 `!= 0` 出现几次：
    # docstring 里也会提到它（第一版就是这么写的，数出 5 次然后红在一个假问题上）。
    import ast

    tree = ast.parse(base)
    for fn in ("is_unconverted", "unconverted_clause"):
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        rets = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, ast.Return) and x.value]
        assert rets, f"{fn} 没有 return"
        assert any("!= 0" in r for r in rets), \
            f"{fn} 的判据里漏了 `!= 0`（显式填 0 的行会被误报成未折算）：{rets}"


def test_a_second_exclusion_axis_is_honoured_by_the_shipment_page(client, session, monkeypatch):
    """`ledger_exclusions()` 里**追加一条轴**之后，集运页必须跟着不算——和看板一样。

    那个返回值是个**列表**，`LedgerBase.ledger_exclusions` 的 docstring 明写着理由：
    「将来加卖出/退货这类并行的状态轴时，是往列表里追一项」。
    看板按 `(列, 值集合)` 逐条判（`dashboard._valid_conds`），
    而集运页原先把它**拍平**成一个值集合、再拿去比 `purchase_status`——列名被丢掉。

    今天两张表各只声明一条轴，拍平恰好等价，所以**光靠现有数据测不出来**。
    这条守卫因此自己造出「将来」：临时给 `Order.ledger_exclusions` 追一条
    以 `created_via` 为轴的规则，再问集运页算不算。
    拍平的写法会拿「imported」去比 `purchase_status`、永远不命中 ⇒
    把一张本该排除的单算进货款合计，而看板不算——**同一笔钱两个页面两个说法**。

    用 `created_via` 当第二根轴，是因为它是 `LedgerBase` 上真实存在、
    且与 `purchase_status` **取值集合完全不相交**的列：不相交才能把
    「拿错列去比」和「拿对列去比」区分开。
    """
    from app.models import CreatedVia, Order
    from app.routers import shipment as mod

    s = client.post("/api/shipment", json={"date": "2027-06-01",
                                           "shipment_no": "SH-SECOND-AXIS",
                                           "price_cny": "0", "fx_rate": "20"}).json()
    made = []
    for tag, via in (("算的", CreatedVia.manual.value), ("不该算的", CreatedVia.imported.value)):
        o = client.post("/api/orders", json={
            "date": "2027-06-01", "title": f"{tag}", "order_no": f"AXIS-{tag}",
            "purchase_status": "待收货", "price_cny": "100", "fx_rate": "20"}).json()
        # **先挂靠、再改列**：反过来的话，直接改库不会 bump `version`，
        # 而 PATCH 送的 `version+1` 就对不上（第一版这么写，夹具直接 0 円）。
        r = client.patch(f"/api/orders/{o['id']}",
                         json={"version": o["version"], "shipment_order_id": s["id"]})
        assert r.status_code == 200, r.text
        row = session.get(Order, o["id"])
        row.created_via = via                      # 走库直接置，POST/PATCH 都不收这个字段
        session.add(row)
        session.commit()
        made.append(r.json())

    base = client.get(f"/api/shipment/{s['id']}").json()
    assert base["orders_jpy"] == 4000, f"夹具没造对：两单各 2000 円，实际 {base['orders_jpy']}"

    # —— 造出「将来」：追一条以 created_via 为轴的排除规则 ——
    orig = Order.ledger_exclusions

    @classmethod
    def two_axes(cls):
        return [*orig.__func__(cls), (cls.created_via, (CreatedVia.imported.value,))]

    monkeypatch.setattr(Order, "ledger_exclusions", two_axes)

    after = client.get(f"/api/shipment/{s['id']}").json()
    assert after["orders_jpy"] == 2000, (
        f"追加一条轴之后集运页仍然把两单都算了（{after['orders_jpy']} 円）——"
        f"它多半又把 `ledger_exclusions()` 拍平成值集合、拿新轴的值去比 `purchase_status`，"
        f"永远不命中。而看板按列判、会排除掉，同一笔钱两个页面两个说法")
    by_no = {o["order_no"]: o for o in after["orders"]}
    assert by_no["AXIS-不该算的"]["counted"] is False, (
        "子订单表上那一行也该标成「不计入」——前端就是照这个字段渲染的")
    assert by_no["AXIS-算的"]["counted"] is True, "把不该排的也排了"
