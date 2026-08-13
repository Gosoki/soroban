"""只读屏障 + 迁移变更检测。

背景：`replace_data` 逐表读源库，而 SQLite 侧**没有读快照**（pysqlite 的 SELECT 跑在
autocommit），拷贝期间的写入会产生撕裂的拷贝。所以迁移全程必须只读。
而「迁移完 → 隔一段时间才点切换」这段时间里的写入会留在旧库、切换后静默消失，
故在切换前比对源库指纹，把静默丢失变成知情选择。
"""
import json
import threading
import time
from pathlib import Path

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



def test_scheduler_loop_checks_barrier():
    """屏障期间起爬虫子进程 = 白开一次浏览器冲淘宝，而并发多开是风控红线。"""
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "app" / "routers" / "plugins.py").read_text(encoding="utf-8")
    body = src.split("async def scheduler_loop")[1]
    assert "barrier.blocked_reason()" in body, "scheduler_loop 没查只读屏障"


def test_get_tags_does_not_write_while_held(client, session):
    """第四条写路径：GET /api/tags/{field} 会把数据里的新值自动登记成标签并 commit。
    中间件按「GET 是安全方法」放行，拦不到——屏障期间必须由 _sync 自己不落库。"""
    from sqlmodel import select

    from app.models import TagOption

    account = "屏障期才出现的账号"
    client.post("/api/orders", json={"date": "2029-03-01", "platform_account": account})

    def registered() -> bool:
        session.expire_all()
        rows = session.exec(select(TagOption).where(TagOption.field == "platform_account")).all()
        return any(r.value == account for r in rows)

    with barrier.hold("数据库迁移中", drain=0.1):
        r = client.get("/api/tags/platform_account")
        assert r.status_code == 200                                  # 读照常放行
        assert not registered(), "屏障期间 GET /api/tags 仍往源库写了 tagoption"

    client.get("/api/tags/platform_account")                         # 撤销后下一次 GET 自然补上
    assert registered(), "屏障撤销后应当恢复自动登记（只是推迟，不是丢弃）"


def test_no_get_endpoint_writes_without_checking_barrier():
    """通用护栏：HTTP 方法推断不出会不会写库。任何 GET 端点只要（传递地）能走到
    commit/flush，它的调用链上就必须有人查过屏障——否则迁移拷贝期间会被它写脏。"""
    import ast
    import collections
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    calls: dict[str, set[str]] = collections.defaultdict(set)
    writes: set[str] = set()
    guards: set[str] = set()
    gets: list[tuple[str, str]] = []

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                if name:
                    calls[fn.name].add(name)
                if isinstance(f, ast.Attribute):
                    if f.attr in ("commit", "flush"):
                        writes.add(fn.name)
                    if f.attr == "blocked_reason":
                        guards.add(fn.name)
            for d in fn.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "get"):
                    gets.append((fn.name, f"{path.name}:{fn.lineno}"))

    def reachable(start: str) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for callee in calls.get(stack.pop(), ()):
                if callee not in seen:
                    seen.add(callee)
                    stack.append(callee)
        return seen

    unguarded = [
        f"GET {name} ({loc}) 经 {sorted(reachable(name) & writes)} 写库，但链上无人查屏障"
        for name, loc in gets
        if (reachable(name) & writes) and not (reachable(name) & guards)
    ]
    assert not unguarded, "只读屏障漏了 GET 写路径：\n  " + "\n  ".join(unguarded)


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


# --- 单进程闸（T8）-------------------------------------------------------------

_APP_DIR = str(Path(__file__).resolve().parents[1])


def _try_acquire_in_subprocess(url: str):
    """在**另一个进程**里抢同一把锁。同进程内 flock 是可重入的，自己抢自己测不出东西。"""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_APP_DIR!r})
        from app import single_process
        try:
            ok = single_process.acquire({url!r})
        except single_process.MultipleInstances as e:
            print("REFUSED", str(e).replace(chr(10), " "))
            raise SystemExit(0)
        print("ACQUIRED" if ok else "SKIPPED")
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=60)


def test_second_instance_on_the_same_data_dir_is_refused(tmp_path):
    """同一份数据目录起第二个进程 → 当场拒绝，并说清为什么。

    插件令牌的撤销表 `scopes._ALIVE` 是**进程内**的 dict：多一个 worker 就多一份空表，
    插件回灌被负载均衡分到另一个进程就是 **401**——「抓了一批单一条都没回来」，
    而日志里只有一串 401，没有任何一处会说「因为你开了多进程」。
    """
    from app import single_process

    url = f"sqlite:///{tmp_path / 'ctl.db'}"
    assert single_process.acquire(url) is True
    try:
        r = _try_acquire_in_subprocess(url)
        assert "REFUSED" in r.stdout, f"第二个进程拿到了锁：{r.stdout!r} {r.stderr[-300:]!r}"
        assert "--workers" in r.stdout, "拒绝了却没说清怎么解决"
    finally:
        single_process.release()


def test_lock_is_per_data_dir_not_global(tmp_path):
    """同机跑两份互不相干的 soroban（不同目录、不同端口）是合法用法，不该被误伤。"""
    from app import single_process

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert single_process.acquire(f"sqlite:///{a / 'ctl.db'}") is True
    try:
        r = _try_acquire_in_subprocess(f"sqlite:///{b / 'ctl.db'}")
        assert "ACQUIRED" in r.stdout, f"另一个目录也被挡住了：{r.stdout!r} {r.stderr[-300:]!r}"
    finally:
        single_process.release()


def test_release_lets_the_next_instance_in(tmp_path):
    """正常关停后，下一次启动必须能拿到锁——否则重启一次就再也起不来了。"""
    from app import single_process

    url = f"sqlite:///{tmp_path / 'ctl.db'}"
    assert single_process.acquire(url) is True
    single_process.release()
    assert single_process.acquire(url) is True
    single_process.release()


def test_a_finishing_hold_does_not_revoke_someone_elses_barrier():
    """超时自愈之后，先来者收工时**不许**撤掉后来者的屏障。

    屏障有硬上限（`DEFAULT_TIMEOUT`）：入口 `_reason_locked()` 到点会自愈，
    于是 A 还没收工、B 也能拿到一个全新的屏障——这本身是对的（否则一次卡死的迁移
    会把维护功能永久锁死）。错的是出口：原先 `finally` 无条件 `self._reason = None`，
    **A 一收工就把 B 的屏障撤了**。

    后果不是报错：B 的迁移在零保护下跑完，中间件放行一切写入，于是出现
    「订单拷过去了、它的物品没拷」「子表引用了尚未拷贝的父行」这类跨表撕裂；
    而 B 的响应仍然是 `{"ok": true}`，日志只说「只读屏障已撤销」，不会说那是别人的。

    现实窗口不小：前端给迁移配的超时是 120 秒，屏障硬上限 900 秒，中间件又明确
    放行 `/api/db/`——用户以为失败、再点一次就复现。
    """
    import time

    from app.maintenance import ReadOnlyBarrier

    b = ReadOnlyBarrier()
    a = b.hold("A 的迁移", timeout=0.05, drain=0)
    a.__enter__()
    time.sleep(0.08)                              # A 的屏障过期，触发自愈

    with b.hold("B 的迁移", timeout=60, drain=0):
        assert b.blocked_reason() == "B 的迁移"
        a.__exit__(None, None, None)              # A 这时候才收工
        assert b.blocked_reason() == "B 的迁移", \
            "A 收工时把 B 的屏障撤掉了——B 的迁移会在零保护下跑完"
    assert b.blocked_reason() is None, "B 自己收工后屏障没撤干净"
