"""金额派生（compute_money / price_from_items）与输入校验的纯单元测试。"""
from decimal import Decimal

import pytest

from app.config import CNY_MAX, JPY_MAX
from app.models import MiscExpense, Order, OrderItem, ShipmentOrder
from app.models.base import price_from_items


def _mk(**kw):
    return MiscExpense(date="2026-01-01", name="t", **kw)


def test_rounding_half_up():
    m = _mk(price_cny=Decimal("1.005"), fx_rate=Decimal("1.0000"))
    m.compute_money()
    # 1.005 量化到分 = 1.01（HALF_UP），× 1.0000 = 1.01 → 取整 1
    assert m.jpy_auto == 1


def test_jpy_auto_basic():
    m = _mk(price_cny=Decimal("100"), fx_rate=Decimal("20.5"))
    m.compute_money()
    assert m.jpy_auto == 2050
    assert m.jpy_settled == 2050


def test_override_wins():
    m = _mk(price_cny=Decimal("100"), fx_rate=Decimal("20"), jpy_override=999)
    m.compute_money()
    assert m.jpy_auto == 2000
    assert m.jpy_settled == 999


def test_no_rate_no_auto():
    m = _mk(price_cny=Decimal("100"))
    m.compute_money()
    assert m.jpy_auto is None and m.jpy_settled is None


def test_override_alone_settles():
    m = _mk(jpy_override=500)
    m.compute_money()
    assert m.jpy_auto is None and m.jpy_settled == 500


def test_shipment_adds_special_fee():
    s = ShipmentOrder(date="2026-01-01", price_cny=Decimal("100"),
                      fx_rate=Decimal("20"), special_fee_jpy=1200)
    s.compute_money()
    assert s.jpy_auto == 2000 + 1200


def test_shipment_special_fee_only():
    s = ShipmentOrder(date="2026-01-01", special_fee_jpy=700)
    s.compute_money()
    assert s.jpy_auto == 700 and s.jpy_settled == 700


def test_cny_overflow_raises():
    m = _mk(price_cny=CNY_MAX + Decimal("1"), fx_rate=Decimal("20"))
    with pytest.raises(ValueError):
        m.compute_money()


# --- 派生金额的越界卡口（暂存侧没有 compute_money，必须自己卡）---------------------
# 单字段校验只管直填列：单项都合法、乘出来/加起来照样能越界。SQLite 会静默落库并让整个
# 暂存列表被 response_model 打成 422；MySQL 则 commit 时 1264 → 裸 500。

@pytest.mark.parametrize("payload", [
    pytest.param({"items": [{"name": "量大", "quantity": 1_000_000,
                             "unit_price_cny": "10000.00"}]}, id="数量×单价"),
    pytest.param({"items": [{"name": "a", "quantity": 1, "unit_price_cny": "9999999999.99"},
                            {"name": "b", "quantity": 1, "unit_price_cny": "9999999999.99"}]},
                 id="多物品求和"),
    pytest.param({"postage_cny": "9999999999.99",
                  "items": [{"name": "一元", "quantity": 1, "unit_price_cny": "1.00"}]},
                 id="光靠邮费"),
])
def test_staging_derived_price_overflow_rejected(client, session, payload):
    from sqlmodel import select

    from app.models import OrderStaging

    before = len(session.exec(select(OrderStaging)).all())
    r = client.post("/api/staging", json={"order_no": f"OVF-{payload!r:.12}", **payload})
    assert r.status_code == 422, f"越界的派生总价应当被拒绝，实际 {r.status_code}"
    session.expire_all()
    assert len(session.exec(select(OrderStaging)).all()) == before, "脏行仍然落库了"


def test_staging_list_stays_readable_after_overflow_attempt(client):
    """越界被拒之后，暂存列表必须照常打得开——脏行一旦落库，整页都会 422。"""
    client.post("/api/staging", json={
        "order_no": "OVF-LIST",
        "items": [{"name": "x", "quantity": 1_000_000, "unit_price_cny": "10000.00"}]})
    assert client.get("/api/staging").status_code == 200


def test_jpy_overflow_raises():
    m = _mk(price_cny=Decimal(JPY_MAX), fx_rate=Decimal("20"))
    with pytest.raises(ValueError):
        m.compute_money()


def test_nan_price_raises():
    m = _mk(price_cny=Decimal("NaN"), fx_rate=Decimal("20"))
    with pytest.raises(ValueError):
        m.compute_money()


def test_price_from_items():
    items = [OrderItem(name="a", quantity=3, unit_price_cny=Decimal("1.11")),
             OrderItem(name="b", quantity=1, unit_price_cny=Decimal("2.00"))]
    assert price_from_items(items) == Decimal("5.33")


def test_price_from_items_empty_and_null_prices():
    assert price_from_items([]) == Decimal("0.00")
    assert price_from_items([OrderItem(name="a", quantity=2)]) == Decimal("0.00")


def test_order_sync_adds_postage():
    o = Order(date="2026-01-01", postage_cny=Decimal("8.00"), fx_rate=Decimal("20"))
    o.items = [OrderItem(name="a", quantity=2, unit_price_cny=Decimal("10.00"))]
    o.sync_from_items()
    assert o.price_cny == Decimal("28.00")
    assert o.jpy_auto == 560


def test_freight_without_fx_is_not_masked_by_the_special_fee(client, mk):
    """集运单缺汇率时不许拿特殊费冒充结算额。

    集运的 `price_cny` 是**运费**、`special_fee_jpy` 是特殊费。缺汇率时若落到
    「auto = 特殊费」那一支，界面会显示一个看起来完整的金额，运费部分永久缺失
    并被看板加总——而商品订单同场景显示「—」，一眼看得出缺口。两者必须同口径。
    """
    from sqlmodel import Session, delete, select

    from app.database import get_engine
    from app.models import FxRate

    with Session(get_engine()) as s:            # 制造「一条汇率都没有」的前置条件
        s.exec(delete(FxRate))
        s.commit()
    try:
        j = mk("/api/shipment", {"date": "2027-01-05", "recipient": "缺汇率",
                                 "price_cny": 500, "special_fee_jpy": 3000})
        assert j["fx_rate"] is None, "前置没成立：库里还有汇率"
        assert j["jpy_settled"] is None, \
            f"运费被吞掉了，只剩特殊费冒充结算额：jpy_settled={j['jpy_settled']}"
    finally:
        pass


def test_zero_freight_plus_special_fee_still_settles(client, mk):
    """运费显式填 0（预付/包邮）+ 特殊费，是一笔算得出的账，不许被打成 None。

    这是上一条的边界：判据若写成 `price_cny is not None` 会把这种情况一起打掉，
    修一个丢钱的 bug 反而新造一个丢钱的 bug。
    """
    from sqlmodel import Session, delete

    from app.database import get_engine
    from app.models import FxRate

    with Session(get_engine()) as s:
        s.exec(delete(FxRate))
        s.commit()
    j = mk("/api/shipment", {"date": "2027-01-06", "recipient": "包邮",
                             "price_cny": 0, "special_fee_jpy": 700})
    assert j["jpy_settled"] == 700, f"运费 0 + 特殊费 700 应结算 700，实际 {j['jpy_settled']}"


def test_an_override_takes_effect_even_when_the_items_carry_no_price(client, session):
    """物品单价全是 NULL 的订单，填「覆盖（円）」必须**当场生效**。

    `sync_from_items` 里那道 `items_carry_no_price` 早退闸是对的——
    「不知道多少钱」不是「这单值 0 元」。但它原先把 `compute_money()` 一起圈了进去，
    而重算日元只看 `price_cny` / `fx_rate` / `jpy_override`，**与物品有没有单价无关**。

    后果很具体：HTTP 200、响应里 `jpy_override` 是新值而 `jpy_settled` 还是旧值。
    而「结算（円）」与「覆盖（円）」在订单页是**并排显示**的两列——
    用户同时看到「覆盖 3500」和「结算 6000」，看板也仍按 6000 算。
    改汇率、`stamp_fx` 自愈存量脏行，当时同样一并被跳过。

    这种订单不是构造出来的边角：`f6a7b8c9d0e1` 只加列不回填，
    `app/demo.py` 今天造的仍然是「有名称有数量、单价 NULL」的物品。

    判据落在 `jpy_settled` 上，不是 `jpy_override`——后者只是把入参回显一遍，
    存进去了不等于结算跟着动了，而**结算才是看板和页脚用的那个数**。
    """
    import datetime as dt

    from app.models import Order, OrderItem

    o = Order(date=dt.date(2027, 3, 1), title="历史形态", order_no="OVR-NOPRICE",
              price_cny=300, fx_rate=20, purchase_status="待收货")
    o.items = [OrderItem(name="甲", quantity=1), OrderItem(name="乙", quantity=2)]
    o.compute_money()
    session.add(o)
    session.commit()
    session.refresh(o)
    assert o.jpy_settled == 6000, f"夹具没造对：{o.jpy_settled}"
    assert all(i.unit_price_cny is None for i in o.items), "夹具的物品带了单价，前提不成立"

    r = client.patch(f"/api/orders/{o.id}", json={"version": o.version, "jpy_override": 3500})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jpy_settled"] == 3500, (
        f"覆盖存进去了但结算没动（override={body.get('jpy_override')}、"
        f"settled={body.get('jpy_settled')}）——订单页会并排显示这两个互相矛盾的数")


def test_importing_a_staging_row_never_loses_its_money(client, session):
    """暂存行有价、物品单价全 NULL 时，导入之后**钱必须还在**。

    建单原先完全指望 `order.sync_from_items()` 从物品派生货款
    （旧注释：「订单价由物品派生（= 暂存价，一致）」）——那句话只在物品带单价时成立。
    单价全 NULL 时它什么都不派生，于是这张单**从来没有过价**：
    暂存页明明显示 ¥45.00，导入之后订单页和暂存页双双变成「—」
    （暂存已导入行显示的是账本实时值），`jpy_auto` / `jpy_settled` 全是 None。

    最难查的是第三处：看板那个「被吞掉的钱」告警判据是「有 `price_cny` 却没折算」，
    而这里 `price_cny` 也是 None ⇒ **它报 0**。钱在三个地方同时消失，没有一处会响。

    判据同时钉 `price_cny` 与 `jpy_settled`：只钉前者的话，
    把货款带过去却不重算日元照样绿，而用户看的是日元。
    """
    import datetime as dt

    from sqlmodel import select

    from app.models import Order, OrderStaging, StagingItem

    st = OrderStaging(order_date=dt.date(2027, 3, 2), order_no="IMP-NOPRICE",
                      price_cny=45, fx_rate=20, purchase_status="待收货")
    st.items = [StagingItem(name="色纸", quantity=2), StagingItem(name="明信片", quantity=1)]
    session.add(st)
    session.commit()
    session.refresh(st)
    assert all(i.unit_price_cny is None for i in st.items), "夹具的物品带了单价，前提不成立"

    assert client.post(f"/api/staging/{st.id}/import").status_code == 200

    o = session.exec(select(Order).where(Order.order_no == "IMP-NOPRICE")).one()
    assert o.price_cny is not None and int(o.price_cny) == 45, (
        f"暂存 ¥45 导入之后订单价是 {o.price_cny}——钱在订单页、暂存页、"
        f"以及看板的「被吞掉的钱」告警三处同时消失")
    assert o.jpy_settled == 900, f"货款带过去了但没重算日元：settled={o.jpy_settled}"


def test_renaming_an_item_on_a_priceless_order_does_not_zero_the_money(client, session):
    """物品单价全 NULL 的历史订单，**改一个物品名不许把钱变成 0**。

    `items_carry_no_price` 的 docstring 承诺的正是这件事（「在订单页对这样一张单做
    **任何一次** PATCH……货款当场从 ¥300 变成 ¥0」），但那道闸只挂在 `sync_from_items`
    那一层——**带 items 的 PATCH 会先过 `build_items`**，而它把「没单价」重新编码成
    `(0.00, auto=True)`（那段注释自己写着「没给种子就是不知道单价，一律记 0 + auto」）。
    等到闸那里 NULL 已经没了，闸形同虚设。

    2026-09-02 实测（修之前）：¥320.00 / 6400 円 的单，在展开面板里改一个物品名，
    保存后变成 **¥0.00 / 0 円**，HTTP 200、零提示、再编辑一次也回不来。

    这种订单不是构造出来的边角：`f6a7b8c9d0e1` 只加列不回填，
    `app/demo.py` 今天造的仍然是这个形态。
    """
    import datetime as dt

    from app.models import Order, OrderItem

    o = Order(date=dt.date(2027, 5, 1), title="历史形态", order_no="RENAME-KEEPS-MONEY",
              price_cny=320, fx_rate=20, purchase_status="待收货")
    o.items = [OrderItem(name="甲", quantity=1), OrderItem(name="乙", quantity=2)]
    o.compute_money()
    session.add(o)
    session.commit()
    session.refresh(o)
    assert o.jpy_settled == 6400 and all(i.unit_price_cny is None for i in o.items), "夹具没造对"

    r = client.patch(f"/api/orders/{o.id}", json={"version": o.version, "items": [
        {"name": "甲改名", "quantity": 1, "unit_price_cny": None, "auto": False},
        {"name": "乙", "quantity": 2, "unit_price_cny": None, "auto": False},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jpy_settled"] == 6400, (
        f"改了个物品名，结算从 6400 变成 {body['jpy_settled']}——"
        f"`build_items` 把 NULL 压成 0.00 之后，那道「派生不出就别动」的闸看不见它了")
    assert str(body["price_cny"]) .startswith("320"), f"货款也变了：{body['price_cny']}"


def test_deliberately_clearing_every_price_still_zeroes_the_order(client):
    """**反面，而且不能省**：用户把单价一个个删掉再保存，货款仍然必须归零。

    这两件事送来的 payload **形状完全相同**（items 都不带 `unit_price_cny`），
    分辨它们的唯一信号是**替换之前的存量状态**：
      · 存量有价 + 传来无价 = 主动清空 → 归零（既定决定，与「只清空一条」同口径）
      · 存量本来就无价 + 传来无价 = 价格没被碰过 → 一个字都不动

    只钉上一条的话，「凡是传来无价就不动货款」也能过，
    而那会把 `test_clearing_all_prices_zeroes_them` 那条既定行为推翻掉。
    """
    from decimal import Decimal

    o = client.post("/api/orders", json={
        "date": "2027-05-02", "title": "正常单", "order_no": "CLEARED-TO-ZERO",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10.00"},
                  {"name": "b", "quantity": 1, "unit_price_cny": "20.00"}]}).json()
    assert Decimal(o["price_cny"]) == Decimal("30.00"), o

    body = client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "a", "quantity": 1}, {"name": "b", "quantity": 1}]}).json()
    assert Decimal(body["price_cny"]) == Decimal("0.00"), (
        f"主动清空全部单价之后货款应归零（待补价），实际 {body['price_cny']}")

