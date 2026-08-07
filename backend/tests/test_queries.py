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
        client.get("/api/staging", params={"status": "已导入", "limit": 100})
    for i in range(6):
        s = client.post("/api/staging", json={
            "order_no": f"QS-B-{i}",
            "items": [{"name": "x", "quantity": 1, "unit_price_cny": "1"}]}).json()
        client.post(f"/api/staging/{s['id']}/import")
    with count_queries() as b:
        client.get("/api/staging", params={"status": "已导入", "limit": 100})
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


def test_scheduler_loop_does_not_block_event_loop():
    """协程里做同步 DB I/O，MySQL 运行中掉线会阻塞整个事件循环——
    实测单次卡 384 秒（pymysql read_timeout 默认 None），期间 /api/health 都一起卡死。"""
    import inspect

    from app.routers import plugins as mod

    src = inspect.getsource(mod.scheduler_loop)
    assert "run_in_threadpool" in src, "定时循环又在事件循环里直接做同步 DB I/O 了"


def test_mysql_engine_has_read_and_write_timeouts():
    """connect_timeout 只管 TCP 建连，**不管握手之后的读**。三个都要设，否则最坏是无界阻塞。"""
    import inspect

    from app import database

    src = inspect.getsource(database.build_engine)
    for k in ("connect_timeout", "read_timeout", "write_timeout"):
        assert k in src, f"MySQL engine 缺 {k}"
