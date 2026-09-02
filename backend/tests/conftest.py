"""pytest 公共夹具：每个测试会话建一个隔离的临时 SQLite 库 + 已登录的 TestClient。

必须在 import app.* **之前**设好 DATABASE_URL/SECRET_KEY —— app.database 在模块导入时
就构造引擎、app.config 在导入时就实例化 Settings（环境变量优先于 .env，见 pydantic-settings）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# --- 必须先于 app.* 的导入 ---------------------------------------------------
_TMPDIR = Path(tempfile.mkdtemp(prefix="soroban-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR / 'test.db'}"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
# 隔离真实插件目录，插件发现恒为空。**两个名字都要设**：
# PLUGIN_DIR 是现名、SCRAPER_DIR 是兼容别名，只设旧名的话新名会回落到仓库里的
# plugins/，测试就变成「取决于本机装了哪些插件」——那种测试的绿是没有意义的。
os.environ.setdefault("PLUGIN_DIR", str(_TMPDIR / "plugins-none"))
os.environ.setdefault("SCRAPER_DIR", str(_TMPDIR / "scraper-none"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import migrate_to_latest, get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import FxRate, User  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASS = "admin12345"


@pytest.fixture(autouse=True, scope="session")
def _never_touch_the_real_backups_dir():
    """测试**永远**不许往仓库真实的 `backups/` 里写东西。

    靠每条用例自己记得 patch `_default_dir` 是靠不住的：2026-08-22 就漏了一次，
    一份 304 单的测试库快照落进了生产备份目录——而备份目录里多出一份来路不明的
    「备份」，正是最会误导人的东西（真出事时可能被当成可用的那一份拿去恢复）。

    这里把落点**从结构上**钉死在会话临时目录里。需要自己目录的用例照样可以再 patch 一次。
    """
    import app.backup as _bk

    original = _bk._default_dir
    _bk._default_dir = lambda: _TMPDIR / "backups"
    try:
        yield
    finally:
        _bk._default_dir = original


@pytest.fixture(autouse=True)
def _the_global_engine_never_leaves_the_temp_dir():
    """每条用例跑完，**进程级的数据库指向必须还在会话临时目录里**。

    `DATABASE_URL` 在 import `app.*` 之前就被指到临时目录了，那是结构性的防线。
    但 `set_data_engine()` 是**进程级全局**，用例中途换掉它、跑完忘了换回来，
    后面每一条用例都会跟着跑到别的库上去。

    2026-09-01 真踩过：一条恢复相关的用例调了 `set_data_engine(临时库)` 不还原，
    单跑绿，全量跑时后面 4 条用例一起 ERROR，而报错内容与那条用例毫无关系——
    最难查的正是这种「红的地方不是错的地方」。
    本文件里 `ledger` 夹具那套（`monkeypatch` 掉 `get_engine`）之所以是对的，
    就是因为 monkeypatch 会自动还原；直接调 `set_data_engine` 不会。

    判据落在**路径**上而不是「跟用例前一样」：后者会把「用例故意换了又换回来」
    和「换到别处没还」当成同一件事，而只有后一种是问题。
    """
    import app.database as dbmod

    yield
    try:
        url = str(dbmod.get_engine().url)
    except Exception:                                   # noqa: BLE001  引擎没初始化
        return
    ok = str(_TMPDIR) in url or ":memory:" in url or "/tmp/" in url or "pytest-" in url
    assert ok, (
        f"这条用例跑完之后，进程级的数据引擎还指着 {url}\n"
        f"——它必须留在会话临时目录（{_TMPDIR}）里，否则后面每一条用例都会跑到别的库上，"
        f"而它们报的错与真正出问题的这一条毫无关系。\n"
        f"用 `monkeypatch.setattr(app.database, \"get_engine\", ...)`（会自动还原），"
        f"不要直接调 `set_data_engine()`。")


@pytest.fixture(autouse=True)
def _reset_plugin_process_state():
    """每条用例前后清掉插件的两张进程级全局表。

    `_INFLIGHT`（互斥键）与 `_ALIVE_PROCS`（在飞进程注册表）都是模块级的，
    而清理它们的是**收割线程**——某条用例走真的 `_launch` 起了个进程、自己先跑完了，
    残留就会留到下一条用例。两种表现都是「单独跑绿、整套跑红」，而且红的是**无关的**那条：
      · `_INFLIGHT` 残留 → 后面的用例点同一个命令吃 409；
      · `_ALIVE_PROCS` 残留 → `shutdown_plugins()` 会去 poll 上一条用例的假进程。
    与 test_maintenance 里的 `barrier.reset()` 同一个道理：模块级状态必须在用例之间归零。
    """
    from app.routers import plugins as mod

    def clear():
        with mod._PROCS_LOCK:
            mod._INFLIGHT.clear()
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()
        from app.plugins import runlog
        runlog.reset()          # 同理：按 run 聚合的核心事实表也是模块级的
        from app.services import fx as _fx
        _fx.reset_warning_throttle()   # 「库里没汇率」的每分钟节流窗，同样是模块级的

    clear()
    yield
    clear()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """建库 + 建 admin + 灌一条基准汇率（很多路由在缺汇率时行为不同，固定它以免测试飘）。"""
    migrate_to_latest()
    with Session(get_engine()) as s:
        if not s.exec(select(User).where(User.username == ADMIN_USER)).first():
            s.add(User(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASS), display_name="管理员"))
            s.commit()
    yield


@pytest.fixture(scope="session")
def token(_schema) -> str:
    with TestClient(app) as c:
        r = c.post("/api/auth/login", data={"username": ADMIN_USER, "password": ADMIN_PASS})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]


@pytest.fixture()
def client(token) -> TestClient:
    """已带 Authorization 头的客户端。不进 lifespan（不起后台循环），避免测试间互相干扰。"""
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


@pytest.fixture()
def anon() -> TestClient:
    """未认证客户端（用于鉴权测试）。"""
    return TestClient(app)


@pytest.fixture()
def mk(client):
    """断言式造数：POST 必须成功，否则当场红。

    裸 `client.post(...)` 造数是本套件的历史地雷——写模型是 `extra="forbid"`，
    键名一旦漂移（改列名那类重构）POST 全部 422，行根本没建出来，而断言
    「这些行不计入合计」在**没有这些行**时同样成立，于是重构与守卫同时失效、测试全绿。
    造数一律走这里，让下一个人默认掉进正确的坑。
    """
    def _mk(url: str, payload: dict) -> dict:
        r = client.post(url, json=payload)
        assert r.status_code == 200, f"造数失败 {url}: {r.status_code} {r.text}"
        return r.json()

    return _mk


@pytest.fixture()
def session():
    with Session(get_engine()) as s:
        yield s


@pytest.fixture()
def fx_today():
    """保证「今天」有一条汇率记录，供 create 时自动填汇率的路径使用。"""
    import datetime as dt

    from app.services.fx import JST

    today = dt.datetime.now(JST).date()
    with Session(get_engine()) as s:
        row = s.exec(select(FxRate).where(FxRate.date == today)).first()
        if not row:
            from decimal import Decimal

            s.add(FxRate(date=today, rate=Decimal("20.0000")))
            s.commit()
    return today
