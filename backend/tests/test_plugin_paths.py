"""两条路都得走得通：**装了插件**、**没装插件**。

这是本次重构的验收条件。把外部数据获取搬进插件之后，最大的风险不是「插件坏了」，
而是「没有插件时账本悄悄不对」——`SUM(jpy_settled)` 对 NULL 视而不见，
少算的那几笔在界面上和正常的长得一模一样。

所以两条路各自要证明的东西不同：
  · **不装插件**：账本仍然能自洽运转（手填汇率兜底），且缺口**看得见**（过期标记 + 日志）；
  · **装了插件**：数据能通过通用通道进来，而且插件**只能**动它被授权的那部分。
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from sqlmodel import select

from app.models import FxRate, PluginConfig, PluginRecord, User
from app.plugins import scopes
from app.services import fx, ingest, prefs


@pytest.fixture(autouse=True)
def _clean(session):
    for row in session.exec(select(FxRate)).all():
        session.delete(row)
    for row in session.exec(select(PluginRecord)).all():
        session.delete(row)
    session.commit()
    prefs.save(session, {"fx.manual_rate": "", "fx.stale_hours": 48})
    yield


# ══════════════════════════════════════════════════════════════════════════
# 一、没装插件
# ══════════════════════════════════════════════════════════════════════════

def test_no_plugin_no_manual_rate_app_still_works(client):
    """极端情形：没有插件、没有手填汇率、库里一条汇率都没有。

    要求：**接口不炸**。订单能建、列表能开、看板能出——只是日元金额是空的。
    这条守的是「可用性倒挂」：一个可选插件不该把「订单存不进去」变成后果。
    """
    r = client.post("/api/orders", json={
        "date": "2026-09-01", "order_no": "NOPLUG-1", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "10"}]})
    assert r.status_code == 200, r.text
    assert r.json()["fx_rate"] is None
    assert r.json()["jpy_settled"] is None, "没有汇率却算出了日元？那个数是哪来的"

    assert client.get("/api/orders", params={"limit": 10}).status_code == 200
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/fx").status_code == 200


def test_no_plugin_but_manual_rate_keeps_the_ledger_whole(client, session):
    """有手填汇率时，没有插件也能正常记账——**这是「留一个口」的验收点**。"""
    prefs.save(session, {"fx.manual_rate": "20.5"})
    r = client.post("/api/orders", json={
        "date": "2026-09-02", "order_no": "NOPLUG-2", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 2, "unit_price_cny": "10"}]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert Decimal(got["fx_rate"]) == Decimal("20.5")
    assert got["jpy_settled"] == 410, f"20 元 × 20.5 应是 410 円，实际 {got['jpy_settled']}"

    shown = client.get("/api/fx").json()
    assert shown["source"] == "manual"
    assert shown["source_label"] == "手填", "界面上必须看得出这个汇率是手填的，不是抓来的"


def test_manual_rate_is_recorded_as_a_real_row(client, session):
    """手填值写成一行真正的 FxRate，而不是读取时兜底。

    这样 `rate_for_date` / 审计 / 历史查询一行都不用改；否则每个读汇率的地方
    都要加一条分支——那正是本项目反复出问题的形状（同一件事写两处）。
    """
    prefs.save(session, {"fx.manual_rate": "19.9"})
    client.post("/api/orders", json={"date": "2026-09-03", "order_no": "NOPLUG-3",
                                     "platform": "淘宝",
                                     "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]})
    rows = session.exec(select(FxRate)).all()
    assert len(rows) == 1 and rows[0].source == "manual"


def test_missing_plugin_shows_up_as_expired_not_as_silence(client, session):
    """插件装了但很久没跑成功 → 界面必须显式说「已过期 N 小时」。

    最危险的失败不是「没有汇率」（那是吵的：日元列空着），而是「几个月前的汇率
    被当成新鲜的一直用」（那是安静的）。这条钉住那个提示确实出得来。
    """
    prefs.save(session, {"fx.stale_hours": 1})
    session.add(FxRate(date=dt.date(2026, 1, 1), rate=Decimal("20"), source="boc",
                       fetched_at=fx.utcnow() - dt.timedelta(days=30)))
    session.commit()
    got = client.get("/api/fx").json()
    assert got["expired"] is True
    assert got["age_hours"] > 600


# ══════════════════════════════════════════════════════════════════════════
# 二、装了插件（走通用写入通道）
# ══════════════════════════════════════════════════════════════════════════

def _plugin_client(client, session, granted: set[str], plugin_id="fx"):
    """造一个「插件身份」的请求头。绕过子进程，直接测通道与权限。"""
    user = session.exec(select(User)).first()
    token, jti = scopes.issue(user, plugin_id, granted)
    return {"Authorization": f"Bearer {token}"}, jti


def test_plugin_writes_rate_through_the_generic_channel(client, session):
    """插件把汇率交给核心 → 账本立刻能用。**全程没有为汇率新开任何接口。**"""
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    r = client.post("/api/plugins/ingest", headers=headers, json={
        "kind": "fx.rate", "items": [{"rate": "21.25", "source": "boc"}]})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == {"created": 1}

    o = client.post("/api/orders", json={
        "date": "2026-09-10", "order_no": "PLUG-1", "platform": "淘宝",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "100"}]}).json()
    assert Decimal(o["fx_rate"]) == Decimal("21.25")
    assert o["jpy_settled"] == 2125
    scopes.revoke(jti)


def test_plugin_cannot_touch_what_it_was_not_granted(client, session):
    """**权限的验收点**：只授了写汇率的插件，碰不到订单、暂存、数据库。

    这句话必须是真的——它是整套 scope 对用户唯一可见的价值
    （插件卡片上写着「本插件只能写汇率」）。
    """
    headers, jti = _plugin_client(client, session, {"fx:write"})
    for method, path in [("get", "/api/orders"), ("get", "/api/staging"),
                         ("post", "/api/staging"), ("get", "/api/db/connections")]:
        r = (client.post(path, headers=headers, json={}) if method == "post"
             else client.get(path, headers=headers))
        assert r.status_code == 403, f"{method.upper()} {path} 竟然放行了（{r.status_code}）"
    scopes.revoke(jti)


def test_ingest_scope_is_decided_by_kind_not_by_route(client, session):
    """一条路由服务多种数据，权限按 **kind** 判。

    只授了 `fx:write` 的插件，不能借同一条路由去写插件私有存储——
    否则「通用通道」就成了权限的后门。
    """
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    r = client.post("/api/plugins/ingest", headers=headers, json={
        "kind": "plugin.record",
        "items": [{"kind": "x", "key": "k", "data": {"a": 1}}]})
    assert r.status_code == 403, f"按 kind 的鉴权没生效（{r.status_code}）"
    scopes.revoke(jti)


def test_plugin_private_storage_is_namespaced(client, session):
    """插件私有存储按插件隔离——这是「插件能存自己的东西」而不破坏账本的前提。"""
    ingest.load_kinds()
    h1, j1 = _plugin_client(client, session, {"data:own"}, plugin_id="tracking")
    h2, j2 = _plugin_client(client, session, {"data:own"}, plugin_id="fx")
    body = {"kind": "note", "key": "k1", "data": {"v": 1}}
    assert client.post("/api/plugins/ingest", headers=h1,
                       json={"kind": "plugin.record", "items": [body]}).status_code == 200
    mine = client.get("/api/plugins/records/note", headers=h1).json()["items"]
    other = client.get("/api/plugins/records/note", headers=h2).json()["items"]
    assert [x["key"] for x in mine] == ["k1"]
    assert other == [], "另一个插件读到了不属于它的数据"
    scopes.revoke(j1)
    scopes.revoke(j2)


def test_revoked_token_stops_working_immediately(client, session):
    """任务结束即作废。跑完之后那枚令牌不该还能用二十几分钟——
    而它此刻已经落在插件的日志与环境变量里了。"""
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    body = {"kind": "fx.rate", "items": [{"rate": "20", "source": "boc"}]}
    assert client.post("/api/plugins/ingest", headers=headers, json=body).status_code == 200
    scopes.revoke(jti)
    r = client.post("/api/plugins/ingest", headers=headers, json=body)
    assert r.status_code == 401, f"作废后的令牌还能用（{r.status_code}）"


def test_rejected_item_is_not_written(client, session):
    """被拒的项**绝不能**落库。

    handler 内部一 commit 就会击穿外层 savepoint：回执说 rejected、行却已经进去了。
    回执与事实相反是最难查的一类错误，所以这条要有。
    """
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    r = client.post("/api/plugins/ingest", headers=headers, json={
        "kind": "fx.rate",
        "items": [{"rate": "20", "source": "boc"},
                  {"rate": "20", "source": "boc",
                   "date": (dt.date.today() + dt.timedelta(days=5)).isoformat()}]})
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[0]["status"] == "created"
    assert res[1]["status"] == "rejected" and "未来" in res[1]["message"]
    rows = session.exec(select(FxRate)).all()
    assert len(rows) == 1, f"被拒的那条也写进去了：{[(x.date, x.source) for x in rows]}"
    scopes.revoke(jti)


def test_manual_rate_is_not_overwritten_by_plugin(client, session):
    """人 > 机器：当天手填过的汇率，插件不覆盖。与 `can_advance_purchase` 同一条原则。"""
    ingest.load_kinds()
    prefs.save(session, {"fx.manual_rate": "18.0"})
    client.post("/api/orders", json={"date": "2026-09-20", "order_no": "MAN-1",
                                     "platform": "淘宝",
                                     "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]})
    headers, jti = _plugin_client(client, session, {"fx:write"})
    r = client.post("/api/plugins/ingest", headers=headers, json={
        "kind": "fx.rate", "items": [{"rate": "25.0", "source": "boc"}]})
    assert r.json()["results"][0]["status"] == "unchanged"
    assert client.get("/api/fx").json()["source"] == "manual"
    scopes.revoke(jti)


def test_unknown_kind_says_what_is_known(client, session):
    """插件版本比核心新时，报错必须说清核心认识哪些——否则只能靠猜。"""
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    r = client.post("/api/plugins/ingest", headers=headers,
                    json={"kind": "not.a.thing", "items": []})
    assert r.status_code == 400
    assert "fx.rate" in r.json()["detail"]["known"]
    scopes.revoke(jti)


def test_contract_lets_plugins_self_project(client, session):
    """插件靠 `/contract` 知道该发哪些字段，而不是发一堆让核心默默丢掉。"""
    ingest.load_kinds()
    headers, jti = _plugin_client(client, session, {"meta:read"})
    c = client.get("/api/plugins/contract", headers=headers).json()
    assert c["api"] == 1
    assert "rate" in c["kinds"]["fx.rate"]["fields"]
    assert c["kinds"]["fx.rate"]["scope"] == "fx:write"
    scopes.revoke(jti)


# ══════════════════════════════════════════════════════════════════════════
# 三、结构守卫：防这套设计以后腐化
# ══════════════════════════════════════════════════════════════════════════

# 人类专用的整片区域（登录、数据库管理、列布局、设置、插件管理本身…）。
# 插件永远不该碰，逐条登记没意义，按前缀整片划走。
_HUMAN_PREFIX = ("/api/auth", "/api/db", "/api/layout", "/api/settings",
                 "/api/plugins", "/api/health", "/api/dashboard", "/api/tags",
                 "/api/misc", "/api/items")

# 账本区里**刻意不对插件开放**的路由。逐条登记而不是按前缀，因为同一前缀下
# 风险差别极大：`GET /api/orders` 是只读列表，`DELETE /api/orders/{id}` 是删账本单。
# 新增路由时这张名单会逼作者表态；要开放就去挂 x-scope。
_PLUGIN_CLOSED = {
    "POST /api/orders",                                   # 建账本单要走暂存 → 人工确认
    "PATCH /api/orders/{order_id}",
    "DELETE /api/orders/{order_id}",
    "GET /api/orders/{order_id}",                         # 单条详情，插件用列表接口就够
    "POST /api/orders/ocr",                               # OCR 是人在界面上拖图片
    "POST /api/shipment",                                 # 建集运单是人的决定
    "GET /api/shipment/{shipment_id}",
    "DELETE /api/shipment/{shipment_id}",
    "POST /api/shipment/ocr",
    "POST /api/shipment/{shipment_id}/ocr-express",
    "POST /api/shipment/{shipment_id}/order/{order_id}",  # 挂靠/解挂是人的决定
    "DELETE /api/shipment/{shipment_id}/order/{order_id}",
}


def test_every_route_is_classified():
    """每条 API 路由要么挂了 `x-scope`，要么在「插件永远进不去」的名单里。

    默认拒绝的代价是：漏挂 = 插件访问不了。这条让漏挂在测试里就暴露，
    而不是等某个插件报 403 时才发现。
    """
    from app.main import app

    unclassified = []
    for r in scopes._iter_routes(app):
        if r.path.startswith(_HUMAN_PREFIX):
            continue
        sig = f"{sorted(r.methods)[0]} {r.path}"
        if (r.openapi_extra or {}).get("x-scope") or sig in _PLUGIN_CLOSED:
            continue
        unclassified.append(sig)
    assert not unclassified, (
        "这些路由既没挂 x-scope、也没登记为「对插件关闭」——新增路由时必须显式表态：\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n（要开放：加 openapi_extra={\"x-scope\": \"...\"}；不开放：加进 _PLUGIN_CLOSED）")


def test_closed_list_has_not_gone_stale():
    """名单的元断言：登记为「关闭」的路由若已不存在（或已经挂上 scope），就该从名单里去掉。

    豁免名单是最容易腐烂的守卫——留着旧条目永远不会红，保护范围却在悄悄缩小。
    """
    from app.main import app

    live = set()
    for r in scopes._iter_routes(app):
        sig = f"{sorted(r.methods)[0]} {r.path}"
        if not (r.openapi_extra or {}).get("x-scope"):
            live.add(sig)
    stale = _PLUGIN_CLOSED - live
    assert not stale, f"_PLUGIN_CLOSED 里这些条目已过期：{sorted(stale)}"


def test_route_flattening_actually_finds_routes():
    """元断言：路由展平一旦失效，整个闸门要么全拒要么全放行，而两种都不好在测试里看出来。

    本版 FastAPI 把 include_router 的路由包在 `_IncludedRouter` 里，它没有 `.routes`，
    子路由挂在 `.original_router` 上——只看 `app.routes` 只能拿到 `/api/health` 一条。
    """
    from app.main import app

    assert len(scopes._iter_routes(app)) > 40, "路由展平失效了（闸门会失去意义）"


def test_every_handler_scope_is_registered():
    """handler 声明的权限必须在权限表里。权限只有一张表，不许各处自己造词。"""
    ingest.load_kinds()
    bad = [h.kind for h in ingest.KINDS.values() if h.scope not in scopes.SCOPES]
    assert not bad, f"这些 handler 用了未注册的权限：{bad}"


def test_handlers_never_commit():
    """handler 只能 add/flush。一个 `commit()` 就会击穿外层 savepoint，
    让「被拒绝的数据」照样落库，而回执还说 rejected。"""
    import ast
    import inspect
    import textwrap

    bad = []
    for h in ingest.KINDS.values():
        tree = ast.parse(textwrap.dedent(inspect.getsource(h.apply)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("commit", "rollback", "delete"):
                bad.append(f"{h.kind}: session.{node.attr}()（第 {node.lineno} 行）")
    assert not bad, "handler 里不许自己提交/回滚/删除：\n  " + "\n  ".join(bad)


# --- 三条启动路径不许各自漂移 --------------------------------------------------

def test_every_launch_path_sends_plugin_params():
    """凡是 spawn 子进程的路径，下发的 config 必须含 `params`。

    本轮审计里 (A2)(A6)(A7) 全是同一个形状：手动 / 定时 两条路径各自组装、各自漂移。
    最贵的一条是定时不带 params——用户在卡片上设的汇率源对定时**完全无效**，
    而账本采用的恰恰是定时写进来的那条。安静地写错数据。

    这条守的是漂移本身：谁再加第三条启动路径、又忘了带 params，当场红。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "plugins.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))

    def _is_conf(expr, fn) -> bool:
        """expr 是不是 `_launch_conf(...)` 的返回值——直接调用，或先赋给局部变量再传。
        （把调用提到循环外是**更好**的写法，守卫不该逼人写差。）"""
        if isinstance(expr, ast.Call):
            return getattr(expr.func, "id", "") == "_launch_conf"
        if isinstance(expr, ast.Name) and fn is not None:
            for a in ast.walk(fn):
                if (isinstance(a, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == expr.id for t in a.targets)):
                    return _is_conf(a.value, None)
        return False

    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_launch"):
                continue
            conf = next((k.value for k in node.keywords if k.arg == "config"), None)
            if conf is None:
                continue                  # 不带 config 的（如纯登录）不在此列
            if not _is_conf(conf, fn):
                bad.append(f"plugins.py:{node.lineno}")
    assert not bad, ("这些 _launch 的 config 不是 _launch_conf() 组装的，插件参数不会下发：\n  "
                     + "\n  ".join(bad))


def test_plugin_token_ttl_is_derived_from_the_reap_timeout():
    """令牌活得必须比子进程久。

    原先 `scopes.issue` 三个调用点全用默认 timeout_s=600 → TTL 12 分钟，
    而收割器 `communicate(timeout=1800)` 等 30 分钟。超过 12 分钟的抓取，
    最后那次回灌必然 401，整批订单静默丢失。
    """
    import datetime as dt

    from app.plugins import scopes
    from app.routers import plugins as mod

    class _U:
        id, username = 1, "u"

    _, jti = scopes.issue(_U(), "demo", set(), timeout_s=mod._REAP_TIMEOUT)
    try:
        left = scopes._ALIVE[jti] - __import__("time").monotonic()
        assert left > mod._REAP_TIMEOUT, \
            f"令牌只活 {left:.0f} 秒，比子进程允许跑的 {mod._REAP_TIMEOUT} 秒还短"
    finally:
        scopes.revoke(jti)
