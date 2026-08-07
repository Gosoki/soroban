"""安全加固的回归测试：分发路径的密钥/监听默认值、登录失败退避。

背景（审计确认的最高危一条）：打包出来的 soroban.exe 曾经**从不生成 .env**，SECRET_KEY 落到
仓库里公开的默认常量，同时默认监听 0.0.0.0、并自动播种 admin/admin123 且把口令打印到控制台。
三者叠加 = 同网段任何人签一个 JWT 就是管理员。这里把修复后的每一环都钉住。
"""
import os
import re
import time
from pathlib import Path

import pytest

from app.main import _INSECURE_KEYS, _check_secret_key
from app.ratelimit import BASE_DELAY, FREE_TRIES, LoginThrottle, login_throttle

_REPO = Path(__file__).resolve().parents[2]
_RUN_PY = _REPO / "backend" / "run.py"


# --- SECRET_KEY 必须 fail-closed ---------------------------------------------

def test_insecure_secret_key_refuses_to_start(monkeypatch):
    from app.config import settings
    for bad in list(_INSECURE_KEYS) + ["short", "x" * 15]:
        monkeypatch.setattr(settings, "SECRET_KEY", bad)
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            _check_secret_key()


def test_strong_secret_key_passes(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SECRET_KEY", "a" * 64)
    _check_secret_key()          # 不该抛


def test_default_secret_key_is_in_the_insecure_list():
    """config.py 的默认值必须被 _INSECURE_KEYS 认出来，否则 fail-closed 形同虚设。"""
    from app.config import Settings
    assert Settings.model_fields["SECRET_KEY"].default in _INSECURE_KEYS


# --- 打包入口：首启生成 .env + 默认只绑环回 ------------------------------------

def test_run_py_generates_env_with_random_secret(tmp_path):
    """冻结路径下没有别的地方会生成 .env——这一步漏了就是公开默认密钥签 JWT。"""
    import run

    assert run.ensure_env(tmp_path) is True
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    m = re.search(r"^SECRET_KEY=(\S+)$", env, re.M)
    assert m, ".env 里没有 SECRET_KEY"
    secret = m.group(1)
    assert secret not in _INSECURE_KEYS and len(secret) >= 32
    # 再调一次不该覆盖用户已有配置
    assert run.ensure_env(tmp_path) is False
    assert (tmp_path / ".env").read_text(encoding="utf-8") == env


def test_run_py_generated_secrets_differ(tmp_path):
    import run
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    run.ensure_env(a); run.ensure_env(b)
    assert (a / ".env").read_text() != (b / ".env").read_text()


def test_run_py_defaults_to_loopback():
    """默认监听必须是 127.0.0.1；要暴露到局域网得显式设 HOST。"""
    src = _RUN_PY.read_text(encoding="utf-8")
    m = re.search(r'os\.environ\.get\("HOST",\s*"([^"]+)"\)', src)
    assert m and m.group(1) == "127.0.0.1", "run.py 的 HOST 默认值必须是 127.0.0.1"


def test_run_py_ensures_env_before_importing_app():
    """.env 必须在 import app.* 之前写好——app.config 在导入时就实例化 Settings 读 .env，
    顺序反了等于没生成。"""
    src = _RUN_PY.read_text(encoding="utf-8")
    i_env = src.index("ensure_env(rt)")
    i_app = src.index("from app.main import app")
    assert i_env < i_app, "ensure_env 必须在 `from app.main import app` 之前调用"


def test_run_py_does_not_print_default_password():
    """别再把 admin/admin123 打到控制台——那是给旁观者/截图/日志看的。"""
    src = _RUN_PY.read_text(encoding="utf-8")
    assert "admin123" not in src


def test_pyinstaller_bat_claim_matches_reality():
    """打包脚本的说明文字曾声称 exe 首启会生成 .env——当时是假的。现在真了，说明也得对得上。"""
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    if ".env" in bat:
        assert "ensure_env" in _RUN_PY.read_text(encoding="utf-8"), \
            "pyinstaller.bat 宣称会生成 .env，run.py 就必须真的生成"


# --- 登录失败退避 -------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_throttle():
    login_throttle.reset()
    yield
    login_throttle.reset()


def test_free_tries_are_not_throttled(anon):
    for _ in range(FREE_TRIES):
        r = anon.post("/api/auth/login", data={"username": "admin", "password": "nope"})
        assert r.status_code == 401, "前几次手滑不该被退避"


def test_throttles_after_free_tries(anon):
    for _ in range(FREE_TRIES + 1):
        anon.post("/api/auth/login", data={"username": "admin", "password": "nope"})
    r = anon.post("/api/auth/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1


def test_throttle_blocks_even_with_correct_password(anon):
    """退避期内即使密码对了也要挡住——否则爆破者猜中的那一刻正好绕过限速。"""
    from tests.conftest import ADMIN_PASS, ADMIN_USER
    for _ in range(FREE_TRIES + 1):
        anon.post("/api/auth/login", data={"username": ADMIN_USER, "password": "nope"})
    r = anon.post("/api/auth/login", data={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 429


def test_successful_login_clears_counter(anon):
    from tests.conftest import ADMIN_PASS, ADMIN_USER
    for _ in range(FREE_TRIES - 1):       # 停在还没触发退避的那一步
        anon.post("/api/auth/login", data={"username": ADMIN_USER, "password": "nope"})
    ok = anon.post("/api/auth/login", data={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert ok.status_code == 200
    # 计数已清零 → 又能再失败 FREE_TRIES 次而不被挡
    for _ in range(FREE_TRIES):
        assert anon.post("/api/auth/login",
                         data={"username": ADMIN_USER, "password": "nope"}).status_code == 401


def test_throttle_is_scoped_per_username(anon):
    """一个用户名被退避，不该牵连另一个用户名。"""
    for _ in range(FREE_TRIES + 2):
        anon.post("/api/auth/login", data={"username": "victim", "password": "nope"})
    assert anon.post("/api/auth/login",
                     data={"username": "victim", "password": "x"}).status_code == 429
    assert anon.post("/api/auth/login",
                     data={"username": "someone-else", "password": "x"}).status_code == 401


# --- 退避算法本身（纯单元，不走 HTTP）-----------------------------------------

def test_backoff_grows_and_is_capped():
    t = LoginThrottle()
    k = t.key("u", "1.2.3.4")
    for _ in range(FREE_TRIES):
        t.record_failure(k)
    assert t.retry_after(k) == 0                      # 免罚额度内
    t.record_failure(k)
    first = t.retry_after(k)
    assert 0 < first <= BASE_DELAY + 1
    for _ in range(30):                               # 猛刷 → 退避应被封顶而不是无限增长
        t.record_failure(k)
    from app.ratelimit import MAX_DELAY
    assert t.retry_after(k) <= MAX_DELAY + 1


def test_entries_are_forgotten_after_window(monkeypatch):
    t = LoginThrottle()
    k = t.key("u", "ip")
    for _ in range(FREE_TRIES + 3):
        t.record_failure(k)
    assert t.retry_after(k) > 0
    from app import ratelimit
    base = time.monotonic()
    monkeypatch.setattr(ratelimit.time, "monotonic",
                        lambda: base + ratelimit.FORGET_AFTER + 1)
    assert t.retry_after(k) == 0, "过了遗忘窗口还在罚，手滑一次会被记一整天"


def test_entry_table_is_bounded():
    """攻击者每次换个用户名，不能把内存撑爆。"""
    from app.ratelimit import MAX_ENTRIES
    t = LoginThrottle()
    for i in range(MAX_ENTRIES + 500):
        t.record_failure(t.key(f"user{i}", "1.1.1.1"))
    assert len(t._fails) <= MAX_ENTRIES


def test_backoff_survives_username_flooding():
    """退避中的条目**恰恰是最旧的**（它一直被 429 挡回、时间戳不再刷新）。
    只按「最旧」淘汰，攻击者刷几千个不存在的用户名就能把它挤出表、计数归零，
    白拿一轮免罚尝试（实测把 900s 惩罚压成 27s）。淘汰必须优先丢**没有惩罚**的条目。"""
    from app.ratelimit import FREE_TRIES, MAX_ENTRIES, LoginThrottle

    t = LoginThrottle()
    victim = ("admin", "1.2.3.4")
    for _ in range(FREE_TRIES + 3):
        t.record_failure(victim)
    assert t.retry_after(victim) > 0, "夹具没把目标打进退避"

    for i in range(MAX_ENTRIES + 500):           # 灌满无惩罚条目
        t.record_failure((f"ghost{i}", "9.9.9.9"))

    assert t.retry_after(victim) > 0, "退避条目被无惩罚条目挤掉了"


def test_prune_is_fail_open():
    """表满也要照常为新 key 建条目。反过来（表满就拒建）是 fail-closed：
    攻击者把表填满高 count 条目就能把你自己稳定锁在门外——用提速换 DoS，更糟。"""
    from app.ratelimit import MAX_ENTRIES, LoginThrottle

    t = LoginThrottle()
    for i in range(MAX_ENTRIES + 200):
        t.record_failure((f"u{i}", "1.1.1.1"))
    fresh = ("brand-new", "2.2.2.2")
    assert t.retry_after(fresh) == 0             # 新 key 不受牵连
    t.record_failure(fresh)                       # 且能被正常记账
    assert t.retry_after(fresh) == 0              # 一次失败还在免罚额度内


def test_frozen_build_never_uses_exe_as_interpreter(monkeypatch):
    """打包后 sys.executable 是 soroban.exe 自己。拿它跑 `-m venv` 不会建环境，
    而是**把 soroban 再启动一遍**（实测子进程真跑了迁移、最后卡在端口占用），
    用户看到的报错是「address already in use」——和建 venv 毫无关系。"""
    import shutil

    from app.routers import plugins as plug

    monkeypatch.setattr(plug.sys, "frozen", True, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert plug._base_python() is None, "冻结且 PATH 无 python 时必须返回 None，而不是 exe"

    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/python3" if n == "python3" else None)
    assert plug._base_python() == "/usr/bin/python3"


def test_install_refuses_instead_of_spawning_shadow_instance(client, monkeypatch):
    """找不到解释器时明确 409，而不是拿 exe 去跑出一个影子实例。"""
    from app.routers import plugins as plug

    monkeypatch.setattr(plug, "_base_python", lambda: None)
    monkeypatch.setattr(plug, "_find_manifest", lambda pid: {"id": pid, "_dir": plug.Path("/tmp")})
    r = client.post("/api/plugins/taobao/install")
    assert r.status_code == 409 and "Python" in r.json()["detail"]
