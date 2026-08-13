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




def test_dialect_is_detected_from_a_session_not_silently_defaulted(session):
    """路由层手里只有 Session，方言判定必须认它。

    这条测的是**行为**而不是「源码里有没有 ci_contains 这个词」：
    `_name()` 原先兜底走 `getattr(bind, "dialect", None)`，Session 没有这个属性，
    于是返回 Session 的 repr、`is_mysql()` 恒 False。错得没有任何声响——
    只是 MySQL 上 BinStr 列的模糊搜索悄悄变回大小写敏感。
    """
    from app.db.dialect import _name, is_mysql, is_sqlite

    assert _name(session) == "sqlite", f"从 Session 判方言失败，拿到的是 {_name(session)!r}"
    assert is_sqlite(session) and not is_mysql(session)


def test_unknown_bind_raises_instead_of_pretending_to_be_sqlite():
    """认不出来的 bind 必须报错，不许静默当成非 MySQL。

    静默兜底正是上一个 bug 的形状：`is_mysql()` 返回 False，一切照常运行，
    只有 MySQL 上的搜索结果不对——而那要等用户搜不到东西才会发现。
    """
    import pytest

    from app.db.dialect import is_mysql

    with pytest.raises(TypeError):
        is_mysql(object())


def test_engine_pool_is_explicit_for_file_backed_sqlite():
    """连接池必须**显式**配，别吃 SQLAlchemy 的默认 5+10=15。

    15 条连接 + anyio 的 40 个线程令牌 = 25 个线程堵在 `get_current_user` 里等池。
    实测并发 40 路 OCR 时响应从 7ms 涨到 6220ms（174 倍），30 秒后抛 TimeoutError。
    这条钉的是「有人把配置删掉就变红」，不是性能本身。
    """
    from app.database import _MAX_OVERFLOW, _POOL_SIZE, build_engine

    e = build_engine("sqlite:///./_pool_probe.db")
    try:
        assert e.pool.size() == _POOL_SIZE
        assert e.pool._max_overflow == _MAX_OVERFLOW
        assert _POOL_SIZE + _MAX_OVERFLOW >= 40, "池要对齐 anyio 的 40 个线程令牌"
    finally:
        e.dispose()
        import pathlib
        pathlib.Path("_pool_probe.db").unlink(missing_ok=True)


def test_in_memory_sqlite_still_builds():
    """内存库走 SingletonThreadPool，它不接受 max_overflow/pool_timeout ——
    池参数一刀切地传下去会让 `DATABASE_URL=sqlite://` 在 create_engine 就 TypeError。"""
    from app.database import build_engine

    for url in ("sqlite://", "sqlite:///:memory:"):
        build_engine(url).dispose()


# --- OCR 推理期间不许占着数据库连接 ---------------------------------------------

def _checkedout():
    from app.database import get_engine
    return get_engine().pool.checkedout()


def test_ocr_holds_no_db_connection_while_inferring(client, monkeypatch):
    """OCR 推理的那几秒里，这个请求**不许**占着一条数据库连接。

    为什么这是个真问题：推理是几秒到十几秒（首次还要加载模型），而鉴权依赖
    `get_current_user` 会先 `session.get(User, ...)`。那笔读开的事务如果一直不结束，
    这条连接在整个推理期间就是「idle in transaction」——在 MySQL 上还一路攥着
    REPEATABLE READ 快照与 MDL，让并发的 DDL/迁移一起等。

    **`_OCR_CONCURRENCY` 挡不住这件事**：它只压住「同时在解码/推理的」，
    排队等这个闸的请求照样各占一条连接。池是 20+20，几十路并发上传就能见底，
    而见底之后**整个应用**（不只是 OCR）都要等满 30 秒 pool_timeout 才抛错。

    量的是真实请求路径上的 `pool.checkedout()`，不是推理这条链路「应该」怎么样——
    这条曾经被推理成「rollback 只结束事务、不还连接」，而实测正好相反。
    """
    from app.services import ocr as ocr_mod

    during = []

    def slow_recognise(image_bytes, platform_hint=None):
        during.append(_checkedout())      # 正在「推理」的这一刻，池里被占了几条
        return {"order_no": "POOL-1", "raw_text": ""}

    monkeypatch.setattr(ocr_mod, "recognize_order", slow_recognise)
    r = client.post("/api/orders/ocr", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert r.status_code == 200, r.text
    assert during == [0], f"推理期间占着 {during} 条连接（应当是 0）"


def test_shipment_express_ocr_releases_its_connection_before_inferring(client, monkeypatch):
    """集运的「内含快递」OCR 同理——而它比另外两条更容易漏。

    这条路由在推理**之前**就得先查一次集运单是否存在，那笔读会开事务、占住连接。
    所以光靠鉴权那侧还回去不够，它必须自己再还一次。

    这么做不影响正确性：那次读只是 fail-fast（别为一张不存在的集运单白跑十几秒 OCR），
    真正说了算的是挂靠那条 UPDATE 自带的 EXISTS 守卫。删掉那句 `session.rollback()`
    这条测试就会红，而**功能测试一条都不会红**——这正是它存在的理由。
    """
    from app.services import ocr as ocr_mod

    sid = client.post("/api/shipment", json={"date": "2026-12-01",
                                             "shipment_no": "POOL-SHIP-1"}).json()["id"]
    during = []

    def slow_recognise(image_bytes):
        during.append(_checkedout())
        return {"express_nos": []}

    monkeypatch.setattr(ocr_mod, "recognize_shipment", slow_recognise)
    r = client.post(f"/api/shipment/{sid}/ocr-express",
                    files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert r.status_code == 200, r.text
    assert during == [0], f"推理期间占着 {during} 条连接（应当是 0）"


def test_wal_checkpoint_does_not_run_on_the_event_loop_thread():
    """周期性 WAL 截断必须在**线程池**里做，不能占着事件循环线程。

    `PRAGMA wal_checkpoint(TRUNCATE)` 撞上写锁时会一直等到 sqlite3 的 busy timeout
    （实测 5 秒），而它原先是在协程里裸调的——那 5 秒整个事件循环停摆：健康检查、
    前端轮询、静态资源全卡。最坏的是**一行日志都不会有**：checkpoint 撞锁返回 busy
    而不是抛异常，那圈 try/except 根本进不去，事后完全归因不到这里。

    现实触发路径：数据库页点「迁回本地 SQLite」，迁移在单事务里逐表 delete+insert、
    全程握着写锁，而 600 秒一轮的截断正好落进那段窗口。

    **比对线程 id，不 grep 源码**：`await run_in_threadpool(...)` 与
    `_wal_truncate()` 在源码里长得完全不同，但只有真跑一次才知道换没换线程。
    """
    import asyncio
    import threading

    from app import database as db

    seen = {}

    def spy():
        seen["thread"] = threading.get_ident()

    async def drive():
        seen["loop"] = threading.get_ident()
        orig, db._wal_truncate = db._wal_truncate, spy
        try:
            task = asyncio.create_task(db.wal_checkpoint_loop(interval=0))
            for _ in range(200):                  # 等它跑完至少一轮
                await asyncio.sleep(0.005)
                if "thread" in seen:
                    break
            task.cancel()
        finally:
            db._wal_truncate = orig

    asyncio.run(drive())
    assert "thread" in seen, "循环一轮都没跑起来，这条测试没验到东西"
    assert seen["thread"] != seen["loop"], "WAL 截断跑在事件循环线程上——整个服务会被它卡住"
