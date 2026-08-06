"""只读屏障 + 迁移变更检测。

背景：`replace_data` 逐表读源库，而 SQLite 侧**没有读快照**（pysqlite 的 SELECT 跑在
autocommit），拷贝期间的写入会产生撕裂的拷贝。所以迁移全程必须只读。
而「迁移完 → 隔一段时间才点切换」这段时间里的写入会留在旧库、切换后静默消失，
故在切换前比对源库指纹，把静默丢失变成知情选择。
"""
import json
import threading
import time

import pytest

from app.maintenance import ReadOnlyBarrier, barrier
from app.services import db_migrate


@pytest.fixture(autouse=True)
def _clean_barrier():
    barrier.reset()
    yield
    barrier.reset()


# --- 屏障：HTTP 写路径 --------------------------------------------------------

def test_writes_blocked_while_held(client):
    with barrier.hold("数据库迁移中", drain=0.1):
        r = client.post("/api/orders", json={"date": "2029-01-01", "title": "挡住"})
        assert r.status_code == 503
        assert "迁移" in r.json()["detail"]
        assert r.headers.get("Retry-After")


def test_reads_still_work_while_held(client):
    with barrier.hold("数据库迁移中", drain=0.1):
        assert client.get("/api/orders").status_code == 200
        assert client.get("/api/dashboard").status_code == 200
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/orders", {"date": "2029-01-01"}),
    ("post", "/api/shipment", {"date": "2029-01-01"}),
    ("post", "/api/misc", {"date": "2029-01-01", "name": "x"}),
    ("post", "/api/staging", {"order_no": "BR-1"}),
    ("post", "/api/tags/recipient", {"value": "x"}),
    ("put", "/api/layout/orders", {"columns": []}),
])
def test_all_write_endpoints_blocked(client, method, path, body):
    """用中间件而不是逐端点挂依赖，就是为了「将来新增的写端点自动被覆盖」。"""
    with barrier.hold("数据库迁移中", drain=0.1):
        assert getattr(client, method)(path, json=body).status_code == 503


def test_delete_blocked(client):
    o = client.post("/api/orders", json={"date": "2029-01-01"}).json()
    with barrier.hold("数据库迁移中", drain=0.1):
        assert client.delete(f"/api/orders/{o['id']}").status_code == 503


def test_db_endpoints_not_blocked_by_own_barrier(client):
    """迁移/切换端点不能被自己挂的屏障拦住，否则一挂就再也解不开。"""
    with barrier.hold("数据库迁移中", drain=0.1):
        assert client.get("/api/db/status").status_code == 200
        assert client.post("/api/db/test", json={"backend": "sqlite"}).status_code == 200


def test_login_not_blocked(anon):
    """登录只读 user 表，屏障期间要能进来看状态。"""
    from tests.conftest import ADMIN_PASS, ADMIN_USER
    with barrier.hold("数据库迁移中", drain=0.1):
        r = anon.post("/api/auth/login", data={"username": ADMIN_USER, "password": ADMIN_PASS})
        assert r.status_code == 200


def test_writes_resume_after_release(client):
    with barrier.hold("数据库迁移中", drain=0.1):
        pass
    assert client.post("/api/orders", json={"date": "2029-01-01", "title": "放行"}).status_code == 200


def test_barrier_released_even_on_exception(client):
    """屏障泄漏 = 应用永久只读，是比迁移失败严重得多的后果。"""
    with pytest.raises(ValueError):
        with barrier.hold("数据库迁移中", drain=0.1):
            raise ValueError("拷贝炸了")
    assert barrier.blocked_reason() is None
    assert client.post("/api/orders", json={"date": "2029-01-01"}).status_code == 200


def test_hard_timeout_self_heals():
    """万一 finally 都没跑到（进程被 SIGKILL 前的残留状态、或有人手工置位），
    硬超时兜底，绝不让账本永久只读。"""
    b = ReadOnlyBarrier()
    b._reason = "卡住的迁移"                    # 模拟泄漏
    b._deadline = time.monotonic() - 1          # 已过期
    assert b.blocked_reason() is None
    assert b.begin_write() is None


def test_concurrent_hold_rejected():
    b = ReadOnlyBarrier()
    with b.hold("第一项", drain=0.01):
        with pytest.raises(RuntimeError, match="已有另一项维护操作"):
            with b.hold("第二项", drain=0.01):
                pass


def test_hold_waits_for_inflight_writes():
    """挂起屏障后要等在飞的写请求排空，否则它们仍会与拷贝重叠。"""
    b = ReadOnlyBarrier()
    assert b.begin_write() is None              # 模拟一个在飞的写
    released = threading.Event()

    def finish_later():
        time.sleep(0.2)
        b.end_write()
        released.set()

    threading.Thread(target=finish_later, daemon=True).start()
    t0 = time.monotonic()
    with b.hold("迁移", drain=2.0):
        waited = time.monotonic() - t0
    assert released.is_set(), "hold 没等在飞的写请求结束"
    assert waited >= 0.15


def test_begin_write_is_atomic_with_check():
    """「查屏障」与「计数」必须同一把锁：否则查完通过、屏障挂起、才计数 → 漏掉一个在飞写。"""
    b = ReadOnlyBarrier()
    with b.hold("迁移", drain=0.01):
        assert b.begin_write() == "迁移"
        assert b._inflight == 0, "被拒绝的请求不该被计入在飞数"


# --- 屏障：非 HTTP 写路径 ------------------------------------------------------

def test_fx_loop_checks_barrier():
    """fx_loop 直接用 Session 写 FxRate，绕过 HTTP 中间件，必须自己查屏障。"""
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "app" / "services" / "fx.py").read_text(encoding="utf-8")
    body = src.split("async def fx_loop")[1]
    assert "barrier.blocked_reason()" in body, "fx_loop 没查只读屏障"


def test_scheduler_loop_checks_barrier():
    """屏障期间起爬虫子进程 = 白开一次浏览器冲淘宝，而并发多开是风控红线。"""
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "app" / "routers" / "plugins.py").read_text(encoding="utf-8")
    body = src.split("async def scheduler_loop")[1]
    assert "barrier.blocked_reason()" in body, "scheduler_loop 没查只读屏障"


# --- 迁移指纹与变更检测 --------------------------------------------------------

def test_fingerprint_changes_on_insert(client, session):
    from app.database import get_engine
    before = db_migrate.source_fingerprint(get_engine())
    client.post("/api/orders", json={"date": "2029-02-01", "title": "指纹"})
    after = db_migrate.source_fingerprint(get_engine())
    assert before != after
    assert db_migrate.describe_fingerprint_diff(before, after) != []


def test_fingerprint_stable_without_writes(client):
    from app.database import get_engine
    a = db_migrate.source_fingerprint(get_engine())
    client.get("/api/orders")                    # 纯读不该改变指纹
    assert db_migrate.source_fingerprint(get_engine()) == a
    assert db_migrate.describe_fingerprint_diff(a, a) == []


def test_fingerprint_ignores_layout_noise(client):
    """拖一下列宽不该触发「源库有改动」的告警。"""
    from app.database import get_engine
    a = db_migrate.source_fingerprint(get_engine())
    client.put("/api/layout/orders", json={"columns": [{"key": "date", "width": 123}]})
    assert db_migrate.source_fingerprint(get_engine()) == a


def test_diff_describes_counts_and_edits():
    a = json.dumps({"orders": [5, "2028-01-01"], "shipmentorder": [2, "2028-01-01"]}, sort_keys=True)
    b = json.dumps({"orders": [8, "2028-01-02"], "shipmentorder": [2, "2028-01-05"]}, sort_keys=True)
    d = db_migrate.describe_fingerprint_diff(a, b)
    assert "商品订单 +3 条" in d          # 增删看行数
    assert "集运订单 有改动" in d          # 只改内容看时间戳


def test_diff_survives_corrupt_fingerprint():
    assert db_migrate.describe_fingerprint_diff("not json", "{}") == ["无法比对（指纹损坏）"]


def test_fingerprint_roundtrip_in_control_db():
    from app.database import control_engine
    from app.db import control
    control.save_migrate_fingerprint(control_engine(), "u@h:3306/db", '{"orders":[1,null]}')
    assert control.read_migrate_fingerprint(control_engine(), "u@h:3306/db") == '{"orders":[1,null]}'
    control.save_migrate_fingerprint(control_engine(), "u@h:3306/db", '{"orders":[2,null]}')
    assert control.read_migrate_fingerprint(control_engine(), "u@h:3306/db") == '{"orders":[2,null]}'
    assert control.read_migrate_fingerprint(control_engine(), "从没迁过") is None


def test_control_tables_excluded_from_legacy_detection():
    """加控制表时最容易踩的坑：run_migrations 的「pre-Alembic 旧库」判定若把控制表算成业务表，
    全新部署会被误判、stamp 到 baseline 而建不出业务表。排除集必须从 control_metadata 派生。"""
    from app.database import run_migrations
    import inspect
    src = inspect.getsource(run_migrations)
    assert "control.control_metadata.tables" in src, \
        "排除集又写成手抄清单了——加一张控制表就会瘫痪全新部署"
