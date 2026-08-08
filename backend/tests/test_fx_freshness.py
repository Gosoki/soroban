"""汇率的新鲜度：过期要**看得见**，不能安静地照用。

这是把汇率获取搬去插件之前必须先补的一条。理由：搬走之后获取从「进程内、与后端同生共死」
变成「独立子进程、可能默默不跑」——失败概率上升，而失败的可见度如果不变（=零），
那就是纯粹把风险加大。上一轮 `run_in_threadpool` 漏导入让定时抓取整整一轮没跑过、
而测试全绿，就是这种「安静失败」的实例。

分两级，刻意的：
  · `stale`   —— 不是今天的。日粒度，很常见（凌晨还没刷新），黄标提示即可。
  · `expired` —— 超过 `fx.stale_hours`。意味着取汇率的链路真的断了，红标 + 建单记警告。

**过期仍然照用**，不返回 None：一个两天前的真实汇率比「没有」更接近事实，而且订单会把
当时用的汇率逐行存下来、事后可审计可改。代价是它安静——所以才有下面这些断言。
"""
import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import select

from app.models import FxRate
from app.services import fx, prefs


@pytest.fixture(autouse=True)
def _clean_rates(session):
    """每个用例开始前清空 FxRate。

    `latest_stored` 按日期取最新——别的用例（甚至本文件前一个用例）留下的「今天」那行
    会一直是最新，于是这里造的「4 天前」根本轮不到。第一版就是这么假红的：
    断言查到的是上一个用例的行。
    """
    for row in session.exec(select(FxRate)).all():
        session.delete(row)
    session.commit()
    yield


def _put_rate(session, *, hours_ago: float, rate="20.0", source="boc", date=None):
    """直接落一条指定「多久以前取到」的汇率行。"""
    d = date or (dt.datetime.now(fx.JST).date() - dt.timedelta(days=int(hours_ago // 24)))
    row = session.exec(select(FxRate).where(FxRate.date == d)).first()
    fetched = fx.utcnow() - dt.timedelta(hours=hours_ago)
    if row:
        row.rate, row.source, row.fetched_at = Decimal(rate), source, fetched
    else:
        row = FxRate(date=d, rate=Decimal(rate), source=source, fetched_at=fetched)
    session.add(row)
    session.commit()
    return row


def test_age_is_measured_from_fetched_at(session):
    row = _put_rate(session, hours_ago=5)
    age = fx.rate_age_hours(row)
    assert 4.5 < age < 5.5, f"算出来的年龄是 {age}，与实际不符"


def test_naive_timestamp_does_not_blow_up(session):
    """SQLite 取回的时间戳可能是 naive。拿它和带时区的 now 相减会 TypeError，
    整个建单路径当场 500——这条守着那次转换。"""
    row = _put_rate(session, hours_ago=3)
    row.fetched_at = row.fetched_at.replace(tzinfo=None)
    assert fx.rate_age_hours(row) is not None


def test_expired_follows_the_setting(session):
    prefs.save(session, {"fx.stale_hours": 10})
    assert not fx.is_expired(session, _put_rate(session, hours_ago=5))
    assert fx.is_expired(session, _put_rate(session, hours_ago=20))
    prefs.save(session, {"fx.stale_hours": 48})     # 还原，免得影响后面的用例


def test_expired_rate_is_still_served(session):
    """过期**不**等于拒绝供给。拒绝会让建单直接失去日元金额，那更糟。"""
    prefs.save(session, {"fx.stale_hours": 1})
    _put_rate(session, hours_ago=100, rate="19.5")
    assert fx.current_rate(session) is not None, "过期就不给值了？建单会整批失去日元金额"
    prefs.save(session, {"fx.stale_hours": 48})


def test_stamp_rate_warns_when_expired(session, caplog):
    """核心：用了过期汇率必须留痕。没有这条，链路断掉时账本会安静地一路记错。"""
    prefs.save(session, {"fx.stale_hours": 1})
    _put_rate(session, hours_ago=100, rate="18.0")
    with caplog.at_level("WARNING"):
        got = fx.stamp_rate(session, "建商品订单 TEST-1")
    assert got == Decimal("18.0000") or got == Decimal("18.0")
    # 用 getMessage()：日志是 %-风格惰性格式化，直接对 r.message 做 % r.args 会在
    # 参数个数对不上时抛 TypeError（第一版就是这么炸的），而且 r.message 本身是模板不是成品。
    assert any("过期" in r.getMessage() for r in caplog.records), "用了过期汇率却没有任何警告"
    prefs.save(session, {"fx.stale_hours": 48})


def test_stamp_rate_is_quiet_when_fresh(session, caplog):
    """反面：新鲜时不该刷警告——狼来了喊多了就没人看了。"""
    prefs.save(session, {"fx.stale_hours": 48})
    _put_rate(session, hours_ago=1, rate="20.5")
    with caplog.at_level("WARNING"):
        fx.stamp_rate(session, "建商品订单 TEST-2")
    assert not [r for r in caplog.records if "过期" in r.getMessage()], "汇率还新鲜却报了过期"


def test_api_exposes_age_and_expired(client, session):
    """前端要靠这两个字段把「已过期多久」显示出来。
    只有 `stale`（日粒度）的话，1 天前和 3 个月前长得一模一样。"""
    prefs.save(session, {"fx.stale_hours": 1})
    _put_rate(session, hours_ago=100)
    got = client.get("/api/fx").json()
    assert got["expired"] is True
    assert got["age_hours"] and got["age_hours"] > 90
    prefs.save(session, {"fx.stale_hours": 48})


def test_every_rate_stamping_path_can_warn():
    """**所有**给账本行盖汇率的地方都必须走带 `what` 的告警版本。

    原先只有建商品订单/建集运订单两处走 `stamp_rate`，另外四条（杂项建单、三张表 PATCH
    补价、暂存自身补价、暂存导入建订单）直接 `rate_for_date(session, date)` —— 不触发手填
    兜底、不告警。于是「没装插件也自洽」在**插件的主入账路径上根本不成立**：
    暂存导入是爬虫抓完之后的必经之路，恰恰是最需要兜底的那条。

    判据用 AST 看实参，不 grep 文本（注释里必然写到这些函数名）。
    """
    import ast
    import inspect
    import textwrap

    from app.routers import common, orders, shipment, staging

    bad = []
    for mod in (orders, shipment, common, staging):
        tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            fn = node.func.id
            if fn == "current_rate":
                bad.append(f"{mod.__name__}:{node.lineno} 直接调 current_rate（过期不留痕）")
            elif fn == "rate_for_date":
                if not any(k.arg == "what" for k in node.keywords):
                    bad.append(f"{mod.__name__}:{node.lineno} rate_for_date 没传 what（不兜底也不告警）")
            elif fn == "stamp_rate":
                if len(node.args) < 2:
                    bad.append(f"{mod.__name__}:{node.lineno} stamp_rate 少了 what")
    assert not bad, "这些盖汇率的路径不会喊：\n  " + "\n  ".join(bad)


def test_import_from_staging_falls_back_to_manual_rate(client, session):
    """行为级：库里一条汇率都没有 + 设了手填值 → 暂存导入建出来的订单**有**汇率。

    这条是「不装插件也能记账」的端到端证明。它走的是爬虫抓完之后的必经路径，
    而那条路径此前完全不触发兜底。
    """
    from app.services import prefs

    prefs.save(session, {"fx.manual_rate": "20.5"})
    r = client.post("/api/staging", json={
        "order_date": "2026-07-01", "order_no": "FXFB-1", "platform": "淘宝", "title": "t",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert client.post(f"/api/staging/{sid}/import").status_code == 200

    got = next(x for x in client.get("/api/orders", params={"limit": 200}).json()["items"]
               if x["order_no"] == "FXFB-1")
    assert got["fx_rate"] is not None, "没装插件、设了手填汇率，导入的订单却没有汇率"
    assert Decimal(got["fx_rate"]) == Decimal("20.5")
    assert got["jpy_settled"], "有汇率却没算出日元——看板会静默少算这一笔"
    prefs.save(session, {"fx.manual_rate": ""})


def test_no_manual_rate_means_no_silent_zero(client, session):
    """反面：没设手填值也没有插件时，订单的日元金额是 **None**（空着），不是 0。

    None 会在界面上留白、看板笔数与金额对不上，人看得见；0 会被静默加进合计，
    看起来像一笔真实的「零元订单」。
    """
    from app.services import prefs

    prefs.save(session, {"fx.manual_rate": ""})
    r = client.post("/api/staging", json={
        "order_date": "2026-07-02", "order_no": "FXFB-2", "platform": "淘宝", "title": "t",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]})
    sid = r.json()["id"]
    client.post(f"/api/staging/{sid}/import")
    got = next(x for x in client.get("/api/orders", params={"limit": 200}).json()["items"]
               if x["order_no"] == "FXFB-2")
    assert got["jpy_settled"] in (None, 0) and got["jpy_settled"] != 0, \
        f"没有汇率时日元应为空而不是 0，实际 {got['jpy_settled']!r}"
