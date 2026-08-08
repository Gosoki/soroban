"""查询次数回归：列表接口必须与行数无关（不能 N+1）。

用 SQLAlchemy 的 before_cursor_execute 事件数 SQL 条数。断言用「上界」而非精确值——
只要不随行数线性增长即可，避免为无关的实现细节反复改测试。
"""
import contextlib

import pytest
from sqlalchemy import event

from app.database import get_engine


@contextlib.contextmanager
def count_queries():
    stats = {"n": 0, "sql": []}
    engine = get_engine()

    def _on(conn, cursor, statement, params, context, executemany):
        stats["n"] += 1
        stats["sql"].append(statement.split("\n")[0][:110])

    event.listen(engine, "before_cursor_execute", _on)
    try:
        yield stats
    finally:
        event.remove(engine, "before_cursor_execute", _on)


def _seed_shipments(client, n, tag):
    for i in range(n):
        s = client.post("/api/shipment", json={"date": "2026-12-01",
                                               "shipment_no": f"{tag}-{i}"}).json()
        for j in range(2):
            o = client.post("/api/orders", json={
                "date": "2026-12-01", "order_no": f"{tag}-{i}-{j}", "platform": "淘宝",
                "items": [{"name": f"物{j}", "quantity": 1, "unit_price_cny": "1"}]}).json()
            client.post(f"/api/shipment/{s['id']}/order/{o['id']}")


def test_shipment_list_is_not_n_plus_1(client):
    _seed_shipments(client, 2, "QN-A")
    with count_queries() as a:
        client.get("/api/shipment", params={"limit": 2})
    _seed_shipments(client, 6, "QN-B")
    with count_queries() as b:
        client.get("/api/shipment", params={"limit": 8})
    assert b["n"] <= a["n"] + 1, (
        f"集运列表查询数随行数增长（2 行 {a['n']} 条 → 8 行 {b['n']} 条）\n"
        + "\n".join(b["sql"])
    )


def test_staging_list_is_not_n_plus_1(client):
    for i in range(2):
        s = client.post("/api/staging", json={
            "order_no": f"QS-A-{i}",
            "items": [{"name": "x", "quantity": 1, "unit_price_cny": "1"}]}).json()
        client.post(f"/api/staging/{s['id']}/import")
    with count_queries() as a:
        client.get("/api/staging", params={"import_status": "已导入", "limit": 100})
    for i in range(6):
        s = client.post("/api/staging", json={
            "order_no": f"QS-B-{i}",
            "items": [{"name": "x", "quantity": 1, "unit_price_cny": "1"}]}).json()
        client.post(f"/api/staging/{s['id']}/import")
    with count_queries() as b:
        client.get("/api/staging", params={"import_status": "已导入", "limit": 100})
    assert b["n"] <= a["n"] + 1, (
        f"暂存列表查询数随行数增长（{a['n']} 条 → {b['n']} 条）\n" + "\n".join(b["sql"])
    )


def test_orders_list_is_not_n_plus_1(client):
    for i in range(8):
        client.post("/api/orders", json={
            "date": "2026-12-05", "order_no": f"QO-{i}", "platform": "淘宝",
            "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"},
                      {"name": "b", "quantity": 1, "unit_price_cny": "1"}]})
    with count_queries() as a:
        client.get("/api/orders", params={"limit": 1})
    with count_queries() as b:
        client.get("/api/orders", params={"limit": 100})
    # 判据是「**不随行数增长**」，不是「一条都不能多」：selectinload 每个关系最多多发
    # **一条**批量查询（items 一条、shipment_order 一条），且只在该页真有相关行时才发——
    # 所以 1 行页与 100 行页之间存在最多 2 条的常数差。真的 N+1 会是 ~100 条，不会混淆。
    assert b["n"] <= a["n"] + 2, (
        f"订单列表查询数随行数增长（1 行 {a['n']} 条 → 100 行 {b['n']} 条）\n"
        + "\n".join(b["sql"])
    )


def test_tags_list_scans_data_once(client):
    """_data_values 是几张表的 DISTINCT 扫描，一个请求里只该跑一轮。"""
    client.get("/api/tags/platform_account")     # 预热：先把该登记的都登记掉
    with count_queries() as s:
        client.get("/api/tags/platform_account")
    # platform_account 有 2 个数据来源表 → 一轮 = 2 条 DISTINCT；再加 1 条取标签
    distinct = [q for q in s["sql"] if "DISTINCT" in q.upper()]
    assert len(distinct) == 2, f"DISTINCT 扫描了 {len(distinct)} 次（应为 2）：\n" + "\n".join(distinct)


def test_shipment_brief_skips_children(client):
    """订单页/物品页拉这个接口只是为了填「选哪张集运单」的下拉，全仓无一处读 j.orders。
    不加 brief 时 200 张集运单会展开出全部子订单+物品（实测 1.1MB / 1073ms）。
    查询条数测试钉不住体积——所以这里直接断言形状。"""
    full = client.get("/api/shipment", params={"limit": 200}).json()["items"]
    brief = client.get("/api/shipment", params={"limit": 200, "brief": True}).json()["items"]
    assert len(full) == len(brief), "brief 不该改变返回的集运单条数"
    assert all(x["orders"] == [] for x in brief), "brief 应当完全不展开子订单"
    if full and any(x["orders"] for x in full):
        assert any(x["orders"] for x in full), "非 brief 仍应展开（集运页自己的面板要用）"


def test_scheduler_loop_runs_db_work_off_the_event_loop():
    """协程里做同步 DB I/O，MySQL 运行中掉线会阻塞整个事件循环——
    实测单次卡 384 秒（pymysql read_timeout 默认 None），期间 /api/health 都一起卡死。

    ⚠️ 这条**必须真跑一轮**，不能只 grep 源码里有没有 `run_in_threadpool`。
    上一版就是 grep 版：`run_in_threadpool` 被用了却从没导入，NameError 每轮被循环的
    兜底 `except` 吞成一行警告 —— 定时抓取实际上一直没跑，而测试一路全绿。
    字符串在源码里，名字却没绑定，grep 分辨不了这两件事。
    """
    import asyncio
    import threading

    from app.routers import plugins as mod

    ran_in = {}
    loop_thread = None

    def fake_run_due():
        ran_in["thread"] = threading.current_thread().ident

    async def one_round():
        nonlocal loop_thread
        loop_thread = threading.current_thread().ident
        # 只跑一轮：把 sleep 变成取消信号
        async def stop(_):
            raise asyncio.CancelledError
        orig_sleep, orig_run = mod.asyncio.sleep, mod._run_due_in_session
        mod._run_due_in_session = fake_run_due
        mod.asyncio.sleep = stop
        try:
            await mod.scheduler_loop(interval=0)
        except asyncio.CancelledError:
            pass
        finally:
            mod.asyncio.sleep, mod._run_due_in_session = orig_sleep, orig_run

    asyncio.run(one_round())
    assert "thread" in ran_in, "定时循环一轮下来根本没调到 _run_due_in_session（多半是抛异常被吞了）"
    assert ran_in["thread"] != loop_thread, (
        "同步 DB I/O 是在事件循环线程上跑的——MySQL 掉线会把整个服务卡死")


def test_mysql_engine_has_read_and_write_timeouts():
    """connect_timeout 只管 TCP 建连，**不管握手之后的读**。三个都要设，否则最坏是无界阻塞。"""
    import inspect

    from app import database

    src = inspect.getsource(database.build_engine)
    for k in ("connect_timeout", "read_timeout", "write_timeout"):
        assert k in src, f"MySQL engine 缺 {k}"


# --- BinStr 列的模糊搜索必须仍是大小写不敏感 ------------------------------------

def test_binstr_columns_use_ci_contains_for_search():
    """被 LIKE 搜索的 BinStr 列必须走 `ci_contains`，不能裸 `.contains()`。

    BinStr 让 MySQL 的 `=` 逐字节（唯一性 / 等值批改需要），副作用是同列上的 LIKE
    也跟着变大小写敏感，而 SQLite 的 LIKE 对 ASCII 本来就不敏感——同一份数据、
    同一个搜索词，两个后端返回不同结果。

    只能静态查：**本地跑在 SQLite 上，这个差异复现不出来**。
    """
    import ast
    from pathlib import Path

    from sqlmodel import SQLModel

    # 从 metadata 反查哪些列在 MySQL 上带 bin 排序规则，不手抄清单
    binstr = set()
    for t in SQLModel.metadata.tables.values():
        for c in t.columns:
            variant = getattr(c.type, "_variant_mapping", {}).get("mysql")
            if variant is not None and "bin" in str(getattr(variant, "collation", "") or ""):
                binstr.add(c.name)
    assert binstr, "没识别出任何 BinStr 列——探测方式可能已过期，请检查 dialect.BinStr"

    # 只认 .contains 是不够的：改用 .like(f"%{q}%") / .ilike / .startswith 一样会走列的
    # 排序规则，一样在 MySQL 上大小写敏感——而守卫会静默放行。这条守的是本地测不出来的
    # 双引擎发散，被等价写法绕过 = 缺陷重新裸奔。
    _LIKE_OPS = {"contains", "like", "ilike", "startswith", "endswith"}

    bad = []
    for path in sorted((Path(__file__).resolve().parents[1] / "app" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _LIKE_OPS or not isinstance(node.func.value, ast.Attribute):
                continue
            col = node.func.value.attr
            if col in binstr:
                bad.append(f"{path.name}:{node.lineno} {col}.{node.func.attr}(...)")
    assert not bad, ("这些 BinStr 列用了裸的 LIKE 类操作，MySQL 上会变成大小写敏感：\n  "
                     + "\n  ".join(bad) + "\n改用 db.dialect.ci_contains(col, q, session)。")


def test_fx_loop_runs_db_work_off_the_event_loop():
    """`fx_loop` 的 DB 活必须在别的线程上，不能占着事件循环。

    与 test_scheduler_loop_runs_db_work_off_the_event_loop 同一类问题、同一个修法：
    协程体里直接跑同步 Session/pymysql，在「MySQL 掉线但 TCP 还通」时会把整个事件循环
    冻住（scheduler_loop 实测单次 384 秒）。当时只修了 scheduler_loop，fx_loop 漏了。

    ⚠️ 同样**必须真跑一轮**，不能只 grep 源码里有没有 `run_in_threadpool`——
    上一次就吃过这个亏：名字出现在源码里、却根本没导入，NameError 被循环的兜底 except
    吞成一行警告，而测试一路全绿。
    """
    import asyncio
    import threading

    from app.services import fx as mod

    ran_in = {}
    loop_thread = None

    def fake_round() -> int:
        ran_in["thread"] = threading.current_thread().ident
        return 0

    async def one_round():
        nonlocal loop_thread
        loop_thread = threading.current_thread().ident

        async def stop(_):
            raise asyncio.CancelledError

        orig_sleep, orig_round = mod.asyncio.sleep, mod._refresh_round_blocking
        mod._refresh_round_blocking = fake_round
        mod.asyncio.sleep = stop
        try:
            await mod.fx_loop()
        except asyncio.CancelledError:
            pass
        finally:
            mod.asyncio.sleep, mod._refresh_round_blocking = orig_sleep, orig_round

    asyncio.run(one_round())
    assert ran_in.get("thread"), "fx_loop 这一轮根本没跑到 DB 活（可能被异常吞了）"
    assert ran_in["thread"] != loop_thread, \
        "fx_loop 的 DB 活跑在事件循环线程上：MySQL 掉线时会冻住整个服务"
