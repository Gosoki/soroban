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


def test_saving_a_manual_rate_really_does_override_todays_fetched_one(client, session, request):
    """保存手填汇率**会顶掉当天已抓到的那个值**——这是有意的行为，说明必须照实说。

    保存设置走 `record_manual_rate`：无条件给今天追加一条 `source=manual`，
    而手填在 `pick_on` 里优先于机器抓的。所以填一个数 = 从现在起按它折算。

    而那段 hint 原先描述的是**另一个函数**（`ensure_manual_rate`）的语义：
    「没装插件时库里一条汇率都没有……首次需要汇率时会按它记一条」——
    照字面读就是「只在什么都没有时才用」的备胎。
    2026-09-02 实测：插件今天已抓到 20.50，用户照着那段话填 18 当兜底、保存，
    **当天新建的单当场从 2050 円 变成 1800 円**，页面只说「已保存，即时生效」。

    这条钉两半：
      · **行为**——顶掉是对的，别哪天有人把它「修」成只在空库时生效
        （那会让「我明明填了却不生效」变成新的困惑）；
      · **文案**——说明里必须出现「优先」这层意思。
        只钉行为的话，那段假话可以原样留着。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import delete

    from app.models import FxRate
    from app.services import fx, prefs

    session.exec(delete(FxRate))
    session.commit()
    fx.store(session, Decimal("20.50"), "boc")     # 插件今天已经抓到
    session.commit()
    # **跑完要还原，而且要还原的是「设置」不只是「汇率行」。**
    # 第一版只清了 FxRate，`test_money.py` 那条「没有汇率时运费不该被特殊费掩盖」
    # 照样红——因为 `fx.manual_rate` 还停在 "18"，它清空汇率表之后
    # `ensure_manual_rate` 又按 18 补了一条。**污染源是设置，不是数据行。**
    # 红的地方（test_money）不是错的地方（这里），最难查的那一种。
    def _restore():
        prefs.save(session, {"fx.manual_rate": ""})
        session.exec(delete(FxRate))
        session.commit()

    request.addfinalizer(_restore)

    today = dt.datetime.now(fx.JST).date().isoformat()
    before = client.post("/api/orders", json={
        "date": today, "title": "填之前", "price_cny": "100.00"}).json()
    assert before["jpy_settled"] == 2050, before

    assert client.put("/api/settings",
                      json={"values": {"fx.manual_rate": "18"}}).status_code == 200
    after = client.post("/api/orders", json={
        "date": today, "title": "填之后", "price_cny": "100.00"}).json()
    assert after["jpy_settled"] == 1800, (
        f"手填汇率没有顶掉当天抓到的那个值（{after['jpy_settled']}）——"
        f"那会让「我明明填了却不生效」变成新的困惑")

    hint = prefs.SPECS["fx.manual_rate"].hint
    assert "优先" in hint, (
        f"说明里没提「手填优先于插件抓的」这层意思，用户会把它当成用不上的备胎：\n{hint}")


def test_the_manual_tag_does_not_promise_an_update_that_cannot_happen(client, session):
    """侧栏「手填」标签的提示，必须与「当天插件写不进去」这件事对得上。

    行为是有意的（人 > 机器，与 `can_advance_purchase` 同一条原则）：
    当天有手填汇率时，插件的写入被记成 `unchanged`「当天已有手填汇率，不被自动源覆盖」。
    也就是**插件跑通、退出码 0、库里一行都没多**。

    而那个标签原先的提示写的是「装上汇率插件并授权后**会自动更新**」——
    用户照做（装插件、勾 fx:write、开开关、点抓取），侧栏那个数字和「手填」标签
    一整天纹丝不动。他能得到的唯一结论是「插件装坏了」，于是去重建 venv、重下浏览器内核。

    这条钉两半，缺一不可：
      · **行为**——当天有手填时，自动源写入必须回 `unchanged` 且说清原因
        （这条行为此前一条守卫都没有）；
      · **文案**——提示里必须说清「你填的那一天不会更新」，
        否则那句承诺仍然是假的。
    """
    import datetime as dt
    from decimal import Decimal
    from pathlib import Path

    from sqlmodel import delete

    from app.models import FxRate
    from app.plugins import scopes
    from app.services import fx

    # **跑完要还原。** 这两条都往 FxRate 里留行，而「今天有没有汇率／是哪一条」
    # 是后面好几条用例的前提（`test_money.py` 里那条「没有汇率时运费不该被特殊费掩盖」
    # 就是这么被我弄红的——红的地方不是错的地方，最难查的那一种）。
    def _wipe():
        session.exec(delete(FxRate))
        session.commit()

    _wipe()
    today = dt.datetime.now(fx.JST).date()
    fx.store(session, Decimal("18.00"), fx.SOURCE_MANUAL, on=today)   # 用户今天手填过
    session.commit()

    class _U:
        id, username = 1, "admin"

    tok, jti = scopes.issue(_U(), "fx-probe", {"fx:write"})
    try:
        r = client.post("/api/plugins/ingest",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"kind": "fx.rate", "items": [
                            {"date": today.isoformat(), "rate": "20.50", "source": "boc"}]})
        assert r.status_code == 200, r.text
        one = r.json()["results"][0]
        assert one["status"] == "unchanged", (
            f"当天有手填汇率时，自动源写入应被记成 unchanged，实际 {one}")
        assert "手填" in one["message"], f"回执没说清为什么：{one}"
    finally:
        scopes.revoke(jti)

    _wipe()

    layout = (Path(__file__).resolve().parents[2] / "frontend" / "src"
              / "components" / "Layout.vue").read_text(encoding="utf-8")
    tag = layout[layout.index("fx.source === 'manual'"):][:600]
    assert "那一天" in tag or "当天" in tag, (
        "「手填」标签的提示没说清「你填的那一天插件不会覆盖它」——"
        "用户照着「装上插件就会自动更新」去做，会以为插件装坏了")

