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
