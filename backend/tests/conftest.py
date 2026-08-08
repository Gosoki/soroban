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
os.environ.setdefault("SCRAPER_DIR", str(_TMPDIR / "scraper-none"))   # 隔离真实 scraper/，插件发现恒为空

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import create_db_and_tables, get_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import FxRate, User  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASS = "admin12345"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """建库 + 建 admin + 灌一条基准汇率（很多路由在缺汇率时行为不同，固定它以免测试飘）。"""
    create_db_and_tables()
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
