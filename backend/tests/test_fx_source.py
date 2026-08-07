"""汇率多源优先级链与降级策略。

模型：链上依次尝试（默认 中行 → 谷歌 → 通用 API），每个源一轮最多试 N 次；
想用第 i 个源，它上面所有源都得已经**连续失败满 M 小时**，否则沿用上一次取到的值。
N / M / 链的顺序 / 中行取价口径都在「设置」页上可改。

这几段里任何一段写错都不会立刻报错，只会让账本悄悄用上错误口径的汇率，所以逐条钉住。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import FxRate
from app.services import fx, prefs

# 中行真实页面片段（2026-08-07 抓的）。注意开标签带属性 `<tr data-currency='日元'>`——
# 按 `<tr>` 精确匹配会漏掉它，本项目踩过这个坑（一度以为公开页里没有数据）。
_BOC = """
<tr data-currency='美元'><td>美元</td><td>711.83</td><td>705.9</td><td>714.9</td>
<td>714.9</td><td>712.53</td><td class="pjrq">2026/08/07 14:19:39</td><td>14:19:39</td></tr>
<tr data-currency='日元'>
    <td>日元</td><td>4.2454</td><td>4.2454</td><td>4.2782</td><td>4.2782</td>
    <td>4.2791</td><td class="pjrq">2026/08/07 14:19:39</td><td>14:19:39</td>
</tr>
"""

# 谷歌财经 beta 页的真实形状：报价在内嵌 JS 时间序列里，不在 DOM 文本里。
_GOOGLE = """
...[[2026,8,7,7,8,null,null,[]],[23.400000,-1.0E-4,-0.001,4,4,5]],
[[2026,8,7,7,9,null,null,[]],[23.462783,-1.2E-4,-0.0028,4,4,5]],
[[2026,8,6,23,58,null,null,[]],[23.100000,-1.0E-4,-0.001,4,4,5]]...
"""


@pytest.fixture()
def fx_engine(tmp_path):
    """独立空库：汇率按日期唯一，跑在共享会话库上会和别的用例互相踩。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'fx.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


def _ago(hours: float) -> dt.datetime:
    return fx.utcnow() - dt.timedelta(hours=hours)


async def _ret(v):
    return v


def _fail(*_a, **_kw):
    async def boom():
        raise RuntimeError("连不上")
    return boom()


# --- 中行解析 -------------------------------------------------------------------

def test_boc_inverts_per_100_quote():
    """牌价是「100 日元 = ? 人民币」，账本要「1 人民币 = ? 日元」，必须倒过来。"""
    assert fx.parse_boc_jpy(_BOC) == (Decimal(100) / Decimal("4.2791")).quantize(Decimal("0.0001"))


@pytest.mark.parametrize("col,per100", [
    ("hmrj", "4.2454"), ("cmrj", "4.2454"), ("mcj", "4.2782"),
    ("cmcj", "4.2782"), ("zhzjj", "4.2791"),
])
def test_boc_column_selectable(col, per100):
    """五个牌价来自**同一次请求**，换口径不该增加任何访问——只是取哪一列的区别。"""
    assert fx.parse_boc_jpy(_BOC, col) == (Decimal(100) / Decimal(per100)).quantize(Decimal("0.0001"))


def test_boc_unknown_column_falls_back():
    """口径配错不该让汇率整个抓不到，退回折算价即可。"""
    assert fx.boc_column("打错的名字") == fx.BOC_COLUMNS["zhzjj"]


def test_boc_rejects_missing_row():
    with pytest.raises(ValueError, match="没有日元行"):
        fx.parse_boc_jpy("<table><tr data-currency='美元'><td>美元</td></tr></table>")


def test_boc_rejects_insane_rate():
    """页面改版把列挪位、或抓到离谱数时，宁可失败沿用旧值，也不让它污染整本账。"""
    with pytest.raises(ValueError, match="超出合理区间"):
        fx.parse_boc_jpy(_BOC.replace("<td>4.2791</td>", "<td>99999</td>"))


# --- 谷歌解析 -------------------------------------------------------------------

def test_google_takes_newest_point_not_last_element():
    """按**时间戳最大**取，而不是取数组最后一个——数组顺序不是契约。
    夹具里最后一个元素是 8-6 的旧点，取错就会拿到 23.1 而不是 23.462783。"""
    assert fx.parse_google(_GOOGLE) == Decimal("23.4628")


def test_google_rejects_when_structure_changes():
    """谷歌把页面改了（旧 class 选择器就是这么失效的）→ 明确报错，不返回垃圾值。"""
    with pytest.raises(ValueError, match="没解析到报价"):
        fx.parse_google("<div class='YMlKec fxKbKc'>23.46</div>")


# --- 重试 -----------------------------------------------------------------------

def test_retries_configured_times(monkeypatch):
    calls = []

    async def boom(*_a, **_kw):
        calls.append(1)
        raise RuntimeError("连不上")

    monkeypatch.setattr(fx, "fetch_rate_boc", boom)
    monkeypatch.setattr(fx, "_BACKOFF", ())            # 别在测试里真睡
    assert asyncio.run(fx.try_source(fx.SOURCE_BOC, {"fx.attempts": 4})) is None
    assert len(calls) == 4, "重试次数没跟着设置走"


def test_second_attempt_can_succeed(monkeypatch):
    """一次抖动不该让整轮失败。"""
    n = {"i": 0}

    async def flaky(*_a, **_kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("抖了一下")
        return Decimal("23.3694")

    monkeypatch.setattr(fx, "fetch_rate_boc", flaky)
    monkeypatch.setattr(fx, "_BACKOFF", ())
    assert asyncio.run(fx.try_source(fx.SOURCE_BOC, {"fx.attempts": 3})) == Decimal("23.3694")


# --- 降级门槛 -------------------------------------------------------------------

def test_first_source_is_always_allowed(fx_engine):
    with Session(fx_engine) as s:
        assert fx.may_use(s, ["boc", "google"], 0, 72) is True


def test_second_source_blocked_until_first_down_long_enough(fx_engine):
    """中行刚挂 1 小时就换谷歌 → 账本会在两个口径之间横跳。必须等够。"""
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 5), rate=Decimal("23.3"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(1)))
        s.commit()
        assert fx.may_use(s, ["boc", "google"], 1, 72) is False


def test_second_source_allowed_after_threshold(fx_engine):
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 1), rate=Decimal("23.3"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(73)))
        s.commit()
        assert fx.may_use(s, ["boc", "google"], 1, 72) is True


def test_never_succeeded_source_does_not_block(fx_engine):
    """全新部署 + 首选源就是连不上：它「从未成功过」，不该把后面的源一起挡死，
    否则用户开局连一个汇率都拿不到。"""
    with Session(fx_engine) as s:
        assert fx.may_use(s, ["boc", "google"], 1, 72) is True


def test_zero_hours_means_degrade_immediately(fx_engine):
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 7), rate=Decimal("23.3"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(0.01)))
        s.commit()
        assert fx.may_use(s, ["boc", "google"], 1, 0) is True


def test_third_source_needs_all_above_down(fx_engine):
    """已经降级在用谷歌、谷歌也刚挂一下 → 不该马上再往下掉到第三个源。"""
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 1), rate=Decimal("23.3"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(200)))
        s.add(FxRate(date=dt.date(2026, 8, 7), rate=Decimal("23.4"),
                     source=fx.SOURCE_GOOGLE, fetched_at=_ago(2)))
        s.commit()
        assert fx.may_use(s, ["boc", "google", "erapi"], 2, 72) is False


# --- 整链行为 -------------------------------------------------------------------

def _use_chain(monkeypatch, chain, hours=72, attempts=1):
    monkeypatch.setattr(prefs, "load", lambda _s: {
        "fx.sources": chain, "fx.attempts": attempts, "fx.fallback_hours": hours,
        "fx.refresh_seconds": 21600, "fx.boc_column": "zhzjj"})
    monkeypatch.setattr(fx, "_BACKOFF", ())


def test_first_source_wins(monkeypatch, fx_engine):
    monkeypatch.setattr(fx, "fetch_rate_boc", lambda *a, **k: _ret(Decimal("23.3694")))
    monkeypatch.setattr(fx, "fetch_rate_google", lambda *a, **k: _ret(Decimal("99.0")))
    _use_chain(monkeypatch, ["boc", "google"])
    with Session(fx_engine) as s:
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.source == fx.SOURCE_BOC and row.rate == Decimal("23.3694")


def test_keeps_last_value_when_upstream_not_down_long_enough(monkeypatch, fx_engine):
    """中行刚挂 → 沿用上一次的值，**不写库、不换源**，也不去问谷歌。"""
    monkeypatch.setattr(fx, "fetch_rate_boc", _fail)
    called = []
    monkeypatch.setattr(fx, "fetch_rate_google",
                        lambda *a, **k: (called.append(1), _ret(Decimal("23.4")))[1])
    _use_chain(monkeypatch, ["boc", "google"])
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 5), rate=Decimal("23.3694"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(10)))
        s.commit()
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.rate == Decimal("23.3694") and row.source == fx.SOURCE_BOC
        assert not called, "上游还没失败够久就去问下一个源了"
        assert len(s.exec(select(FxRate)).all()) == 1, "沿用旧值时不该新增行"


def test_degrades_to_google_after_threshold(monkeypatch, fx_engine):
    monkeypatch.setattr(fx, "fetch_rate_boc", _fail)
    monkeypatch.setattr(fx, "fetch_rate_google", lambda *a, **k: _ret(Decimal("23.4628")))
    _use_chain(monkeypatch, ["boc", "google", "erapi"])
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 1), rate=Decimal("23.3"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(80)))
        s.commit()
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.source == fx.SOURCE_GOOGLE and row.rate == Decimal("23.4628")


def test_chain_order_is_configurable(monkeypatch, fx_engine):
    """把谷歌调到第一位，就该先问谷歌——「谁是备用」跟着配置走，不是硬编码。"""
    monkeypatch.setattr(fx, "fetch_rate_boc", lambda *a, **k: _ret(Decimal("23.3694")))
    monkeypatch.setattr(fx, "fetch_rate_google", lambda *a, **k: _ret(Decimal("23.4628")))
    _use_chain(monkeypatch, ["google", "boc"])
    with Session(fx_engine) as s:
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.source == fx.SOURCE_GOOGLE


def test_recovers_to_first_source(monkeypatch, fx_engine):
    """首选源恢复后下一轮就写回它，前端的「备用」标记随之消失。"""
    monkeypatch.setattr(fx, "fetch_rate_boc", lambda *a, **k: _ret(Decimal("23.5")))
    _use_chain(monkeypatch, ["boc", "google"])
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 6), rate=Decimal("20.0"), source=fx.SOURCE_GOOGLE))
        s.commit()
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.source == fx.SOURCE_BOC and row.rate == Decimal("23.5")


def test_all_sources_down_keeps_history(monkeypatch, fx_engine):
    for name in ("fetch_rate_boc", "fetch_rate_google", "fetch_rate_fallback"):
        monkeypatch.setattr(fx, name, _fail)
    _use_chain(monkeypatch, ["boc", "google", "erapi"], hours=0)
    with Session(fx_engine) as s:
        s.add(FxRate(date=dt.date(2026, 8, 1), rate=Decimal("23.3694"),
                     source=fx.SOURCE_BOC, fetched_at=_ago(100)))
        s.commit()
        row = asyncio.run(fx.refresh_and_store(s))
        assert row.rate == Decimal("23.3694"), "全挂了也要给出最近一次记录"


# --- 接口 -----------------------------------------------------------------------

def test_api_marks_non_primary_source_as_fallback(client, session, fx_today):
    """「备用」= 不是链上第一个源。用户把谷歌调到第一位时，中行才是那个「备用」。"""
    row = session.exec(select(FxRate).order_by(FxRate.date.desc())).first()
    row.source = fx.SOURCE_GOOGLE
    session.add(row)
    session.commit()

    body = client.get("/api/fx").json()
    assert body["source"] == fx.SOURCE_GOOGLE and body["fallback"] is True
    assert body["source_label"] == "谷歌财经", "要能告诉用户当前到底用的哪个源"

    client.put("/api/settings", json={"values": {"fx.sources": ["google", "boc", "erapi"]}})
    assert client.get("/api/fx").json()["fallback"] is False, "调成首选后不该再标「备用」"
    client.put("/api/settings", json={"values": {"fx.sources": ["boc", "google", "erapi"]}})


# --- 设置 -----------------------------------------------------------------------

def test_settings_roundtrip(client):
    body = client.get("/api/settings").json()
    assert body["values"]["fx.sources"] == ["boc", "google", "erapi"]
    assert any(sp["key"] == "fx.attempts" and sp["min"] == 1 for sp in body["specs"]), \
        "前端要靠 specs 渲染表单，不该自己写死取值范围"

    r = client.put("/api/settings", json={"values": {"fx.attempts": 5}})
    assert r.status_code == 200 and r.json()["values"]["fx.attempts"] == 5
    assert client.get("/api/settings").json()["values"]["fx.attempts"] == 5
    client.put("/api/settings", json={"values": {"fx.attempts": 3}})


@pytest.mark.parametrize("patch,why", [
    ({"fx.attempts": 0}, "次数下界"),
    ({"fx.attempts": 99}, "次数上界"),
    ({"fx.fallback_hours": -1}, "小时不能为负"),
    ({"fx.sources": []}, "不能一个源都不留"),
    ({"fx.sources": ["boc", "boc"]}, "不能重复"),
    ({"fx.sources": ["不存在的源"]}, "未知源"),
    ({"fx.boc_column": "乱填"}, "口径必须是可选值之一"),
    ({"不存在的键": 1}, "未注册的键"),
])
def test_settings_rejects_bad_values(client, patch, why):
    assert client.put("/api/settings", json={"values": patch}).status_code == 422, why


def test_settings_partial_update_keeps_others(client):
    """只传一个键不该把别的键重置成默认值。"""
    client.put("/api/settings", json={"values": {"fx.fallback_hours": 24}})
    client.put("/api/settings", json={"values": {"fx.attempts": 4}})
    v = client.get("/api/settings").json()["values"]
    assert v["fx.fallback_hours"] == 24 and v["fx.attempts"] == 4
    client.put("/api/settings", json={"values": {"fx.fallback_hours": 72, "fx.attempts": 3}})


def test_corrupt_stored_value_falls_back_to_default(session):
    """库里存了坏值（手改过、或降级后残留旧格式）→ 退回默认并告警，不该让汇率彻底停摆。"""
    from app.models import Setting

    # 这一行可能已被前面的用例写过（共享会话库），直接改值而不是重新插
    row = session.get(Setting, "fx.attempts") or Setting(key="fx.attempts")
    before = row.value
    row.value = "这不是 JSON"
    session.add(row)
    session.commit()

    assert prefs.load(session)["fx.attempts"] == prefs.SPECS["fx.attempts"].default

    row.value = before                       # 还原，免得影响后续用例
    session.add(row)
    session.commit()
