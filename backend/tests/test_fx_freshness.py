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
from pathlib import Path

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


def _put_rate(session, *, hours_ago: float = 0, days_ago: int = None,
              rate="20.0", source="boc", date=None):
    """直接落一条汇率行。

    **口径以「它是哪一天的」为准**（`days_ago`），`fetched_at` 只是附带写上去的。
    2026-09-02 起 `rate_age_hours` 按 `date` 算而不是按 `fetched_at`——
    理由见那个函数：按后者算的话，用户去汇率页补填一条历史汇率，
    会把「汇率已过期」这个关于钱的告警静默关掉。

    所以「20 小时旧的汇率」这个说法**在新口径下不存在**：当天的汇率恒为 0 小时旧
    （不然早上抓的一条到晚上就快到 24 小时，接近默认阈值 48 的一半，毫无道理）。
    要表达「旧」，说的是**差几天**。`hours_ago` 保留给还在用它的几条，
    按 `//24` 折成天——与原先的行为一致。
    """
    if days_ago is None:
        days_ago = int(hours_ago // 24)
    d = date or (dt.datetime.now(fx.JST).date() - dt.timedelta(days=days_ago))
    row = session.exec(select(FxRate).where(FxRate.date == d)).first()
    fetched = fx.utcnow() - dt.timedelta(hours=hours_ago or days_ago * 24)
    if row:
        row.rate, row.source, row.fetched_at = Decimal(rate), source, fetched
    else:
        row = FxRate(date=d, rate=Decimal(rate), source=source, fetched_at=fetched)
    session.add(row)
    session.commit()
    return row


def test_age_is_measured_from_the_rate_date_not_from_when_it_was_typed(session):
    """汇率的「年龄」按**它是哪一天的**算，不是按什么时候被写进库的。

    这两者只在一种情形下分叉，而那一种恰恰最要命：用户去汇率页「补填哪一天」补一条
    历史汇率——那正是这个页面存在的理由。补出来的行 `date` 是历史日期、
    `fetched_at` 是**现在**。按 `fetched_at` 算的话（2026-09-02 实测）：

        补填前  expired=True   age_hours=2160    （最新汇率是 90 天前的）
        补填一条 30 天前的汇率
        补填后  expired=False  age_hours≈0       ← 告警被静默关掉

    界面于是把那个 30 天前的值当成新鲜的当前汇率显示，
    每建一单本该记的那条「用的汇率已过期，日元金额可能不准」也一起消失。
    **一个纯粹的补录动作，关掉了一个关于钱的告警。**

    判据的两半缺一不可：
      · 刚写进去的历史行，年龄必须约等于它离今天的天数（而不是 0）；
      · 当天的行年龄必须是 0（不然早上抓的一条到晚上就快 24 小时旧，
        接近默认阈值 48 的一半，毫无道理）。
    """
    old = _put_rate(session, days_ago=30, rate="21.0")
    old.fetched_at = fx.utcnow()                       # 就是刚刚才写进库的
    session.add(old)
    session.commit()

    age = fx.rate_age_hours(old)
    assert age is not None and 29 * 24 <= age < 31 * 24, (
        f"30 天前那一天的汇率，算出来的年龄是 {age} 小时——"
        f"按写入时刻算的话它会是 0，补填一次就能把过期告警关掉")

    today = _put_rate(session, days_ago=0, rate="22.0")
    assert fx.rate_age_hours(today) == 0, (
        f"当天的汇率年龄应恒为 0，实际 {fx.rate_age_hours(today)}")


def test_naive_timestamp_does_not_blow_up(session):
    """SQLite 取回的时间戳可能是 naive。拿它和带时区的比较会 TypeError。

    **这条原先指向 `rate_age_hours`**，而 2026-09-02 起那个函数按 `date` 算、
    根本不碰 `fetched_at` 了——留在原处它会变成一条永远不会红的测试。
    naive 的风险现在实际在 `_recency_key`（`pick_from` 的排序键）与
    `routers/fx._jst_hm`（汇率页显示「那天几点」）两处，所以改指向前者：
    它在建单路径上（`pick_on` → `pick_from`），炸了就是整条建单 500。
    """
    a = _put_rate(session, days_ago=0, rate="20.0")
    b = _put_rate(session, days_ago=0, rate="21.0", date=a.date)
    a.fetched_at = a.fetched_at.replace(tzinfo=None) if a.fetched_at.tzinfo else a.fetched_at
    got = fx.pick_from([a, b])
    assert got is not None, "naive 时间戳让挑选当天汇率炸了——建单路径会整条 500"


def test_expired_follows_the_setting(session):
    """过期与否跟着 `fx.stale_hours` 走。

    用**天**来表达「旧」，不用小时：新口径下当天的汇率恒为 0 小时旧，
    昨天的是 0–24 小时（取决于现在几点）——拿昨天配一个 10 小时的阈值，
    这条测试的绿会取决于它几点跑。前天的恒 ≥24 小时，任何时刻都稳。
    """
    try:
        prefs.save(session, {"fx.stale_hours": 10})
        assert not fx.is_expired(session, _put_rate(session, days_ago=0)), "当天的汇率不该算过期"
        assert fx.is_expired(session, _put_rate(session, days_ago=2)), \
            "前天的汇率至少 24 小时旧，阈值 10 小时下必须算过期"
    finally:
        # **必须 try/finally。** 写在函数末尾的话，上面任何一个断言挂掉都跑不到它，
        # 于是 `fx.stale_hours` 停在 10，后面 `test_settings_roundtrip` 报
        # `assert 1 == 48` ——红的地方不是错的地方，最难查的那一种。
        prefs.save(session, {"fx.stale_hours": 48})


def test_expired_rate_is_still_served(session):
    """过期**不**等于拒绝供给。拒绝会让建单直接失去日元金额，那更糟。"""
    try:
        prefs.save(session, {"fx.stale_hours": 1})
        _put_rate(session, days_ago=4, rate="19.5")
        assert fx.current_rate(session) is not None, "过期就不给值了？建单会整批失去日元金额"
    finally:
        prefs.save(session, {"fx.stale_hours": 48})


def test_rate_lookup_warns_when_expired(session, caplog):
    """核心：用了过期汇率必须留痕。没有这条，链路断掉时账本会安静地一路记错。"""
    try:
        prefs.save(session, {"fx.stale_hours": 1})
        _put_rate(session, days_ago=4, rate="18.0")
        with caplog.at_level("WARNING"):
            got = fx.rate_for_date(session, None, "建商品订单 TEST-1")
        assert got == Decimal("18.0000") or got == Decimal("18.0")
        # 用 getMessage()：日志是 %-风格惰性格式化，直接对 r.message 做 % r.args 会在
        # 参数个数对不上时抛 TypeError（第一版就是这么炸的），而且 r.message 本身是模板不是成品。
        assert any("过期" in r.getMessage() for r in caplog.records), "用了过期汇率却没有任何警告"
    finally:
        prefs.save(session, {"fx.stale_hours": 48})     # 断言挂了也要还原，理由见上一条


def test_rate_lookup_is_quiet_when_fresh(session, caplog):
    """反面：新鲜时不该刷警告——狼来了喊多了就没人看了。"""
    prefs.save(session, {"fx.stale_hours": 48})
    _put_rate(session, days_ago=0, rate="20.5")
    with caplog.at_level("WARNING"):
        fx.rate_for_date(session, None, "建商品订单 TEST-2")
    assert not [r for r in caplog.records if "过期" in r.getMessage()], "汇率还新鲜却报了过期"


def test_api_exposes_age_and_expired(client, session):
    """前端要靠这两个字段把「已过期多久」显示出来。
    只有 `stale`（日粒度）的话，1 天前和 3 个月前长得一模一样。"""
    try:
        prefs.save(session, {"fx.stale_hours": 1})
        _put_rate(session, days_ago=4)
        got = client.get("/api/fx").json()
        assert got["expired"] is True
        # 4 天前的汇率：年龄 = 3 整天 + 今天已过的时数 ∈ [72, 96)。
        # 原先这里写 `> 90`，那是按 `fetched_at` 的 100 小时校准的；
        # 按日期算的话它会随「现在几点」在 72–96 之间浮动，`> 90` 一天里大半时间是假的。
        assert got["age_hours"] and 72 <= got["age_hours"] < 96, got["age_hours"]
    finally:
        prefs.save(session, {"fx.stale_hours": 48})


def test_every_rate_stamping_path_can_warn():
    """**所有**给账本行盖汇率的地方都必须走带 `what` 的告警版本。

    原先只有建商品订单/建集运订单两处走告警版本，另外四条（杂项建单、三张表 PATCH
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


# --- 设置项本身的行为（原 test_fx_source.py 的 prefs 用例，改用还留在核心的两个键）------

def test_settings_roundtrip(client):
    """设置存得进、读得回，且**表单元信息由后端下发**——
    前端不该自己写死取值范围（「后端允许 1..8760、前端写死 1..5」那种两边各说各话）。"""
    body = client.get("/api/settings").json()
    assert body["values"]["fx.stale_hours"] == 48
    sp = next(x for x in body["specs"] if x["key"] == "fx.stale_hours")
    assert sp["min"] == 1 and sp["max"] and sp["label"]

    assert client.put("/api/settings", json={"values": {"fx.stale_hours": 24}}).status_code == 200
    assert client.get("/api/settings").json()["values"]["fx.stale_hours"] == 24
    client.put("/api/settings", json={"values": {"fx.stale_hours": 48}})


@pytest.mark.parametrize("patch", [
    {"fx.stale_hours": 0},              # 下界
    {"fx.stale_hours": 99999},          # 上界
    {"fx.manual_rate": "999"},          # 超出汇率合理区间
    {"fx.manual_rate": "不是数"},        # 解析不出来
    {"不存在的键": 1},                   # 未注册
])
def test_settings_rejects_bad_values(client, patch):
    """任一项不合规就整体 422，不做部分写入——半套设置比旧设置更难排查。"""
    assert client.put("/api/settings", json={"values": patch}).status_code == 422


@pytest.mark.parametrize("bad", ["NaN", "nan", "sNaN", "-NaN"])
def test_manual_rate_rejects_not_a_number_values(client, bad):
    """**NaN 能被 `Decimal()` 正常解析**，坑在下一步：decimal 对 NaN 做**有序比较**
    会抛 `InvalidOperation`——那是 `ArithmeticError` 而不是 `ValueError`，
    于是路由的 `except ValueError` 接不住、`main.py` 也没有对应 handler ⇒ 裸 500。

    `PUT /api/settings` 的 `values` 是个裸 dict（`SettingsUpdate`），pydantic 不做任何
    拦截，所以 `"NaN"` 能一路走到 `_check_manual_rate`。上面那条参数化测试有
    「超出区间」和「解析不出来」两档，唯独没有这一档——**解析得出、但比较会抛**。

    `schemas._q_decimal` 早就踩过同一个坑并写了注释（顺序是「先 is_finite 再比较」），
    而 prefs 这份校验是后来单独写的、漏了那一句。
    """
    assert client.put("/api/settings", json={"values": {"fx.manual_rate": bad}}).status_code == 422


def test_settings_partial_update_keeps_others(client):
    """只提交变了的键。整包提交会把别人刚在另一个标签页改过的项一起盖回去。"""
    client.put("/api/settings", json={"values": {"fx.manual_rate": "20.0", "fx.stale_hours": 36}})
    client.put("/api/settings", json={"values": {"fx.stale_hours": 12}})
    got = client.get("/api/settings").json()["values"]
    assert got["fx.stale_hours"] == 12 and got["fx.manual_rate"] == "20.0"
    client.put("/api/settings", json={"values": {"fx.manual_rate": "", "fx.stale_hours": 48}})


def test_corrupt_stored_value_falls_back_to_default(client, session):
    """库里存着坏值（手改过、或降级后残留的旧格式）→ 退回默认并告警，不让整页打不开。"""
    from app.models import Setting

    row = session.get(Setting, "fx.stale_hours") or Setting(key="fx.stale_hours", value="48")
    row.value = '"not an int"'
    session.add(row)
    session.commit()
    assert client.get("/api/settings").json()["values"]["fx.stale_hours"] == 48


# --- 摘源之后的两条元断言 -------------------------------------------------------

def test_core_only_knows_its_own_source():
    """核心只认识它自己会写的那一个源。其余标识由插件自报，认不出原样透传裸 key——
    核心维护一份它已经不会产出、也无法穷举的源名表，只会烂掉。"""
    assert set(fx.SOURCE_LABELS) == {"manual"}


def test_no_source_config_leaks_back_into_core():
    """汇率**怎么取**是插件的事。源顺序/重试/取价口径以任何形式回流核心设置，这里就红。"""
    from app.services.prefs import SPECS

    assert {k for k in SPECS if k.startswith("fx.")} == {"fx.manual_rate", "fx.stale_hours"}, \
        f"核心设置里出现了不该有的汇率项：{sorted(SPECS)}"


def test_core_has_no_background_loop_writing_fx():
    """核心不得重新引入自己写 FxRate 的后台循环。

    原来 fx_loop 直接用 Session 写 FxRate、绕过 HTTP 中间件，要自己查只读屏障。
    搬进插件后这条路径没了——但约束还在：再加一条后台循环就等于把屏障重新开一个洞。
    """
    import re as _re

    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "fx.py").read_text(encoding="utf-8")
    assert not _re.search(r"async def \w*loop", src), \
        "services/fx.py 又出现了后台循环：汇率写入应经 POST /api/plugins/ingest"


def test_core_never_imports_httpx_for_rates():
    """核心自己不抓汇率了 → `app/services/fx.py` 不该再 import httpx。

    ⚠️ 但 **requirements 里的 httpx 必须保留**：汇率插件的 plugin.toml 写着
    `python = "inherit"`（跑核心的解释器、零安装），它直接 import httpx。
    删掉那条依赖，汇率插件当场起不来，而没有任何测试会拦住——所以这条断言只管
    「核心代码不用它」，requirements 那边由下面一条守着。
    """
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "fx.py").read_text(encoding="utf-8")
    assert "import httpx" not in src, "核心又自己抓汇率了？取数应当在插件里"


def test_httpx_stays_in_requirements_for_inherit_plugins():
    """httpx 是「核心自己不 import、但必须保留」的依赖——很容易被当成无用依赖删掉。

    `python = "inherit"` 的轻插件（汇率就是）直接跑在核心解释器里、直接 import httpx。
    这条把那个理由钉在测试里，而不是只写在注释里。
    """
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "httpx" in req, (
        "requirements.txt 里的 httpx 被删了。它看起来没人用（核心已不 import），"
        "但 python=\"inherit\" 的插件靠它跑——删掉汇率插件当场起不来。")


def test_saving_manual_rate_takes_effect_immediately(client, session):
    """填了手填汇率、点保存 → 库里立刻有一条，界面马上显示得出来。

    `ensure_manual_rate` 平时只在「需要汇率却一条都没有」时才落行（建单时）。
    保存设置不触发它的话，用户看到的是「已保存」+「库里还没有汇率」，像是没保存上——
    而实际上要等到下次建单才会生效。真浏览器复查时就是这么发现的。
    """
    for row in session.exec(select(FxRate)).all():
        session.delete(row)
    session.commit()
    assert client.get("/api/fx").json()["rate"] is None

    assert client.put("/api/settings", json={"values": {"fx.manual_rate": "20.5"}}).status_code == 200
    got = client.get("/api/fx").json()
    assert got["rate"] and Decimal(got["rate"]) == Decimal("20.5"), "保存后界面上仍看不到汇率"
    assert got["source"] == "manual"
    client.put("/api/settings", json={"values": {"fx.manual_rate": ""}})


def test_history_page_agrees_with_what_orders_actually_use(client, session):
    """汇率页每天显示的「采用」必须等于建单时真会用的那条。

    两边各写一遍取舍规则的话，页面显示的和账本里算的会是两个数，
    而那种不一致要等到对账才发现。所以这条比起「页面能打开」重要得多。
    """
    import datetime as dt

    from app.models import FxRate
    from app.services import fx as fxsvc

    base = dt.date(2026, 3, 1)
    for i in range(4):                                  # 四天，每天两条，其中一天有手填
        d = base + dt.timedelta(days=i)
        session.add(FxRate(date=d, rate=Decimal("20.0") + i, source="a",
                           fetched_at=dt.datetime(2026, 3, 1, 1, 0) + dt.timedelta(days=i)))
        session.add(FxRate(date=d, rate=Decimal("21.0") + i, source="b",
                           fetched_at=dt.datetime(2026, 3, 1, 9, 0) + dt.timedelta(days=i)))
    session.add(FxRate(date=base, rate=Decimal("99.5"), source=fxsvc.SOURCE_MANUAL,
                       fetched_at=dt.datetime(2026, 3, 1, 2, 0)))
    session.commit()

    days = (dt.datetime.now(fxsvc.JST).date() - base).days + 1
    got = {r["date"]: r["used"] for r in client.get(f"/api/fx/history?days={days}").json()["items"]}
    for i in range(4):
        d = base + dt.timedelta(days=i)
        assert Decimal(str(got[d.isoformat()])) == fxsvc.pick_on(session, d).rate, \
            f"{d} 页面显示的采用值与建单实际用的不一致"
    assert Decimal(str(got[base.isoformat()])) == Decimal("99.5"), "手填那天没优先取手填"


def test_history_does_not_query_once_per_day(client, session):
    """按天汇总不许随天数线性增加查询数——上限 730 天就是 730 次往返。"""
    import datetime as dt

    from sqlalchemy import event

    from app.database import get_engine
    from app.models import FxRate

    base = dt.date(2026, 4, 1)
    for i in range(12):
        session.add(FxRate(date=base + dt.timedelta(days=i), rate=Decimal("22.0"), source="a"))
    session.commit()

    n = 0

    def count(conn, cursor, statement, params, ctx, many):
        nonlocal n
        if "fxrate" in statement.lower():
            n += 1

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", count)
    try:
        days = (dt.datetime.now(__import__("app.services.fx", fromlist=["JST"]).JST).date() - base).days + 1
        client.get(f"/api/fx/history?days={days}")
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert n <= 2, f"按天各查一次：12 天打了 {n} 条 fxrate 查询"


def test_staging_write_through_restamps_a_missing_rate(client, mk, session):
    """在暂存页改一条**已导入**的行，缺汇率要被补上。

    暂存页的汇率格可编辑且可清空（`clearable` 没关），清一下 PATCH 过去就是
    `fx_rate = null`。若这条路径不补汇率，jpy_auto/jpy_settled 一起变 NULL：
    看板的 SUM 跳过它、笔数照数——「笔数 +1、金额 +0」，一条已导入的账本单
    悄悄变成不计钱的行。orders / misc / shipment 三处都有这一刀，只有这里漏了。
    """
    import datetime as dt

    from app.models import FxRate

    session.add(FxRate(date=dt.date(2027, 2, 10), rate=Decimal("20.5"), source="a"))
    session.commit()

    s = mk("/api/staging", {"order_date": "2027-02-10", "title": "补汇率", "price_cny": 100,
                            "order_no": "STG-FX-1", "platform": "淘宝"})
    imported = client.post(f"/api/staging/{s['id']}/import")
    assert imported.status_code == 200, imported.text

    row = next(x for x in client.get("/api/staging", params={"limit": 200}).json()["items"]
               if x["id"] == s["id"])
    cleared = client.patch(f"/api/staging/{s['id']}",
                           json={"version": row["version"], "fx_rate": None})
    assert cleared.status_code == 200, cleared.text

    oid = row["imported_order_id"]
    order = next(o for o in client.get("/api/orders", params={"limit": 200}).json()["items"]
                 if o["id"] == oid)
    assert order["fx_rate"] is not None, "汇率被清空后没补回来"
    assert order["jpy_settled"], f"结算日元被清成了 {order['jpy_settled']}——这笔钱看板会漏掉"


def test_backfilling_an_old_order_does_not_cry_wolf(client, session, caplog, mk):
    """补录几天前的订单**不该**报「汇率已过期」——那条汇率正是唯一正确的历史汇率。

    这条警告原先在暂存导入（补录的必经路径）上恒真。一条永远在喊狼来了的告警，
    比没有告警更糟：真出事时没人信。

    但「那天没有记录、退回了更旧的一条」仍然要喊——见下一条测试。
    """
    import datetime as dt

    from sqlmodel import delete

    from app.models import FxRate
    from app.services import fx as fxsvc

    # 必须是**真正的过去**：写未来日期的话 rate_age_hours 会被 max(0, …) 夹成 0、
    # is_expired 恒 False，这条测试就永远不走过期分支——假绿。
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).date()
    session.exec(delete(FxRate))
    session.add(FxRate(date=old, rate=Decimal("21.0"), source="a",
                       fetched_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)))
    session.commit()
    assert fxsvc.is_expired(session, fxsvc.pick_on(session, old)), \
        "前置没成立：这条汇率还没到过期线，下面的断言测不出东西"
    with caplog.at_level("WARNING"):
        got = fxsvc.rate_for_date(session, old, "暂存导入建单 TEST-BACKFILL")
    assert got == Decimal("21.0000"), got
    assert not [r for r in caplog.records if "已过期" in r.getMessage()], \
        "取到的就是那天的汇率，却报了「已过期」——假警报"


def test_falling_back_to_an_older_rate_still_warns(client, session, caplog):
    """那天没有记录、退回了更旧的一条 → 这才是真问题，必须喊。

    意味着取汇率的链路可能断了，而金额确实按一个不属于这笔账的汇率算了。
    """
    import datetime as dt

    from sqlmodel import delete

    from app.models import FxRate
    from app.services import fx as fxsvc

    # 自己建立前置：整个会话共用一个库，别的用例留下的**较新**汇率会成为 latest_stored 的
    # 返回值，那条不过期，于是这里什么都不会喊——测试静默变成「测了个寂寞」。
    session.exec(delete(FxRate))
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=19)).date()
    session.add(FxRate(date=old, rate=Decimal("22.0"), source="a",
                       fetched_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=19)))
    session.commit()
    with caplog.at_level("WARNING"):
        # 问今天的汇率 → 那天没有 → 退回 19 天前那条
        fxsvc.rate_for_date(session, dt.datetime.now(fxsvc.JST).date(), "建商品订单 TEST-FALLBACK")
    assert [r for r in caplog.records if "已过期" in r.getMessage()], \
        "退回了 19 天前的汇率却一声不吭"


def test_dashboard_and_fx_endpoint_show_the_same_rate_as_the_ledger_uses(client, session):
    """顶栏/看板显示的汇率，必须**就是**现在建单会用的那一条。

    分叉场景很常见：今天手填了一条，之后插件又抓了一条。
    `latest_stored` 给插件那条、建单走 `pick_on` 用手填那条——
    于是界面上的数字和账本里真正用的不是一个，而手填的**本意**恰恰是「用我这个值」。
    这种不一致不会报错，要等到对账才发现。
    """
    import datetime as dt
    from decimal import Decimal

    from app.models import FxRate
    from app.services.fx import JST, SOURCE_MANUAL, current_rate, rate_for_date

    today = dt.datetime.now(JST).date()
    old = session.exec(select(FxRate).where(FxRate.date == today)).all()
    for r in old:
        session.delete(r)
    session.commit()
    # 先手填，再让「插件」抓一条更晚的——latest_stored 会给后者
    session.add(FxRate(date=today, rate=Decimal("19.0000"), source=SOURCE_MANUAL,
                       fetched_at=dt.datetime(2026, 1, 1, 0, 0)))
    session.add(FxRate(date=today, rate=Decimal("25.0000"), source="boc",
                       fetched_at=dt.datetime(2026, 1, 1, 9, 0)))
    session.commit()

    ledger = rate_for_date(session, today)          # 建单真正用的
    assert ledger == Decimal("19.0000"), "手填优先这条规则本身坏了，下面的对比失去意义"
    assert current_rate(session) == ledger, "看板显示的汇率与建单用的不是同一条"
    shown = client.get("/api/fx").json()
    assert Decimal(str(shown["rate"])) == ledger, "GET /api/fx 与建单用的不是同一条"
