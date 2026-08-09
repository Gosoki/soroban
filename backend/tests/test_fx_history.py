"""一天多条汇率：怎么存、怎么取、怎么看。

原先 `fxrate.date` 是唯一键，一天只留一条、后写的覆盖先写的。两个问题：

  1. **补录会用错汇率**。爬虫抓回几天前买的东西，该按**那一天**折算；
     而那一天可能抓过好几次，只留一条就是强行抹掉当天的变动。
  2. **回看不了**。「那天几点是多少」查不到——出账对不上时无从复核。

改成追加式：写侧只管记，**取哪一条是读侧的事**（`pick_on`）。
"""
import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import select

from app.models import FxRate
from app.services import fx


@pytest.fixture(autouse=True)
def _clean(session):
    for r in session.exec(select(FxRate)).all():
        session.delete(r)
    session.commit()
    yield


def _add(session, d, rate, source="boc", hours_ago=0):
    row = FxRate(date=d, rate=Decimal(rate), source=source,
                 fetched_at=fx.utcnow() - dt.timedelta(hours=hours_ago))
    session.add(row)
    session.commit()
    return row


def test_same_day_can_hold_many_rows(session):
    """唯一约束去掉了——同一天可以记多条。"""
    d = dt.date(2026, 5, 1)
    _add(session, d, "23.1", hours_ago=6)
    _add(session, d, "23.4", hours_ago=1)
    assert len(fx.rows_on(session, d)) == 2


def test_store_appends_instead_of_overwriting(session):
    """`store` 每次追加。覆盖式写入会把当天早些时候的值抹掉，
    而那正是「回看那天几点是多少」需要的东西。"""
    d = dt.date(2026, 5, 2)
    fx.store(session, Decimal("23.1"), "boc", on=d)
    fx.store(session, Decimal("23.9"), "boc", on=d)
    session.commit()
    assert len(fx.rows_on(session, d)) == 2


def test_pick_takes_the_last_of_the_day(session):
    """没有手填时，取当天**最后抓到**的那条（按 fetched_at，不是插入顺序）。"""
    d = dt.date(2026, 5, 3)
    _add(session, d, "23.1", hours_ago=8)
    _add(session, d, "23.9", hours_ago=1)     # 最后抓到
    _add(session, d, "23.5", hours_ago=4)     # 插入最晚，但抓取更早
    assert fx.pick_on(session, d).rate == Decimal("23.9000")


def test_manual_wins_within_the_day(session):
    """人 > 机器：当天手填过就用手填的，哪怕之后又抓到新的。

    与 `can_advance_purchase` 是同一条原则。一天多条之后，这条规则必须落在**读**这一侧——
    写侧只管追加，否则「手填之后自动抓一次就被顶掉」。
    """
    d = dt.date(2026, 5, 4)
    _add(session, d, "23.1", "boc", hours_ago=8)
    _add(session, d, "20.0", "manual", hours_ago=5)
    _add(session, d, "23.9", "boc", hours_ago=1)      # 手填之后又抓到
    assert fx.pick_on(session, d).rate == Decimal("20.0000")
    assert fx.pick_on(session, d).source == "manual"


def test_backfilled_order_uses_that_days_rate(client, session):
    """**这条是本次改动的正题**：爬虫抓回前几天买的东西，按那一天折算。"""
    old = dt.date.today() - dt.timedelta(days=3)
    _add(session, old, "20.0", hours_ago=72)
    _add(session, dt.date.today(), "30.0", hours_ago=1)      # 今天的汇率完全不同

    r = client.post("/api/orders", json={
        "date": old.isoformat(), "order_no": "BACKFILL-1", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert Decimal(got["fx_rate"]) == Decimal("20.0000"), \
        f"补录的单该按下单那天折算，实际用了 {got['fx_rate']}"
    assert got["jpy_settled"] == 200


def test_day_without_any_rate_falls_back_to_latest(client, session):
    """那天一条都没有（比如补录很久以前的单）→ 退回全库最新的一条，而不是留空。"""
    _add(session, dt.date.today(), "25.0", hours_ago=1)
    r = client.post("/api/orders", json={
        "date": "2020-01-01", "order_no": "BACKFILL-2", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]})
    assert Decimal(r.json()["fx_rate"]) == Decimal("25.0000")


def test_latest_stored_is_deterministic(session):
    """全库最新要按 (日期, 抓取时刻) 两级排。只按 date 排的话，同一天多条里返回哪条
    取决于数据库实现，表现是「刷新一下汇率就变了」——最难查的那种不确定性。"""
    d = dt.date(2026, 5, 5)
    _add(session, d, "23.1", hours_ago=8)
    _add(session, d, "23.9", hours_ago=1)
    for _ in range(5):
        assert fx.latest_stored(session).rate == Decimal("23.9000")


# --- 汇率页的两个接口 -----------------------------------------------------------

def test_history_groups_by_day(client, session):
    d = dt.date.today() - dt.timedelta(days=1)
    _add(session, d, "23.1", hours_ago=30)
    _add(session, d, "23.9", hours_ago=25)
    got = client.get("/api/fx/history", params={"days": 7}).json()["items"]
    row = next(x for x in got if x["date"] == d.isoformat())
    assert row["count"] == 2
    assert Decimal(str(row["low"])) == Decimal("23.1") and Decimal(str(row["high"])) == Decimal("23.9")


def test_history_used_matches_what_orders_actually_get(client, session):
    """页面上标的「采用」必须和建单时真正用的是**同一个函数**算出来的。

    另算一遍的话，页面显示的和账本里用的会是两个数，而那种不一致要等到对账才发现。
    """
    d = dt.date.today()
    _add(session, d, "23.1", "boc", hours_ago=8)
    _add(session, d, "19.0", "manual", hours_ago=5)
    _add(session, d, "23.9", "boc", hours_ago=1)
    shown = next(x for x in client.get("/api/fx/history").json()["items"]
                 if x["date"] == d.isoformat())
    o = client.post("/api/orders", json={
        "date": d.isoformat(), "order_no": "USED-1", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]}).json()
    assert Decimal(str(shown["used"])) == Decimal(str(o["fx_rate"])), "页面标的「采用」与建单实际用的不是同一个值"


def test_day_detail_marks_the_used_row_and_uses_jst(client, session):
    """明细里的时刻按 **JST** 给——`date` 是 JST 日期，两者不同源的话，
    非 JST 机器上会出现「2026-08-07 那天的明细写着 8/6 22:00」，同一行两个时区。"""
    d = dt.date.today()
    _add(session, d, "23.1", "boc", hours_ago=6)
    _add(session, d, "19.0", "manual", hours_ago=3)
    got = client.get(f"/api/fx/history/{d.isoformat()}").json()["items"]
    assert [x["used"] for x in got].count(True) == 1, "必须且只能标出一条「采用」"
    assert next(x for x in got if x["used"])["source"] == "manual"
    for x in got:
        assert len(x["at"]) == 5 and x["at"][2] == ":", f"抓取时刻不是 HH:MM：{x['at']!r}"


def test_empty_rate_response_still_says_who_provides(client, session):
    """库里一条汇率都没有时，`GET /api/fx` **仍要**说清有没有插件在供给。

    `_read` 有一条「没有行就早退」的分支，它漏掉过 `auto_provider`——
    而那恰恰是最需要这个字段的时刻：设置页靠它在「有插件但还没跑」与
    「压根没装插件」之间说对话，漏掉就永远显示成后者。

    讽刺的是 `_read` 的 docstring 自己写着「抄两遍迟早有一边忘了加新字段」，
    然后就是那条早退分支忘了。
    """
    for r in session.exec(select(FxRate)).all():
        session.delete(r)
    session.commit()
    got = client.get("/api/fx").json()
    assert got["rate"] is None
    assert "auto_provider" in got, "没有汇率时的响应缺了 auto_provider"


def test_both_branches_of_read_return_the_same_fields(client, session):
    """两条分支（有行 / 无行）返回的字段集必须一致。

    早退分支少一个字段，表现是「某个状态下界面上少一块东西」，而那个状态往往
    正是最少被测到的那个。这条比逐字段断言更耐改。
    """
    for r in session.exec(select(FxRate)).all():
        session.delete(r)
    session.commit()
    empty = set(client.get("/api/fx").json())

    _add(session, dt.date.today(), "23.5")
    filled = set(client.get("/api/fx").json())
    assert empty == filled, f"两条分支字段不一致，无行时少了：{sorted(filled - empty)}"


def test_pick_from_does_not_depend_on_the_callers_ordering(session):
    """`pick_from` 自己排序——**乱序传进去也要挑对**。

    原先的契约是「要求 rows 已按 fetched_at 倒序」，而契约两端在不同文件
    （`rows_on` 与 `routers/fx.py` 的按天汇总各自写 ORDER BY）。
    谁哪天改了那句 ORDER BY，建单就会安静地用上当天**第一条**汇率而不是最后一条，
    而所有 `pick_from` 的单元测试仍然全绿——因为它们自己传的是有序的 rows。
    这是金额相关的隐患，所以不变量必须由函数自己保证。
    """
    import datetime as dt
    from decimal import Decimal

    from app.models import FxRate
    from app.services.fx import pick_from

    d = dt.date(2026, 5, 1)
    base = dt.datetime(2026, 5, 1, 0, 0, tzinfo=dt.timezone.utc)
    rows = [
        FxRate(id=1, date=d, rate=Decimal("20.00"), source="boc", fetched_at=base),
        FxRate(id=3, date=d, rate=Decimal("22.00"), source="boc",
               fetched_at=base + dt.timedelta(hours=6)),          # 当天最后一条
        FxRate(id=2, date=d, rate=Decimal("21.00"), source="boc",
               fetched_at=base + dt.timedelta(hours=3)),
    ]
    assert pick_from(rows).rate == Decimal("22.00")               # 乱序也挑最后一条
    assert pick_from(list(reversed(rows))).rate == Decimal("22.00")

    # 手填仍然最优先，且同样不看传入顺序
    manual = FxRate(id=0, date=d, rate=Decimal("19.00"), source="manual", fetched_at=base)
    assert pick_from([*rows, manual]).rate == Decimal("19.00")
    assert pick_from([manual, *rows]).rate == Decimal("19.00")


def test_pick_from_breaks_ties_deterministically(session):
    """同一时刻抓到两条时，结果必须是确定的（原先取决于数据库实现）。"""
    import datetime as dt
    from decimal import Decimal

    from app.models import FxRate
    from app.services.fx import pick_from

    d = dt.date(2026, 5, 2)
    t = dt.datetime(2026, 5, 2, 1, 0, tzinfo=dt.timezone.utc)
    a = FxRate(id=10, date=d, rate=Decimal("20.00"), source="boc", fetched_at=t)
    b = FxRate(id=11, date=d, rate=Decimal("21.00"), source="boc", fetched_at=t)
    assert pick_from([a, b]).rate == pick_from([b, a]).rate == Decimal("21.00")


def test_pick_from_survives_naive_timestamps(session):
    """SQLite 取回的时间戳可能是 naive；与 aware 混排会 TypeError。"""
    import datetime as dt
    from decimal import Decimal

    from app.models import FxRate
    from app.services.fx import pick_from

    d = dt.date(2026, 5, 3)
    naive = FxRate(id=1, date=d, rate=Decimal("20.00"), source="boc",
                   fetched_at=dt.datetime(2026, 5, 3, 1, 0))
    aware = FxRate(id=2, date=d, rate=Decimal("21.00"), source="boc",
                   fetched_at=dt.datetime(2026, 5, 3, 2, 0, tzinfo=dt.timezone.utc))
    assert pick_from([naive, aware]).rate == Decimal("21.00")


def test_displayed_rate_is_the_one_a_new_order_would_use(client, session):
    """侧栏/看板/设置页显示的「当前汇率」必须**就是**建单会用的那一条。

    典型分叉：用户今天手填 21.00，之后汇率插件抓了一条 20.50（fetched_at 更晚）。
    `latest_stored` 给 20.50（全库最新），而建单走 `pick_on` 拿 21.00（手填优先）。
    显示 20.50、实际用 21.00 —— 用户手填的本意恰恰是「用我这个值」，
    显示层无视手填是这套规则里唯一说话不算数的地方。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import delete

    from app.models import FxRate
    from app.services.fx import JST, rate_for_date

    session.exec(delete(FxRate))
    session.commit()
    today = dt.datetime.now(JST).date()
    now = dt.datetime.now(dt.timezone.utc)
    session.add(FxRate(date=today, rate=Decimal("21.0000"), source="manual",
                       fetched_at=now - dt.timedelta(hours=2)))
    session.add(FxRate(date=today, rate=Decimal("20.5000"), source="boc",
                       fetched_at=now))                       # 更晚抓到，但不是手填
    session.commit()

    shown = Decimal(client.get("/api/fx").json()["rate"])
    used = rate_for_date(session, today)
    assert used == Decimal("21.0000"), "建单没走手填优先——前提变了，这条测试要重写"
    assert shown == used, f"界面显示 {shown}，建单实际用 {used}——两个数"
