"""暂存 → 导入账本的全流程：写穿、镜像、原子门闸、删除一致性。"""
from decimal import Decimal


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
