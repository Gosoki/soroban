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
import os
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
    # 手填汇率。**不是权限问题，是语义问题**：`manual` 那个源名是核心保留给人的，
    # `ingest/kinds/fx_rate.py` 明文拒绝插件用它。给这条开插件入口 = 给那道检查开后门。
    # 插件要报汇率走通用写入通道的 fx_rate，会记成它自己的源名。
    "POST /api/fx",
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
        left = scopes._ALIVE[jti][1] - __import__("time").monotonic()   # (plugin_id, 到期时刻)
        assert left > mod._REAP_TIMEOUT, \
            f"令牌只活 {left:.0f} 秒，比子进程允许跑的 {mod._REAP_TIMEOUT} 秒还短"
    finally:
        scopes.revoke(jti)


def test_a_manifest_cannot_buy_itself_a_long_lived_token():
    """TTL 的**上界**不能让清单说了算。

    `timeout` 是 plugin.toml 里的一个数字，插件作者随手写 `timeout = 604800`
    就换来一枚**一周有效**的令牌——而这枚令牌此刻已经落在插件的日志、
    环境变量和子进程命令行里了。上界必须由核心的 `_HARD_TTL` 说了算。

    与上面那条是**同一个量的两端**：下界防「令牌比子进程先死」（回灌 401、
    整批订单静默丢失），上界防「子进程早没了，令牌还能用一周」。
    补这一条之前只有下界：把 `min(..., _HARD_TTL)` 整个删掉，全套一条都不红。
    """
    import time

    from jose import jwt

    from app.config import settings
    from app.plugins import scopes

    class _U:
        id, username = 1, "u"

    tok, jti = scopes.issue(_U(), "demo", set(), timeout_s=604800)   # 清单写了一周
    try:
        cap = scopes._HARD_TTL.total_seconds()
        left = scopes._ALIVE[jti][1] - time.monotonic()   # (plugin_id, 到期时刻)
        assert left <= cap + 1, f"清单要多久就给多久：这枚令牌还能活 {left / 3600:.1f} 小时"
        # 进程内的撤销表会随重启清空，而**令牌本身**是带着 exp 出门的那一份，
        # 两处都要封顶（只钉一处，另一处改坏了照样不红）。
        claims = jwt.decode(tok, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        assert claims["exp"] - claims["iat"] <= cap + 1
    finally:
        scopes.revoke(jti)

    # 反面：没顶到上界时仍按 timeout+120 走。少了这一句，把上界写成
    # 「恒定 40 分钟」也能过——那会让每枚令牌都活满 40 分钟。
    _, short = scopes.issue(_U(), "demo", set(), timeout_s=600)
    try:
        assert scopes._ALIVE[short][1] - time.monotonic() <= 600 + 120 + 1
    finally:
        scopes.revoke(short)


def test_a_token_nobody_revoked_still_dies_on_its_own(monkeypatch):
    """`alive()` 里那句过期检查是**唯一**一道不依赖别人来收拾的闸——零覆盖（实测：
    把 `if exp < time.monotonic()` 换成 `if False`，全套一条都不红）。

    正常路径上令牌由 `_reap` 的 finally `revoke()` 掉，所以过期检查平时轮不到。
    但确实有几条路没人来收：
      · `_launch` 起收割线程失败（OS 拒绝建线程）——那条路明确写了「不调 on_done」，
        也就没有任何人会 revoke；
      · `run_command` 里 `_launch` 抛 HTTPException（缺 venv / Popen 失败）时，
        只有 `except PluginBusy` 那一支会 revoke，别的异常不会。
    这些情况下 `_ALIVE` 里那一条如果永不过期，它就**一直有效到进程重启**。
    """
    import time as _time

    from app.plugins import scopes

    class _U:
        id, username = 1, "u"

    _, jti = scopes.issue(_U(), "demo", set(), timeout_s=60)
    try:
        assert scopes.alive(jti) is True, "刚签发就说它死了"
        # 拨到 TTL 之后：60+120 秒，多给一点余量
        base = _time.monotonic()
        monkeypatch.setattr(scopes.time, "monotonic", lambda: base + 60 + 120 + 5)
        assert scopes.alive(jti) is False, "过了 exp 还认这枚令牌"
        assert jti not in scopes._ALIVE, "过期项没被顺手清掉，表会一直长"
    finally:
        scopes.revoke(jti)


def test_kind_to_scope_map_is_pinned():
    """每种 kind 对应哪一枚权限，**钉死在这里**。

    `register()` 只校验「这个 scope 存在」，不校验「这是不是最窄的那一枚」。
    把 `plugin.record` 的 scope 从 `data:own` 改成 `staging:write` 照样注册成功，
    而效果是：任何拿到写暂存权限的插件，都能读写**别的插件**的私有存储——
    用户在卡片上勾的是「写暂存订单」，实际给出去的远不止。
    这类扩权在代码里只是一个单词的差别，review 时极易漏过；钉一张表，改了就红。

    加新 kind 时把它加进来，并在 review 里回答一句：这枚权限是**能表达该操作的最窄**的吗？
    """
    ingest.load_kinds()
    expected = {
        "fx.rate": "fx:write",          # 只写汇率表
        "plugin.record": "data:own",    # 只写本插件自己的私有存储
    }
    actual = {k: h.scope for k, h in ingest.KINDS.items()}
    assert actual == expected, (
        f"kind→scope 映射变了：{actual}。若是有意改动，请连带更新这张表，"
        f"并确认新权限是能表达该操作的**最窄**的一枚。"
    )


def test_by_kind_sentinel_is_used_by_exactly_one_route():
    """`x-scope: "*by-kind*"` 是个哨兵：中间件对它**只验「有没有任何权限」**，
    真正的判定交给该路由自己按 kind 做（见 routers/ingest.py）。

    也就是说，任何挂上这个哨兵、却没有自己那层 kind 级判权的路由，
    等于对**所有**持令牌的插件敞开。今天只有 `POST /api/plugins/ingest` 有这层判权，
    所以哨兵只允许出现一次；再多一条就必须先回答「它的第二层闸在哪」。
    """
    from app.main import app

    marked = [f"{sorted(r.methods)} {r.path}" for r in scopes._iter_routes(app)
              if (r.openapi_extra or {}).get("x-scope") == "*by-kind*"]
    assert len(marked) == 1, (
        f"`*by-kind*` 哨兵出现在 {len(marked)} 条路由上：{marked}。"
        f"每一条都必须自带 kind 级判权，否则它对所有持令牌的插件是敞开的。"
    )


def test_a_route_without_an_x_scope_is_closed_to_plugin_tokens(client, session):
    """**默认拒绝**是这套权限的地基：没显式挂 `x-scope` 的路由，插件令牌一律进不去。
    有了它，将来谁 include 一个新 router 都不会悄悄扩大插件的权限面。

    挑 `DELETE /api/orders/{id}` 来测，是因为它正是 scopes 模块 docstring 里点名的
    那个场景——「抓取插件里一个写错的 URL 把 DELETE 发到了 `/api/orders/1`」。

    ⚠️ **必须先断言这条路由真的没挂 x-scope。** 挑到有声明的那种，`need` 非 None，
    于是把闸门从「默认拒绝」翻成「默认放行」也照样测不出来。实测：把 main.py 里
    `ok = matched and need is not None and (…)` 换成 `matched and (need is None or …)`，
    全套 1065 条**一条都不红**——这条地基在此之前是零覆盖的。
    """
    from app.main import app
    from app.plugins import scopes

    hit = [r for r in scopes._iter_routes(app)
           if r.path == "/api/orders/{order_id}" and "DELETE" in (r.methods or set())]
    assert len(hit) == 1, "路由变了：换一条**没挂** x-scope 的写操作来测"
    assert not (hit[0].openapi_extra or {}).get("x-scope"), \
        "这条路由现在挂了 x-scope，默认分支就测不到了——换一条没挂的"

    class _U:
        id, username = 1, "admin"

    # 把核心已知的权限**全都**给它：仍然要被挡住，因为这条路由一项都没声明。
    tok, _ = scopes.issue(_U(), "anyplugin", set(scopes.SCOPES))
    h = {"Authorization": f"Bearer {tok}"}
    r = client.delete("/api/orders/1", headers=h)
    assert r.status_code == 403, \
        f"没挂 x-scope 的删除接口对插件敞开了：{r.status_code} {r.text[:200]}"

    # **反面**：不能靠「插件令牌什么都进不去」来满足上面那条断言。
    # 声明了 orders:read 的那条路由，同一枚令牌必须进得去。
    assert client.get("/api/orders", headers=h).status_code == 200


def test_only_scopes_the_manifest_declared_can_take_effect(client, session):
    """三重交集里的**∩清单声明**与**∩核心已知**：两项都零覆盖（实测各自删掉，全套全绿）。

      ∩清单声明 —— 用户在界面上多勾了（或库里存着一份手改过的 granted_scopes），
                    插件自己都没说要的权限不该生效。
      ∩核心已知 —— 核心删掉某个 scope 之后，库里存着的旧授权自动失效。

    这两项都只在「授权集比清单声明的大」时才起作用，而已有测试全都是
    「授权集 ⊆ 清单声明」，于是交集去掉哪一项结果都一样。
    """
    import json as _json

    from app.plugins import scopes

    class _Cfg:
        granted_scopes = _json.dumps(["fx:write", "orders:read", "core:已删除的旧权限"])

    # 那个「核心已不认识的旧权限」**必须同时写进 declared**，否则它先被
    # 「∩清单声明」挡掉，`∩核心已知` 那一项一次都没参与判定——把它删掉照样不红。
    # （第一版就是这么写的，变异测试当场把这条守卫本身判成零覆盖。）
    eff = scopes.token_scopes({"scopes": ["fx:write", "core:已删除的旧权限"]}, _Cfg())
    assert "fx:write" in eff, "既声明又授权的那项必须在（否则下面两条是恒真的）"
    assert "orders:read" not in eff, "插件从没声明过的权限被授权生效了"
    assert "core:已删除的旧权限" not in eff, "核心已经不认识的旧授权还在生效"


# --- 基础设施权限（baseline）---------------------------------------------------

def test_plugin_token_can_always_read_the_ingest_contract(client, session):
    """`GET /api/plugins/contract` 是「插件自我投影」的地基，任何插件令牌都必须进得去。

    令牌按 `cmd.needs` 收窄之后，仓库里两个插件四条命令**没有任何一条**把 meta:read
    写进 needs → 这条自检接口对所有插件恒 403，地基根本没浇上。
    而它返回的是纯元数据（kind 名、字段名、批量上限），零业务数据。

    ⚠️ 这条测试必须用 `scopes.issue()` **正常签发**的令牌，不能手工拼一个带 meta:read 的
    ——原先的守卫就是那么写的，于是它绕过了签发路径，一直是绿的，而真实插件全部 403。
    """
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "anyplugin", set())     # 一项授权都没有
    r = client.get("/api/plugins/contract", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, f"零授权的插件读不到 contract：{r.status_code} {r.text}"
    assert "kinds" in r.json()


def test_plugin_token_can_always_read_status_rules(client, session):
    """同上：状态机规则是淘宝插件同步已导入订单状态的依据，拿不到就静默同步不上。"""
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "anyplugin", set())
    r = client.get("/api/meta/status-rules", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, f"零授权的插件读不到状态机规则：{r.status_code}"


def test_reading_fx_needs_its_own_scope(client, session):
    """汇率读取**不是**核心元数据：它是业务数据，要单独授权。

    `meta:read` 曾经把 `/api/fx`、`/api/fx/history` 一起开着，而它的名字与文案都在说
    「只读规则」。拆成 `fx:read` 之后，baseline 就真的只剩零业务数据的那部分。
    """
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    bare, _ = scopes.issue(_U(), "anyplugin", set())
    assert client.get("/api/fx", headers={"Authorization": f"Bearer {bare}"}).status_code == 403
    ok, _ = scopes.issue(_U(), "anyplugin", {"fx:read"})
    assert client.get("/api/fx", headers={"Authorization": f"Bearer {ok}"}).status_code == 200


def test_baseline_scopes_are_not_offered_for_granting():
    """baseline 项不出现在授权勾选框里——勾一个「反正都有」的框只会制造疑惑。"""
    from app.plugins import scopes

    offered = {s["key"] for s in scopes.describe()}
    assert "meta:read" not in offered
    assert offered == set(scopes.SCOPES) - set(scopes._BASELINE)


def test_token_scopes_and_issue_agree_on_baseline():
    """`token_scopes()`（算 blocked 用）与 `issue()`（真发令牌）必须同口径。

    不一致的后果：把 baseline 项写进 `needs` 的命令，卡片上按钮永久禁用且无法自救
    （用户勾不到那一项），而 `issue()` 其实照发不误——界面说没权限，实际有。
    """
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    effective = scopes.token_scopes({"scopes": []}, None)
    tok, _ = scopes.issue(_U(), "p", set())
    import jose.jwt as jwt

    from app.config import settings
    scp = set(jwt.decode(tok, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])["scp"])
    assert set(scopes._BASELINE) <= effective, "token_scopes 漏了 baseline → 按钮会永久变灰"
    assert set(scopes._BASELINE) <= scp, "issue 漏了 baseline"


# --- 关停时收掉在飞的子进程 ---------------------------------------------------

def test_shutdown_reaps_inflight_plugin_processes():
    """进程关停必须收掉在飞的插件子进程，否则它们变成 PPID=1 的孤儿。

    `_launch` 用 `start_new_session=True` 起进程（新会话），而收割线程是 daemon
    ——主进程一退出收割线程立刻消失，子进程却还活着，且**再没有任何人执行那个
    30 分钟超时**。对浏览器类插件这意味着一个 chromium 永久留在后台：
    用户「关掉了 soroban」，内存里还躺着几百 MB，而任务管理器里那个进程
    与 soroban 已经毫无关联，没人猜得到该去杀谁。
    """
    import subprocess
    import sys
    import time

    from app.routers import plugins as mod

    # 起一个真的、会赖着不走的子进程（睡 300 秒），走与 _launch 相同的创建参数
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        text=True,
    )
    try:
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS[proc.pid] = (proc, "test/sleeper")
        assert proc.poll() is None, "子进程没起来，这条测试什么都没验"

        n = mod.shutdown_plugins(grace=2.0)
        assert n == 1, f"shutdown_plugins 说收了 {n} 个"

        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None, "关停之后子进程还活着——它会变成孤儿"
        with mod._PROCS_LOCK:
            assert proc.pid not in mod._ALIVE_PROCS, "注册表没清干净"
    finally:
        if proc.poll() is None:                 # 兜底：只动我自己起的这一个 pid
            proc.kill()
            proc.wait(timeout=5)


def test_shutdown_is_a_noop_when_nothing_is_running():
    """没有在飞进程时不该做任何事（也不该抛）。"""
    from app.routers import plugins as mod

    with mod._PROCS_LOCK:
        mod._ALIVE_PROCS.clear()
    assert mod.shutdown_plugins() == 0


def test_lifespan_reaps_before_disposing_the_pool():
    """顺序：先收子进程、再关连接池。

    插件可能正在通过 HTTP 回灌，而回灌走的是同一个连接池——顺序反了的话
    那些请求会撞上一个已经 dispose 的池，日志里刷一片无意义的异常。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert src.index("shutdown_plugins()") < src.index("checkpoint_and_dispose()")


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有进程组语义，走 taskkill 分支")
def test_a_grandchild_is_not_orphaned_when_the_child_exits_first():
    """**子进程先退、孙进程还在**时，孙进程必须被收掉。

    这是真实的失败形状：插件跑完自己退了（或崩了），而它拉起的 chromium 还开着。
    原先三层叠加把它漏得干干净净——
      · `_reap` 一 wait 到子进程就把它移出 `_ALIVE_PROCS`；
      · `shutdown_plugins` 的 `if proc.poll() is None` 判假 ⇒ 整个 `_kill_tree` 跳过；
      · 就算调到 `_kill_tree`，它第一步 `os.getpgid(proc.pid)` 也已经 `[Errno 3]`，
        走「只杀单个进程」的降级分支，而那个进程早没了。
    结果是用户机器上留下一个与 soroban 已无任何关联的浏览器，没人猜得到该去杀谁。

    修法靠的是「起进程时就把 pgid 记下来」——回收之后再查是查不到的。
    """
    import signal
    import subprocess
    import time

    from app.routers import plugins as mod

    # 子进程起完「浏览器」自己就退了；孙进程继续活着
    child = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; exit 0"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, text=True)
    grandchild = int(child.stdout.readline().strip())

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    try:
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS[child.pid] = (child, "test/orphan")
            mod._remember_group(child.pid, "test/orphan")
        assert child.pid in mod._OWN_GROUP, "起进程时没把进程组记下来，后面全白搭"

        child.wait(timeout=5)                       # 子进程退出并被回收
        assert alive(grandchild), "用例前提不成立：孙进程应该还活着"

        mod.shutdown_plugins(grace=0.5)
        for _ in range(40):                         # 给信号一点时间落地
            if not alive(grandchild):
                break
            time.sleep(0.05)
        assert not alive(grandchild), \
            f"孙进程 {grandchild} 成了孤儿——它与 soroban 已无关联，用户找不到该杀谁"
    finally:
        if alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)     # 只动我自己起的这一个 pid
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有进程组语义")
def test_the_reaper_sweeps_the_group_right_after_the_child_exits():
    """不必等到关停——收割线程 wait 到子进程就该扫一次它的进程组。

    只在关停时扫是不够的：`_reap` 一 wait 到就把这条移出 `_ALIVE_PROCS`，
    从那一刻起关停路径根本看不到它。**子进程退出的那一刻就是最后时机。**

    而且这一扫必须排在两个 `join(5)` **之前**。那两个 join 恰恰是因为
    「孙进程还攥着管道、EOF 不来」才存在的——排在它们后面，回收实测要晚 **10.05 秒**
    （原先的写法就是这样，而当时的函数名与 docstring 都写着「立刻」）。
    那 10 秒里 `_INFLIGHT` 还攥着（用户点「再跑一次」吃 409）、卡片多顶 10 秒「执行中」、
    令牌多活 10 秒。提前之后实测 0.05 秒，而插件的结果行一字不丢。
    """
    import signal
    import subprocess
    import time

    from app.routers import plugins as mod

    child = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; exit 0"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, text=True)
    grandchild = int(child.stdout.readline().strip())

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    try:
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS[child.pid] = (child, "test/reaper-sweep")
            mod._remember_group(child.pid, "test/reaper-sweep")
        mod._reap(child, "test/reaper-sweep", "test/reaper-sweep")   # 真的收割线程逻辑，不是桩
        assert not alive(grandchild), \
            f"收割完没扫进程组，孙进程 {grandchild} 活到了关停之外"
        assert child.pid not in mod._ALIVE_PROCS, "注册表没清干净"
    finally:
        if alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有进程组语义")
def test_launch_records_the_group_so_the_whole_chain_works(tmp_path, monkeypatch):
    """整条链走一遍：`_launch` 起进程 → 记下 pgid → 子进程退出 → 收割线程扫掉孙进程。

    上面两条各自打在 `_sweep_group` 的两个调用点上，但都是**手工**往
    `_OWN_GROUP` 里塞的——把 `_launch` 里那句 `_remember_group` 删掉，它们照样绿。
    这条从 `_launch` 进去，钉住「记 pgid」这个动作本身。

    只替换 `subprocess.Popen`，让它返回一个**真的**子进程句柄：假 proc 没有真实的
    进程组，测不出这件事。
    """
    import signal
    import subprocess
    import time

    from app.routers import plugins as mod

    real = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; exit 0"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, text=True)
    grandchild = int(real.stdout.readline().strip())

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    try:
        monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: real)
        # 直接造一份清单：本文件没有插件夹具，而 Popen 已经被替换，
        # 所以 argv/cwd 走不到真执行，只需要 `python = inherit`（解释器存在）与一个 _dir。
        m = {"id": "grp", "name": "组测试", "python": "inherit", "entry": "-m x",
             "_dir": tmp_path}
        assert mod._launch(m, "run", []) == real.pid
        # **断言结果，不断言中间状态。** 第一版断言 `real.pid in mod._OWN_GROUP`，
        # 而收割线程扫完组就会立刻把它摘掉——回收提前到 join 之前（0.05s）之后，
        # 那句断言变成了在跟收割线程赛跑，而且它读的还是一个「本来就该被清掉」的状态。
        # 要的东西其实是「孙进程有没有被收掉」，直接等它就行。
        for _ in range(100):
            if not alive(grandchild):
                break
            time.sleep(0.05)
        assert not alive(grandchild), \
            "孙进程没被收掉——起进程时没把 pgid 记下来，子进程一被回收就再也查不到了"
    finally:
        if alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()
            mod._INFLIGHT.clear()


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有进程组语义")
def test_a_process_that_shares_our_group_is_never_recorded():
    """`start_new_session` 万一没生效，**绝不能**把那个 pgid 记下来。

    没生效时 pgid 是继承来的父进程组——也就是**后端自己所在的组**。
    对它发 killpg 就是把 uvicorn 连同整个终端一起带走。
    所以 `_remember_group` 必须验一次 `pgid == pid`，验不过就不记，
    后续所有按组回收自动降级成不动。

    这条闸在正常路径上永远不触发（`_launch` 一直带 start_new_session），
    所以只能靠守卫钉住——而它的失败后果是**杀掉后端自己**，属于最贵的那一类。
    """
    import subprocess

    from app.routers import plugins as mod

    # 故意**不带** start_new_session：它会落在后端自己的进程组里
    p = subprocess.Popen(["sleep", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert os.getpgid(p.pid) != p.pid, "用例前提不成立：它应该继承父进程组"
        with mod._PROCS_LOCK:
            mod._remember_group(p.pid, "test/shared-group")
        assert p.pid not in mod._OWN_GROUP, \
            "把后端自己所在的进程组记下来了——按组杀会把 uvicorn 一起带走"
        assert mod._sweep_group(p.pid, "test/shared-group", "守卫测试") == 0, \
            "没记下来却仍然去动了那个组"
        assert p.poll() is None, "它被误杀了"
    finally:
        p.kill()
        p.wait(timeout=5)
        with mod._PROCS_LOCK:
            mod._OWN_GROUP.clear()


def test_a_rejection_at_the_ingest_endpoint_reaches_the_card(client, session):
    """**整条链**：插件推坏数据 → 核心逐条拒收 → 卡片如实说出来。

    上面几条只测了 `runlog.note_rejected` 之后的半截；这一条从**真的 ingest 端点**
    进去，钉住「拒收会不会被记下来」这个动作本身——漏了它，端点照样 200、
    `summary` 里照样写着 rejected、日志照样有一行，而卡片什么都不会说。

    这是 F03 的核心：拒收信息一直都返回给插件了，问题在于**卡片显示的是插件自报的**。
    """
    from app.plugins import runlog
    from app.routers import plugins as mod

    ingest.load_kinds()
    if session.get(PluginConfig, "fx") is None:      # 卡片那一行，_write_outcome 要它存在
        session.add(PluginConfig(plugin_id="fx"))
        session.commit()
    headers, jti = _plugin_client(client, session, {"fx:write"})
    try:
        r = client.post("/api/plugins/ingest", headers=headers, json={
            "kind": "fx.rate", "items": [{"rate": "不是数字", "source": "boc"}]})
        assert r.status_code == 200, r.text
        assert r.json()["summary"].get("rejected") == 1, r.json()

        rec = runlog.peek(jti)
        assert rec and rec["rejected"] == 1, \
            f"端点拒收了，核心却没记下来（runlog={rec}）——卡片将什么都不说"

        # 插件这时自报「成功」：卡片必须以核心看到的为准
        mod._result_writer("fx", "抓取", run=jti)(True, "已导入 1 条")
        cfg = session.get(PluginConfig, "fx")
        session.refresh(cfg)
        assert cfg.last_outcome != "ok", "插件自报成功就显示成功"
        assert "核心拒收 1 条" in (cfg.last_summary or ""), cfg.last_summary
    finally:
        scopes.revoke(jti)
        runlog.reset()


@pytest.mark.skipif(os.name == "nt", reason="Windows 没有进程组语义")
def test_the_group_sweep_happens_before_the_output_drains():
    """按组回收必须排在两个 `join(5)` **之前**，而且不许吃掉插件的结果行。

    那两个 join 存在的理由正是「孙进程还攥着管道、EOF 不来」——把回收排在它们后面，
    就得先等满 10 秒。实测：排在后面 10.05s，排在前面 0.05s。
    这 10 秒不是纯等待，期间 `_INFLIGHT` 还攥着（用户点「再跑一次」吃 409）、
    卡片多顶 10 秒「执行中」、令牌多活 10 秒、那个浏览器也多开 10 秒。

    **同时钉住「别为了快把结果吃掉」**：插件的 stdout 最后一行是结果 JSON，
    收割靠它算 outcome。这条断言比时间那条更要紧——快而丢结果是净损失。
    """
    import subprocess
    import time

    from app.routers import plugins as mod

    child = subprocess.Popen(
        ["sh", "-c", 'sleep 60 & echo $!; echo \'{"ok": true, "created": 3}\'; exit 0'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, text=True)
    grandchild = int(child.stdout.readline().strip())
    got = []
    try:
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS[child.pid] = (child, "test/drain-order")
            mod._remember_group(child.pid, "test/drain-order")
        t0 = time.monotonic()
        mod._reap(child, "test/drain-order", "test/drain-order",
                  on_done=lambda ok, summary, warn=False: got.append((ok, summary)))
        spent = time.monotonic() - t0

        assert spent < 5, f"回收排在 join(5) 后面了——实测 {spent:.2f}s"
        assert got and got[0][0] is True, f"结果行被吃掉了：{got}"
        assert "3" in got[0][1], f"插件报的数字没读到：{got}"
    finally:
        try:
            os.kill(grandchild, 9)
        except OSError:
            pass
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()


# --- 核心侧事实：REST 通道也要记 -------------------------------------------------

def test_core_refusals_on_the_rest_channel_are_recorded(client, session):
    """卡片上显示的是插件自己 print 的那句话。一个不看回执的插件——推 30 条、
    核心逐条拒收、照常 print「已导入 30 单」并 exit 0——会让卡片显示绿色的成功而库里零写入。

    `runlog` 就是为这件事建的，但它此前**只覆盖 `/api/ingest`**，
    而唯一批量写数据的淘宝插件走的是 REST（`/api/staging`）——保证装在了不需要它的那一侧。
    """
    import uuid

    from app.plugins import runlog, scopes

    class _U:
        id, username = 1, "admin"

    runlog.reset()
    tok, jti = scopes.issue(_U(), "demo", {"staging:write", "staging:read"})
    h = {"Authorization": f"Bearer {tok}"}
    try:
        # 422：schema 挡下（`import_status` 不许建行时自称已导入）
        r = client.post("/api/staging", json={"order_no": "REST-" + uuid.uuid4().hex[:6],
                                              "import_status": "已导入"}, headers=h)
        assert r.status_code == 422, r.text
        rec = runlog.peek(jti)
        assert rec and rec["rejected"] == 1, f"REST 侧的拒收没被记下来：{rec}"
        assert "api/staging" in rec["first"], rec["first"]

        # **反面**：409 刻意不记。撞唯一键在淘宝插件那里是「这单已经在暂存里了」
        # 这个正常结局（它靠 409 做幂等 upsert）——记了的话每次正常抓取卡片都变黄，
        # 而黄色一旦成为常态，真正的黄就没人看了。
        no = "RESTDUP-" + uuid.uuid4().hex[:6]
        assert client.post("/api/staging", json={"order_no": no}, headers=h).status_code == 200
        before = (runlog.peek(jti) or {}).get("rejected", 0)
        dup = client.post("/api/staging", json={"order_no": no}, headers=h)
        assert dup.status_code == 409, dup.text
        after = (runlog.peek(jti) or {}).get("rejected", 0)
        assert after == before, f"409 被记成拒收了（{before} → {after}）"

        # **反面二**：正常写入不许留下任何痕迹
        ok_no = "RESTOK-" + uuid.uuid4().hex[:6]
        assert client.post("/api/staging", json={"order_no": ok_no}, headers=h).status_code == 200
        assert (runlog.peek(jti) or {}).get("rejected", 0) == after
    finally:
        scopes.revoke(jti)
        runlog.reset()


def test_a_scope_violation_is_also_a_core_side_fact(client, session):
    """越权被闸挡下（403）同样要记：那正是「核心拒绝了插件这一次」最典型的一种，
    而插件那边只看到一个 403，用户只看卡片。
    """
    from app.plugins import runlog, scopes

    class _U:
        id, username = 1, "admin"

    runlog.reset()
    tok, jti = scopes.issue(_U(), "demo", set())      # 一项授权都没有
    try:
        r = client.post("/api/staging", json={"order_no": "NOPERM-1"},
                        headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, r.text
        rec = runlog.peek(jti)
        assert rec and rec["rejected"] == 1, f"越权没被记成核心侧事实：{rec}"
    finally:
        scopes.revoke(jti)
        runlog.reset()


def test_a_rejected_item_does_not_poison_its_key_for_the_rest_of_the_batch(client, session):
    """同一批里，前一条被拒**不许**让后一条同键的有效项被跳过。

    `seen.add(k)` 原先排在 savepoint **之前**，回滚时又不撤销：一条被拒的项会把自己的键
    毒掉，后面那条同键的有效项直接短路成 `unchanged` +「本轮已提交过同一条」——
    **而那是假话**，那个键一个字都没写进去。更糟的是 `unchanged` 算成功：
    `summary["rejected"]` 少数一条、`runlog.note_rejected` 也只被告知一次损失。

    **前提必须是「在 `apply` 里面被拒」**，不能是 schema 层被拒——后者在
    `handler.key()` 之前就 return 了，根本碰不到 `seen`。
    第一版拿越界汇率做这条，而汇率的范围在 schema 上就卡了：走的是早退路径，
    于是把 `seen.add` 挪回原位它照样绿。这里改用「插件私有存储」的**超大 blob**——
    那是由**载荷**决定的拒收（`plugin_record._MAX_BYTES`），而键由 kind+key 决定，
    两条项可以同键、一条被拒一条合法。
    """
    from app.plugins import scopes
    from app.services.ingest.kinds.plugin_record import _MAX_BYTES

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "poison-test", {"data:own"})
    hdr = {"Authorization": f"Bearer {tok}"}

    r = client.post("/api/plugins/ingest", headers=hdr, json={
        "kind": "plugin.record",
        "items": [
            {"kind": "snap", "key": "A", "data": {"blob": "x" * (_MAX_BYTES + 10)}},  # 超大 → 拒
            {"kind": "snap", "key": "A", "data": {"ok": 1}},                          # 同键，合法
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    first, second = body["results"]

    # 前提：第一条必须是**在 apply 里**被拒的（code 不是 schema 层那个 validation）
    assert first["status"] == "rejected", body
    # `code` 区分不了两条路——`apply` 里的 `Outcome(code="validation")` 与 schema 层用的是
    # 同一个 code。判据用**只有 `apply` 内部才会产生**的那句消息。
    assert "单条数据超过" in first["message"], (
        f"第一条不是在 apply 里被拒的（schema 层那条路在 handler.key() 之前就 return 了，"
        f"碰不到 seen）——这条测试的前提不成立：{first}")

    assert second["status"] != "unchanged", (
        f"第二条被前一条毒掉了：{second}（它说「已提交过」，而那个键什么都没写）")
    assert body["summary"].get("rejected") == 1 and second["status"] in ("created", "updated"), body


def test_a_plugin_cannot_rewrite_the_money_of_an_already_imported_row(client, session):
    """插件**不许**改一张已导入订单的金额字段。

    `PATCH /api/staging/{id}` 对已导入的行是「写穿账本」——暂存与账本是同一条记录的
    两个视图，人在哪一页改都该一致。**这对人是对的，对插件不是**：
    那笔钱可能是人导入后手工核过、改过的，而插件回灌是**定时反复**发生的，
    覆盖一次就永久了（HTTP 200、无 409、runlog 也不记）。

    仓库里那个淘宝插件自己很克制（已导入的行只推进状态、只在账本那格为空时补快递号），
    但那是**插件的自觉，不是核心的强制**——`runlog` 存在的理由逐字就是这一句。
    所以这条闸对现有插件一次都不会触发，它防的是下一个插件。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import select

    from app.models import Order, OrderStaging
    from app.plugins import scopes

    row = OrderStaging(order_no="PLG-MONEY-1", title="已导入的单",
                       price_cny=Decimal("100.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid = row.id
    o = client.post(f"/api/staging/{sid}/import").json()
    before = Decimal(str(o["price_cny"]))

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "money-test", {"staging:write"})
    hdr = {"Authorization": f"Bearer {tok}"}
    cur = client.get("/api/staging?q=PLG-MONEY-1").json()["items"][0]

    r = client.patch(f"/api/staging/{sid}", headers=hdr, json={
        "version": cur["version"],
        "items": [{"name": "改小", "quantity": 1, "unit_price_cny": "1"}],
    })
    assert r.status_code == 403, f"插件把已导入订单的钱改掉了：{r.status_code} {r.text}"
    assert "金额字段" in r.json()["detail"], r.text

    session.expire_all()
    order = session.exec(select(Order).where(Order.id == o["id"])).one()
    assert Decimal(str(order.price_cny)) == before, f"账本金额被改成了 {order.price_cny}"


def test_a_plugin_can_still_fill_the_blanks_on_an_imported_row(client, session):
    """反面：插件补空格（快递单号）**仍然要放行**。

    那正是它现在唯一会对已导入行做的事，也是最需要的一批单
    （要拿去和集运的「内含快递」截图对上）。一刀切会把这条路封死。
    """
    import datetime as dt
    from decimal import Decimal

    from app.models import OrderStaging
    from app.plugins import scopes

    row = OrderStaging(order_no="PLG-MONEY-2", title="补快递号",
                       price_cny=Decimal("50.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid = row.id
    client.post(f"/api/staging/{sid}/import")

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "money-test", {"staging:write"})
    cur = client.get("/api/staging?q=PLG-MONEY-2").json()["items"][0]
    r = client.patch(f"/api/staging/{sid}", headers={"Authorization": f"Bearer {tok}"},
                     json={"version": cur["version"], "express_no": "SF9988776655"})
    assert r.status_code == 200, f"插件补快递号被挡了：{r.text}"


def test_revoking_a_grant_also_kills_the_token_already_in_flight(client, session, monkeypatch):
    """在界面上收回授权时，**正在跑的那枚令牌也要当场作废**。

    用户唯一一次真的去点撤销，恰好是他正看着这个插件在乱写的时候。
    而那一刻如果只改数据库里的 `granted_scopes`，正在跑的子进程会拿着旧令牌
    一直写到跑完（`_reap` 最长等 30 分钟）——**卡片上却已经显示「未授权」了**。
    「用户唯一一次真的去用它，恰好是它什么都不做的那一次。」
    """
    from app.models import PluginConfig
    from app.plugins import scopes

    pid = "soroban-plugin-taobao"
    cfg = session.get(PluginConfig, pid) or PluginConfig(plugin_id=pid)
    cfg.granted_scopes = '["staging:read", "staging:write"]'
    session.add(cfg)
    session.commit()

    # 测试环境的插件目录是空的（conftest 刻意隔离），端点在 `_find_manifest` 就会 404。
    # 只替换「找清单」这一步，授权与撤销的判定逻辑跑的是真代码。
    from app.routers import plugins as mod

    monkeypatch.setattr(mod, "_find_manifest",
                        lambda _id: {"id": pid, "scopes": ["staging:read", "staging:write"]})

    class _U:
        id, username = 1, "admin"

    tok, jti = scopes.issue(_U(), pid, {"staging:read", "staging:write"})
    assert scopes.alive(jti), "刚签发就不在飞？前提不成立"

    r = client.put(f"/api/plugins/{pid}/grants", json={"granted": ["staging:read"]})
    assert r.status_code == 200, r.text
    assert r.json().get("revoked_running") == 1, r.json()
    assert not scopes.alive(jti), "收回授权之后，在飞的那枚令牌还活着"


def test_adding_a_grant_does_not_interrupt_a_running_plugin(client, session, monkeypatch):
    """反面：**加**授权不许打断正在跑的那次——那是帮倒忙。

    没有这条的话，「变更即撤销」也能让上面那条绿，而用户每次勾一个新权限
    都会把手头正在跑的抓取掐断。
    """
    from app.models import PluginConfig
    from app.plugins import scopes

    pid = "soroban-plugin-taobao"
    cfg = session.get(PluginConfig, pid) or PluginConfig(plugin_id=pid)
    cfg.granted_scopes = '["staging:read"]'
    session.add(cfg)
    session.commit()

    from app.routers import plugins as mod

    monkeypatch.setattr(mod, "_find_manifest",
                        lambda _id: {"id": pid, "scopes": ["staging:read", "staging:write"]})

    class _U:
        id, username = 1, "admin"

    tok, jti = scopes.issue(_U(), pid, {"staging:read"})
    r = client.put(f"/api/plugins/{pid}/grants",
                   json={"granted": ["staging:read", "staging:write"]})
    assert r.status_code == 200, r.text
    assert r.json().get("revoked_running") == 0, r.json()
    assert scopes.alive(jti), "只是加了个权限，却把正在跑的那次掐断了"


def test_a_failed_launch_does_not_leak_a_live_token(monkeypatch):
    """`_launch` 失败时那枚令牌必须当场作废——**不管是因为什么失败**。

    原先只有 `except PluginBusy` 那一支收。而 `_launch` 还会因为别的原因抛，
    其中「无法创建收割线程」那一支是在 `Popen` **成功之后**才抛的——
    子进程已经带着 `SOROBAN_TOKEN` 跑起来了，却没有任何收割线程会去 revoke 它。
    那枚令牌就一直被 `_plugin_gate` 认到 TTL 到期（最长 30 分钟）。

    这条直接测那个循环体的语义：签发 → `_launch` 抛非 PluginBusy → jti 不许还活着。
    """
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    tok, jti = scopes.issue(_U(), "leak-test", {"staging:read"})
    assert scopes.alive(jti)

    def _boom(*a, **kw):
        raise RuntimeError("启动插件失败（无法创建收割线程）")

    # 复现循环体：起不来就作废，别管抛的是哪一种
    try:
        try:
            _boom()
        except Exception:
            scopes.revoke(jti)
            raise
    except RuntimeError:
        pass
    assert not scopes.alive(jti), "启动失败之后令牌还活着"


def test_the_launch_loop_revokes_on_every_failure_path():
    """上面那条测的是语义，这条钉的是**真实代码里确实有那一支**。

    按 AST 找 `run_command` 里包着 `_launch(...)` 的那个 try，
    要求它的 except 分支**全部**都会 `scopes.revoke(...)`——
    漏掉任何一支，那条路径上的令牌就会活到 TTL 到期。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "plugins.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
             and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_launch"
                     for c in ast.walk(n) if isinstance(c, ast.Call))]
    # 只看直接包着 _launch 调用、且带 except 的那些
    tries = [t for t in tries if t.handlers]
    assert tries, "没找到包着 _launch 的 try —— 探测方式可能已过期"
    bad = []
    for t in tries:
        for h in t.handlers:
            has_revoke = any(isinstance(c, ast.Call)
                             and getattr(c.func, "attr", "") == "revoke"
                             for c in ast.walk(h))
            if not has_revoke:
                name = getattr(h.type, "id", None) or getattr(h.type, "attr", "?")
                bad.append(f"第 {h.lineno} 行的 except {name} 没有 revoke 令牌")
    assert not bad, ("`_launch` 的这些失败分支没有作废令牌：\n  " + "\n  ".join(bad)
                     + "\n（进程没起来就没人会 revoke，那枚令牌会活到 TTL 到期。）")


def test_shipment_read_does_not_hand_over_the_orders_that_orders_read_gates(client, session):
    """只给了「读集运单」的插件，**不该**从集运列表里拿到账本订单的明细。

    用户取消勾选「读商品订单」，界面上就是这么写的；而 `GET /api/shipment` 会把每张
    集运单挂着的订单号、标题、状态、结算日元、以及每件物品的名称/数量/单价原样发出去
    ——**他留下的那道闸，从另一个门整个绕开了**。

    这不是在收紧权限模型（插件都是用户自己写的、不防恶意插件），
    而是让那个勾选框说的话成立。人类登录不受影响。
    """
    import datetime as dt

    from app.plugins import scopes

    s = client.post("/api/shipment", json={"date": "2026-06-01", "shipment_no": "SCOPE-1",
                                           "shipment_status": "打包中"}).json()
    o = client.post("/api/orders", json={"date": "2026-06-01", "title": "机密商品",
                                         "order_no": "SCOPE-ORD-1",
                                         "purchase_status": "待收货"}).json()
    assert client.post(f"/api/shipment/{s['id']}/order/{o['id']}").status_code == 200

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "scope-test", {"shipment:read"})
    body = client.get("/api/shipment", params={"q": "SCOPE-1"},
                      headers={"Authorization": f"Bearer {tok}"}).json()
    mine = next(x for x in body["items"] if x["shipment_no"] == "SCOPE-1")
    assert mine["orders"] == [], f"只给了 shipment:read 却拿到了订单明细：{mine['orders']}"
    assert "机密商品" not in str(body), "订单标题还是漏出去了"

    # 反面一：**人类登录照旧看得到**（他本来就有全部权限）
    human = client.get("/api/shipment", params={"q": "SCOPE-1"}).json()
    hm = next(x for x in human["items"] if x["shipment_no"] == "SCOPE-1")
    assert len(hm["orders"]) == 1, "把人也一起挡了"

    # 反面二：给了 orders:read 的插件照旧看得到
    tok2, _ = scopes.issue(_U(), "scope-test", {"shipment:read", "orders:read"})
    body2 = client.get("/api/shipment", params={"q": "SCOPE-1"},
                       headers={"Authorization": f"Bearer {tok2}"}).json()
    m2 = next(x for x in body2["items"] if x["shipment_no"] == "SCOPE-1")
    assert len(m2["orders"]) == 1, "给了 orders:read 反而看不到了"


def test_patching_a_shipment_does_not_hand_back_what_orders_read_gates(client, session):
    """`PATCH /api/shipment/{id}` 的响应也要过 `_may_see_orders`——**它是第二个门**。

    `_may_see_orders` 的文档写的是 `GET /api/shipment`：用户取消勾选「读商品订单」，
    界面上就是这么写的，而列表会把每张集运单挂着的订单号、标题、状态、结算日元、
    以及每件物品的名称/数量原样发出去——「他留下的那道闸，从另一个门整个绕开了」。
    GET 那扇门当时堵上了，**同前缀的 PATCH 一直开着**。

    五个返回 `ShipmentRead` 的端点实测过一遍：列表自己过闸；
    GET 单条 / POST 挂订单 / DELETE 摘订单都因为没声明 `x-scope` 被中间件挡在 403；
    **只有 PATCH 既声明了 `x-scope`（插件进得来）、又回整份响应**。
    所以这条测试只钉 PATCH，并且顺带把「另外四个门确实进不来」也钉住——
    哪天有人给它们补上 `x-scope`，这里会当场提醒。

    发的是一个**什么字段都不改**的 PATCH（只带 version）：拿数据不需要真去改什么。
    """
    import datetime as dt

    from app.models import Order, ShipmentOrder
    from app.plugins import scopes

    SECRET = "绝密商品标题ABC"
    o = Order(date=dt.date(2027, 4, 1), title=SECRET, order_no="SECRET-ORDER-001",
              price_cny=100, fx_rate=20, purchase_status="待收货")
    o.compute_money()
    sh = ShipmentOrder(date=dt.date(2027, 4, 2), shipment_no="SH-SCOPE-PROBE")
    session.add(o)
    session.add(sh)
    session.commit()
    session.refresh(o)
    session.refresh(sh)
    o.shipment_order_id = sh.id
    session.add(o)
    session.commit()
    session.refresh(sh)

    class _U:
        id, username = 1, "admin"

    # 勾了「读集运单」+「改集运单」，**刻意没勾**「读商品订单」
    tok, jti = scopes.issue(_U(), "scope-probe", {"shipment:read", "shipment:update"})
    hdr = {"Authorization": f"Bearer {tok}"}
    try:
        r = client.patch(f"/api/shipment/{sh.id}", json={"version": sh.version}, headers=hdr)
        assert r.status_code == 200, f"夹具没走到那条路：{r.status_code} {r.text[:200]}"
        assert SECRET not in r.text and "SECRET-ORDER-001" not in r.text, (
            f"PATCH 把 orders:read 挡住的订单明细原样发回去了：{r.text[:400]}")
        assert r.json().get("orders") == [], f"orders 没有被收窄：{r.json().get('orders')}"

        # 另外四个门此刻确实进不来——哪天有人给它们补上 x-scope，这里会提醒
        closed = {
            "GET 单条": client.get(f"/api/shipment/{sh.id}", headers=hdr),
            "POST 挂订单": client.post(f"/api/shipment/{sh.id}/order/{o.id}", headers=hdr),
            "DELETE 摘订单": client.delete(f"/api/shipment/{sh.id}/order/{o.id}", headers=hdr),
        }
        for name, resp in closed.items():
            assert resp.status_code == 403, (
                f"{name} 现在放插件令牌进来了（{resp.status_code}）——"
                f"它也回整份 ShipmentRead，得跟 PATCH 一样过 `_may_see_orders`")
    finally:
        scopes.revoke(jti)

    # 反面：勾上 orders:read 就该看得见，否则这道闸是把插件一刀切了
    tok2, jti2 = scopes.issue(_U(), "scope-probe2",
                              {"shipment:read", "shipment:update", "orders:read"})
    try:
        r2 = client.patch(f"/api/shipment/{sh.id}",
                          json={"version": sh.version + 1},
                          headers={"Authorization": f"Bearer {tok2}"})
        assert r2.status_code == 200, r2.text
        assert SECRET in r2.text, "勾了「读商品订单」却还是看不到——这道闸把插件一刀切了"
    finally:
        scopes.revoke(jti2)


def test_one_plugin_with_a_bad_account_declaration_does_not_open_the_gate_for_the_others(
        client, session, tmp_path, monkeypatch):
    """一个插件的账号声明写错，**不许**让「这是插件管理的账号」这道闸对别的插件失效。

    `_plugin_owns_account` 原先整个循环共用一个 `try`。装一个把
    `accounts_ledger_field` 写成核心不认识的列名的第三方插件——而 `_ledger_field`
    对此会 `raise HTTPException(400, …)`，它是 `Exception` 的子类——
    只要它的目录名排在正常插件之前，循环就在第一圈中止、返回 False，
    **排在后面的插件根本没被检查过**。

    后果不是报错，是**静默错账**：改名的两道闸放行 → 账本里 `platform_account='甲'`
    全改成 '乙'、前端弹绿字「已改名」；而插件侧 `params_json` 里还是「甲」、
    磁盘 `.state/甲.json` 也还在 → 下一轮抓取继续写「甲」，
    同一个人的单从此劈成两半，按账号筛选和合计两边都不对，全程零报错。

    判据用 **A/B**，这是关键：先证明「只有正常插件时闸是拦住的」（基线为真），
    再加一个与这个账号毫无关系的坏插件，闸必须**照样拦住**。
    少了基线那一半，一条永远返回 False 的实现也能过。
    """
    import json
    import shutil
    from pathlib import Path

    from app.config import settings
    from app.models import PluginConfig
    from app.routers import plugins as pmod
    from app.routers.tags import _plugin_owns_account

    real = (Path(__file__).resolve().parents[2] / "plugins"
            / "soroban-plugin-taobao" / "plugin.toml")
    if not real.is_file():
        pytest.skip("仓库里没有淘宝插件清单，这条测试的前提不成立")

    def build(where, with_bad):
        where.mkdir(parents=True)
        g = where / "soroban-plugin-taobao"
        g.mkdir()
        shutil.copy(real, g / "plugin.toml")
        if with_bad:
            # 目录名排在 taobao **之前**——这正是触发条件
            b = where / "soroban-plugin-aaa"
            b.mkdir()
            (b / "plugin.toml").write_text(
                'id = "aaa"\nname = "坏声明"\nversion = "1.0.0"\npython = "inherit"\n'
                'accounts = true\naccounts_ledger_field = "platform_accounts"\n'
                '[[commands]]\nname = "run"\nlabel = "跑"\n', encoding="utf-8")
        return where

    # `merge` 而不是 `add`：同一个会话库里别的用例可能已经建过 taobao 那行，
    # `add` 会撞 `UNIQUE constraint failed: pluginconfig.plugin_id`——
    # 单跑绿、全量跑红，而红的原因与本条要测的事情毫无关系。
    session.merge(PluginConfig(plugin_id="taobao", params_json=json.dumps(
        {"accounts": [{"name": "甲", "platform": "淘宝", "enabled": True}]},
        ensure_ascii=False)))
    session.commit()

    def owns(root) -> bool:
        monkeypatch.setattr(settings, "PLUGIN_DIR", str(root))
        pmod._needs_cache.clear()
        return _plugin_owns_account(session, "platform_account", "甲")

    assert owns(build(tmp_path / "only", False)) is True, (
        "基线不成立：只有淘宝插件时这道闸就没拦住——那么下面那半也证明不了什么")
    assert owns(build(tmp_path / "both", True)) is True, (
        "多了一个与「甲」毫无关系的坏声明插件之后，这道闸就放行了——"
        "一个插件写错，把所有插件的账号保护一起关掉了")

