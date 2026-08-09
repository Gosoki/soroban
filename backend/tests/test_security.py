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


# --- 源码运行路径也必须默认只绑环回 -------------------------------------------
#
# 上面 test_run_py_defaults_to_loopback 钉的是**打包版**（run.py）。源码运行走的是
# start.sh / start.bat + vite dev server，那条路上后端曾经写死 --host 127.0.0.1、
# 而前端 `vite --host` 监听全网卡——dev server 又把 /api 反代到后端，于是后端那句
# 环回绑定形同虚设：局域网任何设备打 http://<本机IP>:8621 就能同源访问全部 API。
# 现在两边收敛到同一个 HOST 旋钮，这里把它钉死。

_FRONTEND = _REPO / "frontend"
_START_SH = _REPO / "start.sh"
_START_BAT = _REPO / "start.bat"


def test_npm_scripts_do_not_force_host():
    """package.json 里不许再出现 `--host`。

    监听地址属于策略，必须落在 vite.config.js（会被 review 的地方），而不是一个
    npm script 参数——后者是「为了手机调试临时加一下」的重灾区，加回去时没有任何
    东西会提醒作者后端也跟着敞开了。
    """
    import json

    scripts = json.loads((_FRONTEND / "package.json").read_text(encoding="utf-8"))["scripts"]
    bad = [f"{k}: {v}" for k, v in scripts.items() if "--host" in v]
    assert not bad, f"npm 脚本里不许写 --host（监听地址请走 vite.config.js 的 HOST）：{bad}"


def test_vite_dev_server_defaults_to_loopback():
    """vite.config.js 必须显式声明 server.host，且默认值不是通配地址。"""
    src = (_FRONTEND / "vite.config.js").read_text(encoding="utf-8")
    m = re.search(r"process\.env\.HOST\s*\|\|\s*['\"]([^'\"]+)['\"]", src)
    assert m, "vite.config.js 必须用 `process.env.HOST || '<环回默认值>'` 声明监听地址"
    assert m.group(1) in ("localhost", "127.0.0.1"), \
        f"vite dev server 默认监听不能是 {m.group(1)!r}"
    assert re.search(r"server\s*:\s*\{[^}]*host\s*:", src, re.S), \
        "vite.config.js 的 server 块里必须有 host（否则 --host 一加回来就又敞开）"


@pytest.mark.parametrize("script", [_START_SH, _START_BAT])
def test_start_scripts_share_one_host_knob(script):
    """启动脚本不许把后端监听地址写死——前后端必须共用同一个 HOST。"""
    src = script.read_text(encoding="utf-8")
    assert "--host 127.0.0.1" not in src, \
        f"{script.name} 把后端监听写死了；应改用 HOST，否则它与前端的策略会各走各的"
    assert "HOST" in src, f"{script.name} 必须定义并透传 HOST"


@pytest.mark.parametrize("script", [_START_SH, _START_BAT])
def test_start_scripts_do_not_print_default_password(script):
    """启动脚本不许把默认口令回显到控制台。

    run.py 早就为同一理由删过（见 test_run_py_does_not_print_default_password），
    但源码运行路径漏了——而它恰恰是 README 主推的日常工作流。
    （README 里作为文档写明默认口令是正常的，这里只管控制台输出。）
    """
    assert "admin123" not in script.read_text(encoding="utf-8"), \
        f"{script.name} 不许回显默认口令"


# --- 登录退避：不能靠换 IP 绕过，也不能靠并发溜过去 --------------------------------

def test_throttle_survives_a_forged_client_ip():
    """每次换一个来源 IP 也必须被挡住。

    `request.client.host` **不可信**：uvicorn 默认信任来自 loopback 的 X-Forwarded-For，
    而同机反向代理与前端开发代理会让所有请求都长得像 127.0.0.1。
    真机实测过：修之前轮换 XFF 打 12 次一个 429 都没有，修之后第 7 次开始 429。

    **刻意不走 TestClient**：它的 `request.client.host` 恒为 "testclient"，
    XFF 头根本不参与，于是有没有「与 IP 无关」的兜底键这条测试都会绿——
    第一版就是这么写的，是一条假绿。直接驱动退避器才测得到那个键。
    """
    from app.ratelimit import FREE_TRIES, LoginThrottle

    t = LoginThrottle()
    waits = [t.begin("admin", f"10.7.7.{i}") for i in range(FREE_TRIES + 3)]
    assert any(w > 0 for w in waits), f"每次换 IP 就绕过了退避：{waits}"
    # 对照：换用户名**应该**互不牵连（不然一个人手滑能把别人锁出去）
    assert t.begin("someone-else", "10.7.7.0") == 0, "不同用户名之间不该互相牵连"


def test_throttle_counts_before_verifying_so_bursts_cannot_slip_through():
    """判定与计数必须在同一把锁里。

    分成「先查退避、再记失败」两次取锁的话，并发请求会全部在任何一次计数落地之前
    通过检查——一个退避窗口能塞进几十次口令尝试。
    """
    import threading

    from app.ratelimit import FREE_TRIES, LoginThrottle

    t = LoginThrottle()
    allowed = []
    lock = threading.Lock()

    def hit():
        w = t.begin("admin", "1.2.3.4")
        if w == 0:
            with lock:
                allowed.append(1)

    threads = [threading.Thread(target=hit) for _ in range(60)]
    for x in threads:
        x.start()
    for x in threads:
        x.join()
    assert len(allowed) <= FREE_TRIES + 1, \
        f"并发放行了 {len(allowed)} 次，退避形同虚设（上限应为 {FREE_TRIES + 1}）"


def test_unknown_username_costs_the_same_as_a_known_one(session):
    """用户名存在与否要花同样的时间。

    原先不存在的用户名直接短路、根本不跑 bcrypt，而 bcrypt 是几十到几百毫秒量级——
    响应时间差一个数量级，等于把「哪些用户名是真的」100% 可判别地告诉外面。
    """
    import time

    from app.auth import authenticate

    def cost(u):
        xs = []
        for _ in range(3):
            s = time.perf_counter()
            authenticate(session, u, "definitely-wrong")
            xs.append(time.perf_counter() - s)
        return sorted(xs)[1]

    known = cost("admin")
    unknown = cost("zz-no-such-user-zz")
    # 只要求同一量级：绝对值随机器差别很大，差 5 倍以上才算可判别的信道
    assert 0.2 < unknown / max(known, 1e-6) < 5, \
        f"不存在的用户名 {unknown*1000:.0f}ms vs 存在的 {known*1000:.0f}ms —— 时序可判别"
