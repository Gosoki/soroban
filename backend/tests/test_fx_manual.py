"""手填汇率（补历史）：`POST /api/fx`。

系统里唯一一条**写**汇率的人工路径。自动获取归汇率插件，插件走通用写入通道的 `fx_rate`，
且明文禁止用 `manual` 这个源名——所以这条端点对插件是关闭的（见 test_plugin_paths）。
"""
# --- 手填历史汇率 ---------------------------------------------------------------

def test_manual_rate_can_be_filled_for_a_past_day(client, session):
    """补录上个月的支出要按**那一天**的牌价折算——所以必须能填过去的日期。"""
    import datetime as dt

    from app.services.fx import JST, pick_on

    past = dt.datetime.now(JST).date() - dt.timedelta(days=45)
    r = client.post("/api/fx", json={"date": past.isoformat(), "rate": "21.5"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "manual" and r.json()["source_label"] == "手填"
    picked = pick_on(session, past)
    assert picked is not None and str(picked.rate).startswith("21.5"), picked


def test_manual_rate_refuses_a_future_day(client):
    """「明天的汇率」不存在。填了只会让明天选中一个凭空捏造的值。"""
    import datetime as dt

    from app.services.fx import JST

    future = dt.datetime.now(JST).date() + dt.timedelta(days=1)
    r = client.post("/api/fx", json={"date": future.isoformat(), "rate": "21.5"})
    assert r.status_code == 422, r.text
    assert "未来" in r.json()["detail"]


def test_manual_rate_goes_through_the_same_sanity_check(client):
    """区间校验与插件报上来的汇率同一套（1元≈20円，越界即脏数据）。"""
    import datetime as dt

    from app.services.fx import JST

    today = dt.datetime.now(JST).date().isoformat()
    for bad in ("0.5", "999", "-3"):
        r = client.post("/api/fx", json={"date": today, "rate": bad})
        assert r.status_code == 422, f"{bad} 竟然收下了：{r.text}"
        assert "合理区间" in r.json()["detail"], r.text


def test_filling_a_rate_never_touches_already_settled_orders(client, session):
    """已经折算过的旧单**不许**被改——那些行盖的是成交当时的汇率。

    事后改汇率去动它们是篡改账目，不是修复。这条钉住「手填只追加一行」这个边界。
    """
    import datetime as dt

    from sqlmodel import select

    from app.models import Order
    from app.services.fx import JST

    day = dt.datetime.now(JST).date() - dt.timedelta(days=3)
    client.post("/api/fx", json={"date": day.isoformat(), "rate": "20.0"})
    r = client.post("/api/orders", json={"date": day.isoformat(), "title": "旧单",
                                         "price_cny": "100", "purchase_status": "待收货"})
    assert r.status_code in (200, 201), r.text
    before = client.get(f"/api/orders/{r.json()['id']}").json()
    assert before["fx_rate"] is not None and before["jpy_settled"]

    client.post("/api/fx", json={"date": day.isoformat(), "rate": "30.0"})
    after = client.get(f"/api/orders/{r.json()['id']}").json()
    assert after["fx_rate"] == before["fx_rate"], "旧单的汇率被改了"
    assert after["jpy_settled"] == before["jpy_settled"], "旧单的日元金额被改了"


def test_the_two_paths_that_store_a_rate_round_it_the_same_way():
    """手填与插件回灌是存汇率的两条路，**同一个输入必须得到同一个数**。

    `services/fx._sane` 曾是全仓唯一一个不带 `rounding=` 的 `quantize()`
    （于是用 decimal 默认的 `ROUND_HALF_EVEN`），而 `schemas._q_fx` 用 `ROUND_HALF_UP`。
    `20.00005` 走手填存成 20.0001、走插件存成 20.0000。

    差在第 4 位小数、金额层面几乎看不出来——正因为看不出来才更该钉住。
    判据是**两条路的输出相等**，不是「源码里有没有 ROUND_HALF_UP」：
    后者换个写法就失效，前者才是真正要成立的东西。
    """
    from decimal import Decimal

    from app.schemas import _q_fx
    from app.services.fx import _sane

    for raw in ("20.00005", "20.00015", "19.99995", "23.86425"):
        v = Decimal(raw)
        assert _sane(v) == _q_fx(v), (
            f"同一个汇率 {raw} 两条路存成了两个数：插件侧 {_sane(v)} / 手填侧 {_q_fx(v)}")
