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
