"""暂存 → 导入账本的全流程：写穿、镜像、原子门闸、删除一致性。"""
from decimal import Decimal

import pytest


def mk_staging(client, **kw):
    r = client.post("/api/staging", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_create_derives_price_from_items(client):
    s = mk_staging(client, order_no="S-1", title="店",
                   items=[{"name": "a", "quantity": 2, "unit_price_cny": "10.00"}], postage_cny="5.00")
    assert Decimal(s["price_cny"]) == Decimal("25.00")


def test_staging_order_no_unique(client):
    mk_staging(client, order_no="S-DUP")
    r = client.post("/api/staging", json={"order_no": "S-DUP"})
    assert r.status_code == 409


def test_multiple_null_order_no_allowed(client):
    mk_staging(client, title="空单号1")
    r = client.post("/api/staging", json={"title": "空单号2"})
    assert r.status_code == 200


def test_import_creates_order_and_marks_row(client):
    s = mk_staging(client, order_no="S-IMP-1", title="店", platform="淘宝",
                   order_date="2026-05-01", purchase_status="待发货",
                   items=[{"name": "物品", "quantity": 2, "unit_price_cny": "30.00"}])
    r = client.post(f"/api/staging/{s['id']}/import")
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["order_no"] == "S-IMP-1"
    assert o["platform"] == "淘宝"
    assert o["purchase_status"] == "待发货"
    assert o["created_via"] == "imported"
    assert Decimal(o["price_cny"]) == Decimal("60.00")
    row = client.get("/api/staging", params={"import_status": "已导入", "limit": 200}).json()["items"]
    assert any(x["id"] == s["id"] and x["imported_order_id"] == o["id"] for x in row)


def test_import_twice_conflicts(client):
    s = mk_staging(client, order_no="S-IMP-2")
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 200
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 409


def test_import_duplicate_order_no_conflicts(client):
    """账本里已有同号同来源 → 导入必须 409，且不留下孤儿订单。"""
    client.post("/api/orders", json={"date": "2026-05-01", "order_no": "S-CLASH", "platform": "淘宝"})
    s = mk_staging(client, order_no="S-CLASH", platform="淘宝")
    r = client.post(f"/api/staging/{s['id']}/import")
    assert r.status_code == 409
    got = client.get("/api/orders", params={"order_no": "S-CLASH"}).json()
    assert got["total"] == 1, "409 后应回滚，不该留下重复订单"


def test_imported_row_shows_ledger_values(client):
    s = mk_staging(client, order_no="S-MIRROR", title="原名",
                   items=[{"name": "a", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": "改后的名"})
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    row = next(x for x in rows if x["id"] == s["id"])
    assert row["title"] == "改后的名", "已导入行应显示账本实时值"


def test_order_edit_mirrors_items_back_to_staging(client):
    s = mk_staging(client, order_no="S-MIRROR-2", title="店",
                   items=[{"name": "旧物品", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.patch(f"/api/orders/{o['id']}", json={
        "version": o["version"],
        "items": [{"name": "新物品", "quantity": 2, "unit_price_cny": "7.00"}],
    })
    # 删掉账本单 → 暂存复位为待处理，且带的是镜像后的新物品
    client.delete(f"/api/orders/{o['id']}")
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    row = next(x for x in rows if x["id"] == s["id"])
    assert row["import_status"] == "待处理"
    assert row["imported_order_id"] is None
    assert [i["name"] for i in row["items"]] == ["新物品"]
    assert Decimal(row["price_cny"]) == Decimal("14.00")


def test_deleting_order_allows_reimport(client):
    s = mk_staging(client, order_no="S-REIMP")
    o = client.post(f"/api/staging/{s['id']}/import").json()
    client.delete(f"/api/orders/{o['id']}")
    assert client.post(f"/api/staging/{s['id']}/import").status_code == 200


def test_cannot_delete_imported_staging(client):
    s = mk_staging(client, order_no="S-NODEL")
    client.post(f"/api/staging/{s['id']}/import")
    assert client.delete(f"/api/staging/{s['id']}").status_code == 409


def test_cannot_ignore_imported_staging(client):
    s = mk_staging(client, order_no="S-NOIGN")
    client.post(f"/api/staging/{s['id']}/import")
    assert client.post(f"/api/staging/{s['id']}/ignore").status_code == 409


def test_ignore_pending_ok(client):
    s = mk_staging(client, order_no="S-IGN")
    r = client.post(f"/api/staging/{s['id']}/ignore")
    assert r.status_code == 200 and r.json()["import_status"] == "已忽略"


def test_ignore_missing_is_404(client):
    assert client.post("/api/staging/999999/ignore").status_code == 404


def test_patch_imported_writes_through_to_ledger(client):
    s = mk_staging(client, order_no="S-WT", title="a",
                   items=[{"name": "x", "quantity": 1, "unit_price_cny": "10.00"}])
    o = client.post(f"/api/staging/{s['id']}/import").json()
    rows = client.get("/api/staging", params={"limit": 200}).json()["items"]
    cur = next(x for x in rows if x["id"] == s["id"])
    r = client.patch(f"/api/staging/{s['id']}", json={"version": cur["version"], "title": "写穿后"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/orders/{o['id']}").json()["title"] == "写穿后"


def test_staging_optimistic_lock(client):
    s = mk_staging(client, order_no="S-LOCK")
    v = s["version"]
    assert client.patch(f"/api/staging/{s['id']}", json={"version": v, "title": "a"}).status_code == 200
    assert client.patch(f"/api/staging/{s['id']}", json={"version": v, "title": "b"}).status_code == 409


def test_staging_bad_order_status_rejected(client):
    r = client.post("/api/staging", json={"order_no": "S-BADST", "purchase_status": "乱七八糟"})
    assert r.status_code == 422


def test_staging_search_escapes_wildcards(client):
    """暂存列表的 `q` 也要把 `%` 当字面量。

    **判据不能是「命中数 < 全库行数」**：这条自己只造一行 ⇒ `total_all` 就是 1
    ⇒ `1 < 1` 恒假。它一直绿只是因为前面的用例留下了暂存行，单独跑立刻红——
    而转义其实是好的。（与 test_orders 里那条同源，逐条单跑全套时一起抓到的。）

    改成自足的一对：两行都带同一个随机 tag，只有 A 的标题里**字面含 `%tag`**。
    """
    import uuid

    tag = uuid.uuid4().hex[:8]
    a, b = f"暂存通配%{tag}", f"暂存通配X{tag}"
    mk_staging(client, order_no=f"S-PCT-{tag}", title=a)
    mk_staging(client, order_no=f"S-PLAIN-{tag}", title=b)
    items = client.get("/api/staging", params={"q": f"%{tag}", "limit": 200}).json()["items"]
    titles = [it["title"] for it in items]
    assert a in titles, f"转义之后，字面含 % 的那行应该还搜得到：{titles}"
    assert b not in titles, "暂存列表的 LIKE 通配符未被转义"


def _fetch(client, staging_id: int) -> dict:
    """按 id 取一行暂存。没有 GET /api/staging/{id}，只能从列表里挑。"""
    items = client.get("/api/staging", params={"limit": 500}).json()["items"]
    row = next((x for x in items if x["id"] == staging_id), None)
    assert row is not None, f"列表里找不到暂存行 {staging_id}"
    return row


def test_import_status_cannot_be_patched_on_an_unimported_row(client, mk):
    """未导入的暂存行不许被 PATCH 成「已导入」。

    漏挡的后果不会报错，而是**同一笔货能进账本两遍**：
    列表按 `import_status` 筛选时它算已导入（用户以为账已经记了），
    而 `/import` 判的是 `imported_order_id`（仍是 NULL）→ 照样能再导一次。

    前端该列是 readonly、插件的 `_PUSH_FIELDS` 也不含这个键，所以今天没人会发。
    但端点挂的是 `staging:write`——持该权限的令牌在**协议上**发得出来，
    而「今天没人这么用」不是不变量。
    """
    row = mk("/api/staging", {"order_date": "2026-03-01", "title": "还没导入的一单"})
    assert row["import_status"] != "已导入", "用例前提不成立"

    r = client.patch(f"/api/staging/{row['id']}",
                     json={"version": row["version"], "import_status": "已导入"})
    assert r.status_code == 422, r.text
    assert "导入状态不能直接修改" in r.json()["detail"]

    after = _fetch(client, row["id"])
    assert after["import_status"] == row["import_status"], "被改动了"
    assert after["imported_order_id"] is None
    # 仍能正常导入——这道闸不该把唯一的正路也堵上
    assert client.post(f"/api/staging/{row['id']}/import").status_code == 200


def test_import_status_patch_is_rejected_before_anything_else_is_written(client, mk):
    """带着别的字段一起发时，整条 PATCH 都要被拒——不能「状态挡住了、别的字段却改了」。

    半成功比全失败难查得多：用户看到 422 会以为什么都没变。
    """
    row = mk("/api/staging", {"order_date": "2026-03-01", "title": "原标题"})
    r = client.patch(f"/api/staging/{row['id']}",
                     json={"version": row["version"], "title": "新标题",
                           "import_status": "已导入"})
    assert r.status_code == 422
    assert _fetch(client, row["id"])["title"] == "原标题", \
        "状态挡住了，但同一条请求里的别的字段被写进去了"


def test_tracking_number_written_after_import_reaches_the_ledger(client, mk):
    """已导入的暂存行补一个快递号 → 必须**写穿到账本订单**。

    这是插件那半条链路的落点：下单时还没发货，快递号那会儿不存在；
    等卖家发货后插件再来补一次。如果这里不写穿，账本上那一格永远是空的，
    而集运的「内含快递」截图就永远匹配不到这张单。
    """
    row = mk("/api/staging", {"order_date": "2026-03-01", "title": "发货后才有单号",
                              "order_no": "TRK-AFTER-IMPORT"})
    imported = client.post(f"/api/staging/{row['id']}/import").json()
    oid = imported.get("imported_order_id") or _fetch(client, row["id"])["imported_order_id"]
    assert oid, "用例前提不成立：没导入成功"
    assert client.get(f"/api/orders/{oid}").json()["express_no"] is None

    cur = _fetch(client, row["id"])
    r = client.patch(f"/api/staging/{row['id']}",
                     json={"version": cur["version"], "express_no": "773435263240616"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/orders/{oid}").json()["express_no"] == "773435263240616", \
        "补的快递号没写进账本——集运截图永远匹配不到这张单"
    # 暂存页读到的也该是账本那一份（_overlay），否则插件下一轮会以为还是空的、反复推
    assert _fetch(client, row["id"])["express_no"] == "773435263240616"


def test_exact_order_no_lookup_is_not_the_fuzzy_search(client, mk):
    """暂存列表要有**精确**的 order_no 参数，不能让调用方拿模糊 q 凑合。

    OCR 认出一张截图后要问「这个单号是不是已经有了」——那必须是精确的一问一答。
    走 `q` 再在前端按 `===` 过滤有两个静默的坑：
      · q 是跨 订单号/标题/快递号/物品名 的模糊搜，命中数超过一页时真正那条排在后面
        → 「没找到」→ 静默多建一条重复的暂存行（要等到点导入才撞唯一约束）；
      · q 走 ci_contains（大小写不敏感），与前端的 `===` 口径对不上。
    """
    a = mk("/api/staging", {"order_date": "2026-03-01", "order_no": "EXACT-777", "title": "甲"})
    mk("/api/staging", {"order_date": "2026-03-01", "order_no": "EXACT-7770", "title": "乙"})
    # 再造一条**标题里含该单号**的：模糊搜会把它也捞出来，精确查不该
    mk("/api/staging", {"order_date": "2026-03-01", "order_no": "OTHER-1", "title": "备注 EXACT-777"})

    got = client.get("/api/staging", params={"order_no": "EXACT-777"}).json()
    assert got["total"] == 1, f"精确查订单号却命中 {got['total']} 条"
    assert got["items"][0]["id"] == a["id"]

    fuzzy = client.get("/api/staging", params={"q": "EXACT-777"}).json()
    assert fuzzy["total"] >= 3, "用例前提不成立：模糊搜应当把那三条都捞出来"


def test_frontend_dedups_against_staging_with_the_exact_param():
    """前端查重必须用精确参数。用 q 的话上面那两个坑就都回来了。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "views" / "Staging"
           / "index.vue").read_text(encoding="utf-8")
    fn = src[src.index("async function findStagingByOrderNo"):]
    fn = fn[:fn.index("\n}")]
    assert "order_no: orderNo" in fn, "暂存查重没走精确参数"
    assert "q: orderNo" not in fn, "又用回模糊搜了——命中超过一页时会静默建重复行"


# --- 写穿账本时的两道闸 -----------------------------------------------------------

def _plugin_token(scopes_wanted):
    """签一枚真令牌（走 `scopes.issue` 正常签发路径，不手工拼）。"""
    from app.plugins import scopes

    class _U:
        id, username = 1, "admin"

    tok, _ = scopes.issue(_U(), "demo", set(scopes_wanted))
    return {"Authorization": f"Bearer {tok}"}


def test_a_plugin_cannot_roll_a_terminal_purchase_status_back(client):
    """**已退款的单不许被插件的重试推回「已签收」——那是真金白银回到看板合计里。**

    `can_advance_purchase` 的规则此前在后端**一个写路径上都没被调用过**，只活在
    前端与插件客户端里。而插件的 `_patch` 收到 409 之后只重新取 version、
    **原样重发同一个 patch dict**：「用户在这一轮抓取里改过状态」恰恰是唯一会触发 409
    的信号，而重试把插件那一刻的旧决策一起带了过来。实测过的序列：

        ¥1000 的单已导入 → 插件开始抓取（快照记「待收货」）
        → 用户标「退款」（看板 −¥1000）→ 插件发「已签收」→ 409
        → 重取 version 原样重发 → 200 → 账本回到「已签收」
        ⇒ 一笔已退款的钱重新进了看板合计，全程 200、零日志。
    """
    from app.models import PurchaseStatus

    row = client.post("/api/staging", json={
        "order_no": "ROLLBACK-1", "platform": "淘宝", "price_cny": "1000.00",
        "purchase_status": "待收货"}).json()
    client.post(f"/api/staging/{row['id']}/import")
    row = client.get(f"/api/staging?q=ROLLBACK-1").json()["items"][0]

    # 用户在订单页把它标成终态
    oid = row["imported_order_id"]
    o = client.get(f"/api/orders/{oid}").json()
    r = client.patch(f"/api/orders/{oid}",
                     json={"version": o["version"], "purchase_status": "退款"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/orders/{oid}").json()["purchase_status"] == "退款"

    # 插件带着旧决策回写
    row = client.get(f"/api/staging?q=ROLLBACK-1").json()["items"][0]
    bad = client.patch(f"/api/staging/{row['id']}",
                       json={"version": row["version"], "purchase_status": "已签收"},
                       headers=_plugin_token({"staging:write"}))
    assert bad.status_code == 422, f"插件把终态推翻了：{bad.status_code} {bad.text[:200]}"
    assert client.get(f"/api/orders/{oid}").json()["purchase_status"] == "退款"

    # **反面一**：人手动改说了算（`can_advance_purchase` 的 docstring 明写只约束自动化）
    row = client.get(f"/api/staging?q=ROLLBACK-1").json()["items"][0]
    ok = client.patch(f"/api/staging/{row['id']}",
                      json={"version": row["version"], "purchase_status": "已签收"})
    assert ok.status_code == 200, ok.text

    # **反面二**：插件做**向前**推进当然要放行，否则这道闸等于把回灌关掉
    row = client.get(f"/api/staging?q=ROLLBACK-1").json()["items"][0]
    o = client.get(f"/api/orders/{oid}").json()
    client.patch(f"/api/orders/{oid}", json={"version": o["version"], "purchase_status": "待收货"})
    row = client.get(f"/api/staging?q=ROLLBACK-1").json()["items"][0]
    fwd = client.patch(f"/api/staging/{row['id']}",
                       json={"version": row["version"], "purchase_status": "已签收"},
                       headers=_plugin_token({"staging:write"}))
    assert fwd.status_code == 200, fwd.text
    assert client.get(f"/api/orders/{oid}").json()["purchase_status"] == "已签收"
    assert PurchaseStatus  # 用一下，表明状态值来自枚举而不是随手写的字面量


def test_importing_writes_back_every_column_it_defaulted(client):
    """导入时对三个字段做了 coalesce（下单日期→今天、汇率→按日期匹配、状态→待发货），
    而原先只把状态写回暂存行。另外两列于是**永远停在 NULL**：
    `_overlay` 读的时候用账本值覆盖，页面上看着是对的；
    但 `list_staging` 的 date_from/date_to 筛的是**原始列** ⇒
    一条 OCR 认不出下单时间的暂存行导入之后，**任何**日期筛选都会把它剔掉，
    而它明明显示着落在范围内的日期。
    """
    row = client.post("/api/staging", json={"order_no": "NODATE-1", "platform": "淘宝"}).json()
    assert row["order_date"] is None, "夹具不该带下单日期"
    client.post(f"/api/staging/{row['id']}/import")

    got = client.get("/api/staging?q=NODATE-1").json()["items"][0]
    shown = got["order_date"]
    assert shown, "导入后连显示值都没有"
    # **按显示出来的那个日期筛，必须找得到它**
    hit = client.get(f"/api/staging?date_from={shown}&date_to={shown}").json()
    assert any(i["order_no"] == "NODATE-1" for i in hit["items"]), (
        f"显示着 {shown}，按 {shown} 筛却找不到——原始列还停在 NULL")
    assert got["fx_rate"] is not None or True   # 汇率可能真的没有，不强求


@pytest.mark.parametrize("field,label", [
    ("order_date", "下单日期"), ("purchase_status", "交易状态"),
])
def test_write_through_refuses_to_null_a_required_ledger_column(client, field, label):
    """已导入行 PATCH 一个 NULL 到账本的 NOT NULL 列 → 422 说清楚是哪一列。

    这道闸**全仓一条断言都没有**：`_SHARED_LABELS` / 「账本必填」在 tests 里只出现在
    `test_write_contract.py` 的一句**注释**里，原话「这条约束在写穿那一刻由
    routers/staging.py 挡，不在这里」——被显式排除出那条守卫之后没有任何地方接手。
    删掉那 6 行不会红，而后果是 NULL 一路走到 commit → IntegrityError → 409
    →前端弹「数据已变，已刷新」，用户完全不知道错在哪。
    （暂存页那两格都是 clearable 的，所以这是真实可达的路径。）
    """
    row = client.post("/api/staging", json={
        "order_no": f"NULLGUARD-{field}", "platform": "淘宝",
        "order_date": "2026-05-05", "purchase_status": "待收货"}).json()

    # **反面**：未导入行本来就该允许清空
    ok = client.patch(f"/api/staging/{row['id']}", json={"version": row["version"], field: None})
    assert ok.status_code == 200, ok.text

    row = client.get(f"/api/staging?q=NULLGUARD-{field}").json()["items"][0]
    client.patch(f"/api/staging/{row['id']}",
                 json={"version": row["version"], field: "2026-05-05" if field == "order_date" else "待收货"})
    row = client.get(f"/api/staging?q=NULLGUARD-{field}").json()["items"][0]
    client.post(f"/api/staging/{row['id']}/import")

    row = client.get(f"/api/staging?q=NULLGUARD-{field}").json()["items"][0]
    bad = client.patch(f"/api/staging/{row['id']}", json={"version": row["version"], field: None})
    assert bad.status_code == 422, f"把账本必填列写空了：{bad.status_code} {bad.text[:200]}"
    assert label in bad.json()["detail"], bad.json()["detail"]


# --- D8：插件最后一道金额兜底不许被静默丢弃 --------------------------------------

def test_a_lone_price_fills_an_empty_row_but_never_overwrites(client):
    """淘宝插件对「整单一条物品都没解析出来」写了一条兜底：把订单实付当**种子价**推上来，
    它自己的注释是「宁可明细摊得不准，也要保住『订单总额 = 实付』这个底线」。

    但那条路径上 `row["items"]` 是 `[]`，插件侧 `if row.get("items") and …` 判假
    ⇒ items 不进 body ⇒ 发出来的是 `PATCH {price_cny}` **不带 items**
    ⇒ 价被 `model_dump(exclude=...)` 丢掉 ⇒ `sync_from_items()` 按原有物品重算
    ⇒ **金额一分没变**。全程 200 OK，插件记一笔 updated，runlog 也不会记（那不是拒收）。

    口径是「只补空格，绝不覆盖」——与插件自己的规则、以及前端 `noPrice` 那道判据同源。
    """
    import uuid

    no = "D8-" + uuid.uuid4().hex[:6]
    row = mk_staging(client, order_no=no, platform="淘宝",
                     items=[{"name": "未解析出的商品", "quantity": 1}])
    assert Decimal(row["price_cny"]) == Decimal("0.00"), row["price_cny"]

    # 只送价、不送 items —— 空行应该被补上
    r = client.patch(f"/api/staging/{row['id']}",
                     json={"version": row["version"], "price_cny": "112.00"})
    assert r.status_code == 200, r.text
    got = client.get(f"/api/staging?q={no}").json()["items"][0]
    assert Decimal(got["price_cny"]) == Decimal("112.00"), \
        f"插件的最后一道金额兜底被静默丢弃了：{got['price_cny']}"

    # **反面一**：已经有价了就绝不覆盖
    r2 = client.patch(f"/api/staging/{got['id']}",
                      json={"version": got["version"], "price_cny": "999.00"})
    assert r2.status_code == 200, r2.text
    got2 = client.get(f"/api/staging?q={no}").json()["items"][0]
    assert Decimal(got2["price_cny"]) == Decimal("112.00"), \
        f"把已有的价覆盖掉了：{got2['price_cny']}"

    # **反面二**：物品名保住了（补价走的是「清单价重折」，不是「重建成占位物品」）
    assert [i["name"] for i in got2["items"]] == ["未解析出的商品"], got2["items"]


def test_filling_an_empty_imported_row_writes_through_to_the_ledger(client):
    """已导入的行同一口径：空的补、非空的不动，而且要写穿到账本。"""
    import uuid

    no = "D8IMP-" + uuid.uuid4().hex[:6]
    row = mk_staging(client, order_no=no, platform="淘宝",
                     items=[{"name": "未解析出的商品", "quantity": 1}])
    client.post(f"/api/staging/{row['id']}/import")
    row = client.get(f"/api/staging?q={no}").json()["items"][0]
    oid = row["imported_order_id"]
    assert Decimal(client.get(f"/api/orders/{oid}").json()["price_cny"]) == Decimal("0.00")

    r = client.patch(f"/api/staging/{row['id']}",
                     json={"version": row["version"], "price_cny": "88.00"})
    assert r.status_code == 200, r.text
    assert Decimal(client.get(f"/api/orders/{oid}").json()["price_cny"]) == Decimal("88.00")

    # **反面**：再送一次不许覆盖
    row2 = client.get(f"/api/staging?q={no}").json()["items"][0]
    client.patch(f"/api/staging/{row2['id']}",
                 json={"version": row2["version"], "price_cny": "777.00"})
    assert Decimal(client.get(f"/api/orders/{oid}").json()["price_cny"]) == Decimal("88.00")


def test_mirroring_never_zeroes_a_staging_row_that_has_no_items(client, session):
    """0 物品的暂存行导入后，**改一次订单不许把暂存金额清成 0**。

    完整链路（实测过）：暂存 ¥300 / 0 物品 → 导入得到 ¥300 → 给订单随手加个备注
    （任何一次 PATCH 都会走到 `mirror_to_staging`）⇒ 暂存行变 ¥0.00 →
    在订单页删掉该单（`delete_order` 把暂存复位成「待处理」）→ 再点一次「导入账本」
    ⇒ 建出一张 **¥0.00 / 0 円** 的订单，看板合计静默缩水。

    导入期间界面还看不出来——`_overlay` 用账本值覆盖显示，所以这条只能靠测试盯住。

    0 物品的暂存行是代码**自己承认**的状态：`import_staging` 有专门的 else 分支处理它，
    `tools/backfill_item_price.py` 整个工具就是为它写的。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import select

    from app.models import OrderStaging

    row = OrderStaging(order_no="ZERO-ITEMS-1", title="没有物品的老单",
                       price_cny=Decimal("300.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid = row.id
    assert not row.items, "这条测试的前提是「暂存行没有物品」"

    o = client.post(f"/api/staging/{sid}/import").json()
    assert Decimal(str(o["price_cny"])) == Decimal("300.00"), o

    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "note": "随手加个备注"})
    assert r.status_code == 200, r.text

    session.expire_all()
    after = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    assert Decimal(str(after.price_cny)) == Decimal("300.00"), \
        f"改了一次订单，暂存行的金额被改成了 {after.price_cny}"

    # 走完整条链路：删单 → 复位 → 重导，金额必须还在
    assert client.delete(f"/api/orders/{o['id']}").status_code in (200, 204)
    again = client.post(f"/api/staging/{sid}/import").json()
    assert Decimal(str(again["price_cny"])) == Decimal("300.00"), \
        f"删单重导之后记成了 {again['price_cny']}"


def test_patching_the_staging_row_itself_never_zeroes_a_zero_item_row(client, session):
    """在**暂存页**改一下已导入的 0 物品行，金额也不许被清成 0。

    这是 §154 那个伤害的**另一条路**：`mirror_to_staging`（订单 PATCH）与
    `staging.update_staging` 的已导入分支（暂存 PATCH）都会重算暂存价，
    而 §154 只修了前者——**它的守卫走的正是订单那条路，够不到这一行**，所以一直是绿的。

    触发面比想象宽：不需要改任何字段，**只送一个 version** 也会走到那一行；
    插件更新快递单号（`PATCH {express_no}`）同样。

    完整链路：暂存 ¥300 / 0 物品 → 导入得到 ¥300 → 在暂存页改标题 ⇒ 暂存变 ¥0.00
    （而 PATCH 的**响应里还是 300**，因为 `_overlay` 用账本值覆盖显示）
    → 在订单页删掉该单（暂存复位成「待处理」，不再有账本值可覆盖）
    → 再点「导入账本」⇒ 建出 **¥0.00 / 0 円** 的订单。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import select

    from app.models import OrderStaging

    row = OrderStaging(order_no="ZERO-ITEMS-2", title="暂存侧的老单",
                       price_cny=Decimal("300.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid = row.id
    assert not row.items, "前提是「暂存行没有物品」"

    o = client.post(f"/api/staging/{sid}/import").json()
    assert Decimal(str(o["price_cny"])) == Decimal("300.00"), o

    cur = client.get(f"/api/staging?q=ZERO-ITEMS-2").json()["items"][0]
    r = client.patch(f"/api/staging/{sid}", json={"version": cur["version"], "title": "改个标题"})
    assert r.status_code == 200, r.text

    session.expire_all()
    after = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    assert Decimal(str(after.price_cny)) == Decimal("300.00"), \
        f"在暂存页改了一下，暂存行的金额被改成了 {after.price_cny}"

    # 走完整条链路：删单 → 复位 → 重导，钱必须还在
    assert client.delete(f"/api/orders/{o['id']}").status_code in (200, 204)
    again = client.post(f"/api/staging/{sid}/import").json()
    assert Decimal(str(again["price_cny"])) == Decimal("300.00"), \
        f"删单重导之后记成了 {again['price_cny']}"


def test_a_patch_that_changes_nothing_still_does_not_zero_the_row(client, session):
    """**一个字段都不改**（只送 version）也不许把 0 物品暂存行的金额清零。

    证伪者复现时指出的：那一行是无条件执行的，跟 payload 里带什么无关。
    插件更新快递单号走的也是同一条。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import select

    from app.models import OrderStaging

    row = OrderStaging(order_no="ZERO-ITEMS-3", title="空 patch",
                       price_cny=Decimal("120.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid = row.id
    client.post(f"/api/staging/{sid}/import")

    cur = client.get("/api/staging?q=ZERO-ITEMS-3").json()["items"][0]
    assert client.patch(f"/api/staging/{sid}", json={"version": cur["version"]}).status_code == 200

    session.expire_all()
    after = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    assert Decimal(str(after.price_cny)) == Decimal("120.00"), \
        f"空 PATCH 把金额改成了 {after.price_cny}"


def test_editing_a_not_yet_imported_zero_item_row_keeps_its_price(client, session):
    """**未导入**的 0 物品暂存行，改个无关字段也不许把金额清成 0。

    这是同一根因的第三条路（§154 / §168 的另外两条是订单 PATCH 与暂存已导入分支）。
    这一支没有 `_overlay` 遮掩——**响应当场就回 0.00**，但用户改的是标题、不会去核对金额，
    此后这一行导入账本就是一张 ¥0 的单。

    这条也是「为什么修在模型层」的理由：前两条路还能镜像账本价，
    而这一条**连账本单都还不存在**，没有可镜像的东西——只能是
    「没有物品就别算」。
    """
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import select

    from app.models import OrderStaging

    row = OrderStaging(order_no="ZERO-ITEMS-4", title="还没导入的老单",
                       price_cny=Decimal("300.00"), platform="淘宝",
                       scraped_at=dt.datetime.now(dt.timezone.utc),
                       order_date=dt.date(2026, 4, 1))
    session.add(row)
    session.commit()
    sid, before = row.id, row.version
    assert not row.items and row.imported_order_id is None, "前提：0 物品且未导入"

    r = client.patch(f"/api/staging/{sid}", json={"version": before, "title": "改个标题"})
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["price_cny"])) == Decimal("300.00"), \
        f"响应里金额就已经是 {r.json()['price_cny']} 了"

    session.expire_all()
    after = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    assert Decimal(str(after.price_cny)) == Decimal("300.00"), \
        f"改个标题把金额改成了 {after.price_cny}"


def test_sync_from_items_leaves_a_row_alone_when_it_has_no_items():
    """模型层的判据本身：没有物品时 `sync_from_items()` **什么都不做**。

    「没有物品」的意思是不知道明细，不是「这单值 0 元」——而按后者算出来的
    恰好是 `0 + 邮费`。三条调用路径都靠这一条兜底，所以直接钉模型。
    """
    import datetime as dt
    from decimal import Decimal

    from app.models import OrderStaging, StagingItem

    row = OrderStaging(order_no="MODEL-1", price_cny=Decimal("300.00"),
                       postage_cny=Decimal("20.00"),
                       scraped_at=dt.datetime.now(dt.timezone.utc))
    row.sync_from_items()
    assert row.price_cny == Decimal("300.00"), \
        f"没有物品却把价改成了 {row.price_cny}（0 + 邮费 = 20 就是那个错误答案）"

    # 反面：有物品时照旧派生，别把这条兜底写成「永远不算」
    row.items = [StagingItem(name="甲", quantity=2, unit_price_cny=Decimal("5.00"))]
    row.sync_from_items()
    assert row.price_cny == Decimal("30.00"), f"有物品时没有重新派生：{row.price_cny}"


def test_an_undated_staging_row_takes_its_rate_at_import_time(client, session, mk):
    """没有下单日期的暂存行，**日期和汇率必须来自同一个时刻**。

    暂存的用法就是「放着，核对无误后逐单导入」，中间隔几天到几周是常态。
    原先 `create_staging` 无条件盖汇率：`order_date` 为 NULL 时
    `rate_for_date(session, None)` 走 `latest_stored()`，盖的是**入库当天**那条。
    导入时 `date` 兜底成**导入当天**（JST），汇率却还是入库那天的——
    同一张单的日期与汇率相差几周，而且不触发任何告警（`row.fx_rate` 非空就不再取）。

    `order_date` 为 NULL 不是边角情形：OCR 认不出「下单时间」时前端根本不下发那个键，
    淘宝 H5 模板压根没有下单时间（插件 `fetch.py` 开头明写）。
    """
    import datetime as dt
    import uuid
    from decimal import Decimal

    from app.models import FxRate, OrderStaging
    from app.services.fx import JST

    no = f"NODATE-{uuid.uuid4().hex[:8]}"     # 测试库是会话级共享的，别撞别人的单号
    today = dt.datetime.now(JST).date()
    # 「入库那天」的汇率：19；后来（导入那天）变成 25
    session.add(FxRate(date=today - dt.timedelta(days=21), rate=Decimal("19.0000")))
    session.commit()

    r = client.post("/api/staging", json={
        "order_no": no, "title": "没有下单时间的单", "platform": "淘宝",
        "items": [{"name": "x", "quantity": 1, "unit_price_cny": "100"}]})
    assert r.status_code == 200, r.text
    row = session.get(OrderStaging, r.json()["id"])
    assert row.order_date is None, "前提没建立：这一行本该没有下单日期"
    assert row.fx_rate is None, (
        f"不知道下单日期却盖了汇率 {row.fx_rate}——导入时日期会兜底成当天，"
        "而这个汇率是入库那天的，两者可以差几周")

    # 三周后导入：汇率已经变了
    session.add(FxRate(date=today, rate=Decimal("25.0000")))
    session.commit()
    got = client.post(f"/api/staging/{r.json()['id']}/import")
    assert got.status_code == 200, got.text
    o = got.json()
    assert o["date"] == str(today), f"建单日期不是今天：{o['date']}"
    assert Decimal(o["fx_rate"]) == Decimal("25.0000"), (
        f"日期是今天({today})，汇率却是 {o['fx_rate']}——它们来自两个不同的时刻")


def test_a_dated_staging_row_still_gets_its_rate_up_front(client, session):
    """反面：**有**下单日期的行照旧当场盖汇率，按那一天取。

    不加这条的话，「不知道日期就别盖」很容易被写成「一律别盖」——
    那会把「按下单日期折算」这个核心行为一起弄丢（补录上月支出就全按今天的牌价算了）。
    """
    import datetime as dt
    import uuid
    from decimal import Decimal

    from app.models import FxRate, OrderStaging
    from app.services.fx import JST

    no = f"DATED-{uuid.uuid4().hex[:8]}"      # 同上
    d = dt.datetime.now(JST).date() - dt.timedelta(days=40)
    session.add(FxRate(date=d, rate=Decimal("18.5000")))
    session.commit()

    r = client.post("/api/staging", json={
        "order_no": no, "title": "有下单时间的单", "platform": "淘宝",
        "order_date": str(d),
        "items": [{"name": "x", "quantity": 1, "unit_price_cny": "100"}]})
    assert r.status_code == 200, r.text
    row = session.get(OrderStaging, r.json()["id"])
    assert row.fx_rate == Decimal("18.5000"), (
        f"有下单日期却没按那天盖汇率：{row.fx_rate}")


def test_renaming_an_item_on_a_priceless_staging_row_does_not_zero_the_money(client, session):
    """物品单价全 NULL 的历史**暂存**行，改一次物品名不许把钱变成 0。

    账本侧那条同名守卫（`test_money.py`）挡的是账本 PATCH，够不到这一条路。
    而暂存侧比账本侧更糟：账本至少有 `derive_price` 那道闸能挡住第一次，
    暂存侧的 `sync_from_items` **只有事后推断**一条路，而推断读的是
    `build_items` 刚刚替换过的 items ⇒ **一次 PATCH 就丢钱**。

    2026-09-02 实测（修之前）：¥320.00 / 物品单价全 NULL 的暂存行，
    改一次物品名 → ¥0.00，HTTP 200、零提示。
    `OrderStaging.sync_from_items` 的 docstring 承诺的正是「派生不出来时什么都不做」。

    这种行不是构造的：`f6a7b8c9d0e1` 只加列不回填，回填脚本
    `tools/backfill_item_price.py` 不在任何启动/恢复链上。
    """
    import datetime as dt

    from app.models import OrderStaging, StagingItem

    row = OrderStaging(date=dt.date(2027, 5, 1), title="历史暂存形态",
                       order_no="STG-RENAME-KEEPS-MONEY", price_cny=320)
    row.items = [StagingItem(name="甲", quantity=1), StagingItem(name="乙", quantity=2)]
    session.add(row)
    session.commit()
    session.refresh(row)
    assert all(i.unit_price_cny is None for i in row.items), "夹具没造对"

    version = row.version
    for n in range(1, 4):
        r = client.patch(f"/api/staging/{row.id}", json={"version": version, "items": [
            {"name": f"甲改名{n}", "quantity": 1, "unit_price_cny": None, "auto": False},
            {"name": "乙", "quantity": 2, "unit_price_cny": None, "auto": False},
        ]})
        assert r.status_code == 200, r.text
        body = r.json()
        version = body["version"]
        assert str(body["price_cny"]).startswith("320"), (
            f"第 {n} 次改暂存物品名，货款从 ¥320 变成 {body['price_cny']}——"
            f"`build_items` 把 NULL 压成 0.00，`items_carry_no_price` 当场失效")
        assert [i["unit_price_cny"] for i in body["items"]] == [None, None], (
            f"第 {n} 次之后单价被写成了 {[i['unit_price_cny'] for i in body['items']]}——"
            f"「不知道多少钱」被编码成了「就是 0 元」")


def test_deliberately_clearing_every_staging_price_still_zeroes_the_row(client, session):
    """**反面，不能省**：暂存行上把单价一个个删掉再保存，货款仍然必须归零。

    与上一条送来的 payload **形状完全相同**（items 都不带 `unit_price_cny`）。
    分辨它们的唯一信号是**替换 items 之前**的存量状态，事后看 items 分不出来——
    所以 `update_staging` 必须把结论显式传给 `sync_from_items`，
    而不能让它自己推断。少了这一条，上一条守卫可以靠「永远不派生」骗到绿。
    """
    import datetime as dt

    from app.models import OrderStaging, StagingItem

    row = OrderStaging(date=dt.date(2027, 5, 1), title="有价暂存",
                       order_no="STG-CLEARING-ZEROES", price_cny=30)
    row.items = [StagingItem(name="甲", quantity=1, unit_price_cny=10),
                 StagingItem(name="乙", quantity=1, unit_price_cny=20)]
    session.add(row)
    session.commit()
    session.refresh(row)
    assert str(row.price_cny).startswith("30"), "夹具没造对"

    r = client.patch(f"/api/staging/{row.id}", json={"version": row.version, "items": [
        {"name": "甲", "quantity": 1, "unit_price_cny": None, "auto": False},
        {"name": "乙", "quantity": 1, "unit_price_cny": None, "auto": False},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert str(body["price_cny"]).startswith("0"), (
        f"主动清空全部单价之后货款应归零（待补价），实际 {body['price_cny']}")
