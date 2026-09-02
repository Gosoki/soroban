"""插件管理层：发现、账号增删改、路径穿越防护、按账号清理。
用临时 PLUGIN_DIR 造一个假插件，不触碰真实 plugins/、不起子进程。"""
import json
from pathlib import Path

import pytest

from app.config import settings
from app.routers import plugins as plug

PLUGIN_ID = "taobao"


@pytest.fixture()
def fake_plugin(tmp_path, monkeypatch):
    """造 <tmp>/soroban-plugin-taobao/{plugin.toml,.state/}，把 PLUGIN_DIR 指过去。

    设的是 **PLUGIN_DIR**（现名）而不是 SCRAPER_DIR（兼容别名）：`plugin_dir()` 优先取现名，
    只设旧名的话这个假插件根本不会被发现，而 conftest 又把现名指向了一个空目录——
    表现是「一堆插件测试突然全红」，看起来像插件系统坏了。
    """
    d = tmp_path / "soroban-plugin-taobao"
    (d / ".state").mkdir(parents=True)
    (d / "plugin.toml").write_text(
        'id = "taobao"\nname = "淘宝订单"\nversion = "0.1.0"\n'
        'python = ".venv/bin/python"\nentry = "-m taobao_scraper"\nstate_dir = ".state"\n'
        'accounts = true\naccounts_ledger_field = "platform_account"\n',   # 与真实清单一致
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path))
    return d


def test_discover_lists_plugin(client, fake_plugin):
    r = client.get("/api/plugins")
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()]
    assert PLUGIN_ID in ids
    p = next(p for p in r.json() if p["id"] == PLUGIN_ID)
    assert p["installed"] is False           # 没造 venv


def test_unknown_plugin_404(client, fake_plugin):
    assert client.post("/api/plugins/nope/account", params={"name": "a"}).status_code == 404


def test_add_and_list_account(client, fake_plugin):
    assert client.post(f"/api/plugins/{PLUGIN_ID}/account",
                       params={"name": "acctA", "platform": "淘宝"}).status_code == 200
    accs = client.get("/api/plugins").json()[0]["accounts"]
    assert {"acctA"} <= {a["account"] for a in accs}


def test_add_duplicate_account_conflicts(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "dup"})
    assert client.post(f"/api/plugins/{PLUGIN_ID}/account",
                       params={"name": "dup"}).status_code == 409


@pytest.mark.parametrize("bad", ["", "   ", "a,b"])
def test_bad_account_names_rejected(client, fake_plugin, bad):
    r = client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": bad})
    assert r.status_code == 400, f"{bad!r} → {r.status_code}"


def _make_it_runnable(client, d):
    """让假插件真的能走到 `run` 端点里的账号校验。

    **两道闸都得先过，否则断言会被别的原因满足**——而它们回的码正好是
    400 与 409，看起来像是「被挡住了」：

      1. `if not _python(m).exists()` → 400「插件未安装：缺 venv」。
         它排在 `_fan_targets` **前面**，所以不造 venv 的话，
         「断言 status == 400」对**任何**账号名都成立。
      2. `if not cfg.enabled` → 409。`PluginConfig.enabled` 默认就是 False。

    下面 traversal 那条测试此前正是被第 1 条满足的（2026-09-02 实测：
    把 `_check_account_name` 整个换成 `pass`，那四个参数一条都不红）。
    两支解释器路径都造，Windows 上跑也成立。
    """
    for rel in ((".venv", "bin", "python"), (".venv", "Scripts", "python.exe")):
        f = d.joinpath(*rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("#!/bin/sh\n", encoding="utf-8")
    client.put(f"/api/plugins/{PLUGIN_ID}/config",
               json={"enabled": True, "schedule_minutes": 0})


@pytest.mark.parametrize("bad", ["../evil", "a/b", "../../etc/passwd", "sub/dir"])
def test_path_traversal_account_names_rejected(client, fake_plugin, bad):
    """账号名会变成 <state_dir>/<name>.json，两个端点都必须挡住带路径分隔符的名字。

    **只断言 `status == 400` 是不够的**：这条路径上至少有三种机制都会给 400，
    删掉其中任何一个，测试照样绿（2026-09-02 逐个破坏实测）：

      1. `run_command` 的「插件未安装：缺 venv」——它排在账号校验**前面**，
         而假插件本来就没有 venv ⇒ 此前 run 那一半对**任何**名字都成立，
         与账号校验毫无关系。`_make_it_runnable` 就是为了拆掉这一条。
      2. `_check_account_name` 的 `_WIN_BAD_CHARS`——它含 `/` 和 `\\`，
         而上面每个用例都带 `/` ⇒ 实际上是它接住的。
      3. `_state_file` 的 `f.parent != d`——它排在 2 之后，
         **在这条路径上永远轮不到**（能改变父目录的字符已经被 2 挡光了）。
         它仍是必要的：改名端点（`/account/rename`）会直接调它。

    所以断言只钉两件可验的事：**被拒**，且**是账号校验拒的**（不是缺 venv 那种
    与名字无关的原因）。想区分 2 与 3 的话得换输入，而能绕过 2 的输入并不存在。
    """
    _make_it_runnable(client, fake_plugin)
    for ep, params in (
        (f"/api/plugins/{PLUGIN_ID}/account", {"name": bad}),
        (f"/api/plugins/{PLUGIN_ID}/run/login", {"account": bad}),
    ):
        r = client.post(ep, params=params)
        assert r.status_code == 400, f"{bad!r} @ {ep} → {r.status_code} {r.text[:120]}"
        assert ("账号昵称" in r.text or "非法账号名" in r.text), (
            f"{bad!r} @ {ep} 的 400 不是账号校验给的：{r.text[:160]}")


@pytest.mark.parametrize("bad", [" 淘宝", "淘宝 ", "\t淘宝", "淘宝\n"])
def test_an_account_name_padded_with_whitespace_never_reaches_the_ledger(
        client, fake_plugin, bad):
    """首尾带空白的账号名必须在**接口**上被拒，不能只靠界面。

    账号名不只是会话文件名，它还**逐字**写进账本的 `Order.platform_account`
    （插件的 `normalize.py` 直接把 `--account` 的值放进去），而那一列是 `BinStr`
    ——逐字节比较。于是 `「淘宝 」` 与 `「淘宝」` 是两个不同的值：
    订单筛选的下拉里多出一个**肉眼一模一样**的账号，那些单永远不在真账号下显示。

    此前挡不住：`add_account` 会先 `name.strip()`，但 `run_command` 的
    `account` 直接取自查询串、一个字都不改，而 `_fan_targets` 又刻意允许
    「配置里没登记的孤儿账号」⇒ `?account=淘宝%20` 一路通到子进程的 `--account`。
    `_check_account_name` 的注释当时还写着「结尾空格不用管：调用方都先 strip() 过」
    ——一句**自称的**不变式。

    走界面碰不到这条路（卡片发的是名单里的名字），但本仓的口径写在别处：
    「接口不能只靠界面把关——那样别的调用方（或手滑的 curl）照样能……」。
    """
    _make_it_runnable(client, fake_plugin)
    r = client.post(f"/api/plugins/{PLUGIN_ID}/run/login", params={"account": bad})
    assert r.status_code == 400, f"{bad!r} → {r.status_code} {r.text[:160]}"
    assert "空白" in r.text, f"400 了，但不是因为首尾空白：{r.text[:200]}"


def test_a_clean_account_name_still_gets_through_the_name_check(fake_plugin):
    """另一半：这道闸不能变成一堵墙——正常名字必须原样通过。

    只钉「拒得住」的话，把校验写成 `raise` 一句也是绿的。
    直接调校验函数而不是走端点：端点后面还会真的去起子进程（假插件那个
    `python` 不是真解释器），那是另一件事，不该混进这条断言。
    """
    m = {"_dir": fake_plugin, "state_dir": ".state"}
    for good in ("淘宝", "闲鱼主号", "taobao-2", "CONSOLE", "a.b"):
        plug._check_account_name(m, good)      # 不抛就算过


def test_state_file_traversal_blocked_directly(fake_plugin):
    from fastapi import HTTPException
    m = {"_dir": fake_plugin, "state_dir": ".state"}
    for bad in ("../x", "a/b", "../../etc/passwd"):
        with pytest.raises(HTTPException):
            plug._state_file(m, bad)


def test_enable_disable_account(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "toggle"})
    r = client.patch(f"/api/plugins/{PLUGIN_ID}/account",
                     params={"account": "toggle", "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    acc = next(a for a in client.get("/api/plugins").json()[0]["accounts"] if a["account"] == "toggle")
    assert acc["enabled"] is False


def test_enable_unknown_account_404(client, fake_plugin):
    assert client.patch(f"/api/plugins/{PLUGIN_ID}/account",
                        params={"account": "ghost", "enabled": True}).status_code == 404


def test_orphan_state_file_is_discovered(client, fake_plugin):
    """DB 被重置后，磁盘上残留的会话文件仍应显示为「未配置但已授权」。"""
    (fake_plugin / ".state" / "orphan.json").write_text("{}", encoding="utf-8")
    accs = client.get("/api/plugins").json()[0]["accounts"]
    o = next(a for a in accs if a["account"] == "orphan")
    assert o["configured"] is False and o["authorized"] is True


def test_delete_account_removes_state_and_config(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "delme"})
    (fake_plugin / ".state" / "delme.json").write_text("{}", encoding="utf-8")
    r = client.delete(f"/api/plugins/{PLUGIN_ID}/account", params={"account": "delme"})
    assert r.status_code == 200 and r.json()["removed_session"] is True
    assert not (fake_plugin / ".state" / "delme.json").exists()
    assert "delme" not in [a["account"] for a in client.get("/api/plugins").json()[0]["accounts"]]


def test_delete_unknown_account_404(client, fake_plugin):
    assert client.delete(f"/api/plugins/{PLUGIN_ID}/account",
                         params={"account": "ghost"}).status_code == 404


def test_rename_account_migrates_orders_and_session(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "old-acct"})
    (fake_plugin / ".state" / "old-acct.json").write_text("{}", encoding="utf-8")
    o = client.post("/api/orders", json={"date": "2026-11-01", "platform_account": "old-acct"}).json()
    st = client.post("/api/staging", json={"order_no": "RN-1", "platform_account": "old-acct"}).json()

    r = client.post(f"/api/plugins/{PLUGIN_ID}/account/rename",
                    params={"old": "old-acct", "new": "new-acct"})
    assert r.status_code == 200, r.text
    assert r.json()["moved_session"] is True
    assert (fake_plugin / ".state" / "new-acct.json").exists()
    assert not (fake_plugin / ".state" / "old-acct.json").exists()
    assert client.get(f"/api/orders/{o['id']}").json()["platform_account"] == "new-acct"
    rows = client.get("/api/staging", params={"limit": 500}).json()["items"]
    assert next(x for x in rows if x["id"] == st["id"])["platform_account"] == "new-acct"


def test_rename_to_occupied_conflicts(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "aa"})
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "bb"})
    assert client.post(f"/api/plugins/{PLUGIN_ID}/account/rename",
                       params={"old": "aa", "new": "bb"}).status_code == 409


def test_rename_unknown_404(client, fake_plugin):
    assert client.post(f"/api/plugins/{PLUGIN_ID}/account/rename",
                       params={"old": "ghost", "new": "x"}).status_code == 404


def test_delete_account_staging_only(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "purge"})
    client.post("/api/staging", json={"order_no": "PG-1", "platform_account": "purge"})
    o = client.post("/api/orders", json={"date": "2026-11-02", "platform_account": "purge"}).json()
    r = client.delete(f"/api/plugins/{PLUGIN_ID}/account/staging", params={"account": "purge"})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert client.get(f"/api/orders/{o['id']}").status_code == 200     # 账本不动


def test_delete_account_orders_soft_deletes(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "purge2"})
    o = client.post("/api/orders", json={"date": "2026-11-03", "platform_account": "purge2"}).json()
    r = client.delete(f"/api/plugins/{PLUGIN_ID}/account/orders", params={"account": "purge2"})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert client.get(f"/api/orders/{o['id']}").status_code == 404


def test_delete_account_orders_resets_imported_staging(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "purge3"})
    s = client.post("/api/staging", json={"order_no": "PG-3", "platform_account": "purge3"}).json()
    client.post(f"/api/staging/{s['id']}/import")
    client.delete(f"/api/plugins/{PLUGIN_ID}/account/orders", params={"account": "purge3"})
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    assert row["imported_order_id"] is None and row["import_status"] == "待处理"


def test_fetch_without_venv_is_400(client, fake_plugin):
    client.post(f"/api/plugins/{PLUGIN_ID}/account", params={"name": "novenv"})
    client.put(f"/api/plugins/{PLUGIN_ID}/config", json={"enabled": True, "schedule_minutes": 0})
    r = client.post(f"/api/plugins/{PLUGIN_ID}/run/fetch", params={"account": "novenv"})
    assert r.status_code == 400 and "venv" in r.json()["detail"]


def test_fetch_with_no_accounts_is_400(client, fake_plugin):
    client.put(f"/api/plugins/{PLUGIN_ID}/config", json={"enabled": True, "schedule_minutes": 0})
    r = client.post(f"/api/plugins/{PLUGIN_ID}/run/fetch")
    assert r.status_code == 400


def test_save_config_clamps_negative_schedule(client, fake_plugin):
    client.put(f"/api/plugins/{PLUGIN_ID}/config",
               json={"enabled": True, "params": {}, "schedule_minutes": -5})
    assert client.get("/api/plugins").json()[0]["config"]["schedule_minutes"] == 0


def test_legacy_comma_accounts_are_parsed(session, fake_plugin):
    """旧格式 accounts='a,b' + 顶层 platform 必须仍能读出结构化账号。"""
    from app.models import PluginConfig
    cfg = PluginConfig(plugin_id="legacy",
                       params_json=json.dumps({"accounts": "a, b", "platform": "闲鱼"}))
    accs = plug._account_list(cfg)
    assert [a["name"] for a in accs] == ["a", "b"]
    assert all(a["platform"] == "闲鱼" and a["enabled"] for a in accs)


def test_corrupt_params_json_does_not_crash():
    from app.models import PluginConfig
    cfg = PluginConfig(plugin_id="broken", params_json="{not json")
    assert plug._load_params(cfg) == {}
    assert plug._account_list(cfg) == []


def test_unparseable_toml_is_skipped_but_bad_manifest_is_shown(tmp_path, monkeypatch, client):
    """TOML 语法坏掉 → 跳过（连 id 都读不出来，没法在界面上称呼它）。
    TOML 能解析但内容有问题（比如缺 id）→ **进列表并显示原因**。

    后者原先也是静默跳过，用户看到的是「插件不见了」，而不是「插件的清单写错了」。
    对着一个空列表没法排查——插件目录明明在那儿。
    """
    d = tmp_path / "plugins" / "soroban-plugin-broken"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text("this is not = valid toml [[[", encoding="utf-8")
    d2 = tmp_path / "plugins" / "soroban-plugin-noid"
    d2.mkdir(parents=True)
    (d2 / "plugin.toml").write_text('name = "缺 id"\n', encoding="utf-8")
    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    got = client.get("/api/plugins").json()
    ids = {p["id"]: p for p in got}
    assert "soroban-plugin-broken" not in ids, "TOML 都解析不了，不该硬凑一条出来"
    assert "soroban-plugin-noid" in ids, "清单有问题的插件不该从界面上消失"
    assert "id" in ids["soroban-plugin-noid"]["manifest_error"], "没告诉用户清单哪里错了"


def test_missing_plugin_dir_lists_only_leftovers(tmp_path, monkeypatch, client):
    """插件目录不存在时，列表里只剩「库里还留着配置」的那些（带 missing 标记）。

    刻意**不是空列表**：残留配置带着用户当初给的授权，藏起来的话，
    以后放一个同 id 的插件进来会静默继承它。
    """
    monkeypatch.setattr(settings, "PLUGIN_DIR", str(tmp_path / "does-not-exist"))
    got = client.get("/api/plugins").json()
    assert all(p["missing"] for p in got), f"目录都没了，却有插件报告说自己装着：{got}"


# --- 依赖安装（缺什么 → 一键补齐）------------------------------------------------
# 背景：插件本体跑在**独立 venv** 里（plugin.toml 的 python 字段），而建这个 venv 一直得用户
# 自己开终端。结果是「把插件丢进目录 → 面板上一片灰按钮 → 只写着『未安装』」，没有下一步。

@pytest.fixture(autouse=True)
def _clear_needs_cache():
    """needs() 带 60s 缓存（它要 spawn 子进程探测，而 GET /api/plugins 是被轮询的）。
    测试间必须清掉，否则前一条用例的结论会串味。"""
    plug._needs_cache.clear()
    plug._install_state.clear()
    yield
    plug._needs_cache.clear()
    plug._install_state.clear()


def test_missing_venv_is_reported_as_a_need(client, fake_plugin):
    """没建 venv 时要说清「缺什么」，而不是笼统一句未安装——三档依赖的补法完全不同。"""
    p = client.get("/api/plugins").json()[0]
    assert p["installed"] is False
    assert [n["key"] for n in p["needs"]] == ["venv"]
    assert p["needs"][0]["label"] and p["needs"][0]["hint"]      # 前端要拿去显示


def test_half_built_venv_still_counts_as_missing(client, fake_plugin):
    """venv 建到一半失败（系统缺 ensurepip 时很常见）会留下 bin/python 这个符号链接。
    只看「文件在不在」会把半成品当成装好了 → 前端不提示重建，却在下一步莫名报缺依赖。
    判据必须是「解释器能不能跑」。"""
    py = fake_plugin / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("not a real interpreter")                      # 存在但跑不起来
    py.chmod(0o755)
    p = client.get("/api/plugins").json()[0]
    assert p["installed"] is False
    assert [n["key"] for n in p["needs"]] == ["venv"]


def test_install_is_rejected_while_running(client, fake_plugin):
    """安装是长任务，重复点不能并发跑两遍（pip 同时写一个 venv 会互相踩）。"""
    plug._install_state[PLUGIN_ID] = {"running": True, "step": "安装 Python 依赖", "error": None}
    assert client.post(f"/api/plugins/{PLUGIN_ID}/install").status_code == 409


def test_install_progress_is_exposed(client, fake_plugin):
    """前端靠轮询 GET /api/plugins 的 install 字段显示进度与失败原因。"""
    plug._install_state[PLUGIN_ID] = {"running": True, "step": "下载浏览器内核", "error": None}
    p = client.get("/api/plugins").json()[0]
    assert p["install"]["running"] is True and p["install"]["step"] == "下载浏览器内核"


def test_install_unknown_plugin_404(client, fake_plugin):
    assert client.post("/api/plugins/nope/install").status_code == 404


def test_venv_cmd_falls_back_when_ensurepip_missing(monkeypatch, tmp_path):
    """Debian/Ubuntu 上「装了 python3 却没装 python3-venv」很常见：ensurepip 缺失，
    标准 `python -m venv` 会建到一半失败。此时必须退到 --without-pip，
    否则用户被堵在「请先 apt install python3-venv」——而那一步他可能没有 sudo。"""
    import subprocess

    real = subprocess.run

    def fake(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[-1] == "import ensurepip":
            return subprocess.CompletedProcess(cmd, 1)           # 假装本机没有 ensurepip
        return real(cmd, *a, **kw)

    monkeypatch.setattr(plug.subprocess, "run", fake)
    assert "--without-pip" in plug._venv_cmd(tmp_path)


def test_pip_cmd_borrows_soroban_pip_when_venv_has_none(monkeypatch, tmp_path):
    """用 --without-pip 建出来的 venv 自己没有 pip。这时借 soroban 的 pip、
    用 `--python` 指过去装（pip 23.1+），而不是要求用户自己 bootstrap。"""
    monkeypatch.setattr(plug, "_has_pip", lambda py: False)
    cmd = plug._pip_cmd(tmp_path / "python", ["-r", "req.txt"])
    assert "--python" in cmd and cmd[0] == plug.sys.executable


def test_pip_cmd_prefers_own_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(plug, "_has_pip", lambda py: True)
    cmd = plug._pip_cmd(tmp_path / "python", ["-r", "req.txt"])
    assert "--python" not in cmd and cmd[0] == str(tmp_path / "python")


def test_needs_is_cached(client, fake_plugin, monkeypatch):
    """GET /api/plugins 是前端轮询的接口，而探测依赖要 spawn 好几个子进程。
    不缓存的话，装依赖时前端秒级轮询会把机器拖垮。"""
    calls = []
    real = plug.probe_needs
    monkeypatch.setattr(plug, "probe_needs", lambda m: (calls.append(1), real(m))[1])
    client.get("/api/plugins")
    client.get("/api/plugins")
    client.get("/api/plugins")
    assert len(calls) == 1, f"探测跑了 {len(calls)} 次，缓存没生效"


# --- 定时调度：两类插件都得跑得起来 -------------------------------------------

class _Cmd:
    """最小命令替身：_fan_targets 只看 per。"""
    def __init__(self, per=None):
        self.per = per


def test_account_based_command_fans_out_per_account():
    """`per = "account"` 的命令：一个启用账号一个子进程，各带自己的平台。"""
    from app.routers.plugins import _fan_targets

    class _Cfg:
        params_json = ('{"accounts": [{"name": "acctA", "platform": "淘宝", "enabled": true},'
                       ' {"name": "acctB", "platform": "闲鱼", "enabled": true},'
                       ' {"name": "acctC", "platform": "淘宝", "enabled": false}]}')

    got = _fan_targets({"accounts": True, "id": "tb"}, _Cfg(), _Cmd("account"))
    assert [e[1] for e in got] == ["acctA", "acctB"], "停用的账号不该展开"
    assert got[0][0] == ["--account", "acctA", "--platform", "淘宝"]


def test_accountless_command_still_runs_once():
    """**本次要支持的主要形态**：汇率、快递查询这类插件没有账号概念。

    以前 `_run_due` 只按账号展开 → 账号为空则一个都不起 → `launched` 恒为 0 →
    `last_run_at` 永不推进 → 这类插件**永远不会被定时触发**，而界面上完全看不出异常
    （显示「已启用」、有定时周期，只是从不运行）。
    """
    from app.routers.plugins import _fan_targets

    class _Cfg:
        params_json = "{}"

    got = _fan_targets({"id": "fx"}, _Cfg(), _Cmd())
    assert got == [([], "")], "无账号命令必须整体跑一次，且不带 --account"


def test_an_orphan_account_is_not_stamped_with_a_hardcoded_platform(tmp_path):
    """**磁盘上有会话、配置里没登记的孤儿账号，不许被凭空安上「淘宝」。**

    点名某个账号那一支原先写死 `.get("platform", "淘宝")`，而它对**任何**账号型插件生效：
    装一个京东插件、点一个只在磁盘上存在的号 → 核心给它的子进程下发 `--platform 淘宝`
    ⇒ 抓回来的单 `platform="淘宝"` 进账本 ⇒ `platform_provider`（OCR 用它决定说哪句话）
    会报出**京东插件的名字**在管淘宝截图。

    不下发时插件用它自己的默认值——那才是「核心不认识任何具体插件」的口径。
    （`_account_list` 里那个同名默认值是**读旧数据的兼容路径**，不能动：
      动它会让库里已有的旧格式账号平台变空。）
    """
    from app.routers.plugins import _fan_targets

    class _Cfg:
        params_json = '{"accounts": [{"name": "已登记", "platform": "闲鱼", "enabled": true}]}'

    m = {"accounts": True, "id": "jd", "_dir": tmp_path, "state_dir": ".state"}

    # 孤儿号：不下发 --platform
    got = _fan_targets(m, _Cfg(), _Cmd("account"), account="孤儿号")
    assert got == [(["--account", "孤儿号"], "孤儿号")], got

    # **反面**：登记过的账号仍按登记值下发，否则等于把这个参数整个关掉
    got2 = _fan_targets(m, _Cfg(), _Cmd("account"), account="已登记")
    assert got2 == [(["--account", "已登记", "--platform", "闲鱼"], "已登记")], got2


def test_fanout_follows_the_command_not_the_plugin():
    """判据是**命令**的 per，不是插件的 accounts。

    这两者曾经在两条路径上各用一个：手动看 cmd.per、定时看 manifest.accounts。
    于是同一条 `per` 未声明的命令，手动跑一次、定时按账号跑 N 次并多带 `--account X`
    ——插件收到一个它在手动路径下从未见过的参数组合，而差异只出现在无人值守那条路上。
    """
    from app.routers.plugins import _fan_targets

    class _Cfg:
        params_json = '{"accounts": [{"name": "x", "platform": "淘宝", "enabled": true}]}'

    # 插件有账号维度，但这条命令没声明 per → 整体跑一次
    assert _fan_targets({"accounts": True, "id": "tb"}, _Cfg(), _Cmd()) == [([], "")]


# --- 目录与命名：插件不只是爬虫 -------------------------------------------------

def test_canonical_plugin_layout():
    """插件放 `plugins/soroban-plugin-*`。

    改名的理由不是审美：`scraper` 这个词把「外部数据获取」窄化成了「爬」，
    而汇率、国际快递查询都没有爬的语义。名字窄了，下一个人就会以为这套机制不适用于它们，
    于是绕过插件另开一条接口——那正是这次重构要消灭的东西。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "plugins").is_dir(), "缺 plugins/ 目录"
    stray = [p.name for p in (root / "plugins").glob("soroban-scraper-*")]
    assert not stray, f"plugins/ 下还留着旧前缀的目录：{stray}"


def test_legacy_scraper_dir_is_still_discovered(tmp_path, monkeypatch):
    """老部署把插件放在 scraper/ 下。升级后**不能**让它们凭空消失——
    那种失败很吓人：插件列表突然空了，用户以为配置丢了。"""
    import tomllib  # noqa: F401  （仅表明 manifest 走 toml 解析）

    from app.routers import plugins as mod

    legacy = tmp_path / "scraper" / "soroban-scraper-old"
    legacy.mkdir(parents=True)
    (legacy / "plugin.toml").write_text('id = "old"\nname = "老插件"\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    assert [m["id"] for m in mod.discover()] == ["old"]


def test_new_prefix_wins_when_both_exist(tmp_path, monkeypatch):
    """搬家搬到一半（新旧目录都有同一个 id）时，新目录赢，不出现两条同名插件。"""
    from app.routers import plugins as mod

    for base, prefix in (("plugins", "soroban-plugin-x"), ("scraper", "soroban-scraper-x")):
        d = tmp_path / base / prefix
        d.mkdir(parents=True)
        (d / "plugin.toml").write_text(f'id = "x"\nname = "{base}"\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    found = mod.discover()
    assert len(found) == 1 and found[0]["name"] == "plugins", f"新目录没赢：{found}"


# --- 配置下发：设置项在核心、插件只声明要读哪几项 -------------------------------

def test_plugin_settings_picks_declared_keys(session):
    """插件用 plugin.toml 的 `settings = [...]` 声明它关心哪些设置，核心把当前值给它。

    设置项**定义在核心**（services/prefs.SPECS）而不是 plugin.toml：设置页已经能按注册表
    自动渲染标签、说明、取值范围、联动禁用；搬进 plugin.toml 等于把这些退回成一个
    要手工编辑的文本文件。插件只说「我要读哪几项」。
    """
    from app.routers.plugins import plugin_settings

    got = plugin_settings(session, {"id": "x", "settings": ["fx.manual_rate", "fx.stale_hours"]})
    assert set(got) == {"fx.manual_rate", "fx.stale_hours"}
    assert isinstance(got["fx.stale_hours"], int)


def test_unknown_setting_key_is_skipped_not_fatal(session, caplog):
    """插件与核心版本不匹配时（插件要一个核心还没有的设置），宁可少给一项，
    也不该让整次触发失败——但必须留一行日志，否则「配置没生效」会查不出原因。"""
    from app.routers.plugins import plugin_settings

    with caplog.at_level("WARNING"):
        got = plugin_settings(session, {"id": "x", "settings": ["fx.stale_hours", "nope.not_real"]})
    assert set(got) == {"fx.stale_hours"}
    assert any("不认识的设置项" in r.getMessage() for r in caplog.records)


def test_plugin_declared_settings_are_real_keys():
    """所有插件 plugin.toml 里声明的设置键，必须是核心注册表里真实存在的。

    写错一个键不会报错——`plugin_settings` 只是跳过它。于是插件拿不到配置、
    按内置默认值跑，而界面上看不出任何异常。这条在打包/发布前就红。
    """
    from app.routers.plugins import discover
    from app.services.prefs import SPECS

    bad = []
    for m in discover():
        for key in (m.get("settings") or []):
            if key not in SPECS:
                bad.append(f"{m['id']}: {key}")
    assert not bad, f"插件声明了不存在的设置项：{bad}"


def test_config_is_passed_by_env_not_argv():
    """设置项可能含 API key。走 argv 的话它会出现在进程表(ps)与日志里。"""
    import ast
    import inspect
    import textwrap

    from app.routers import plugins as mod

    src = textwrap.dedent(inspect.getsource(mod._launch))
    tree = ast.parse(src)
    # cmd = [...] + ... 里不许出现 config
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "cmd" for t in node.targets):
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert "config" not in names, "配置被拼进了命令行——API key 会出现在 ps 里"
    assert "SOROBAN_CONFIG" in src, "配置没有通过环境变量下发"


def test_last_ok_at_only_advances_on_success(client, session):
    """`last_ok_at` **只在成功那次**推进——失败也推的话它就退化成 `last_finished_at` 的副本。

    这一列存在的全部意义是把两者分开：爬虫的登录会话过期之后，每次定时都照跑、照失败，
    「上次跑完」一直很新，「上次抓到东西」才会停在两周前。后者才是那条会变红的线。
    """
    from app.models import PluginConfig
    from app.routers.plugins import _write_outcome

    pid = "soroban-plugin-taobao"
    if session.get(PluginConfig, pid) is None:
        session.add(PluginConfig(plugin_id=pid))
        session.commit()

    _write_outcome(pid, "ok", "抓到 3 单")
    session.expire_all()
    first_ok = session.get(PluginConfig, pid).last_ok_at
    assert first_ok is not None, "成功之后没记下 last_ok_at"

    _write_outcome(pid, "failed", "登录已过期")
    session.expire_all()
    cfg = session.get(PluginConfig, pid)
    assert cfg.last_ok_at == first_ok, "失败也推进了 last_ok_at"
    assert cfg.last_finished_at != first_ok, "失败没推进 last_finished_at（那一列是成败都推的）"


def test_the_card_reports_when_it_last_succeeded(client, session):
    """列表接口要把「上次成功」单独给出来，前端才说得出「跑过了，但 14 天没抓到东西」。"""
    from app.models import PluginConfig

    pid = "soroban-plugin-taobao"
    if session.get(PluginConfig, pid) is None:
        session.add(PluginConfig(plugin_id=pid))
        session.commit()

    body = client.get("/api/plugins").json()
    for p in body:
        assert "ok_at" in p["last_run"], f"{p.get('id')} 的 last_run 里没有 ok_at：{p['last_run']}"


def test_no_module_level_constant_is_silently_shadowed_by_the_re_export():
    """`plugins.py` 里不许再定义一份已经从 `plugins_proc` 再导入的常量。

    O2 那次把进程层拆到 `plugins_proc.py`、在 `plugins.py` 里再导入回来（测试要按名字拿）。
    但 `_REAP_TIMEOUT` 在**再导入之前**还留着一份 `= 1800` 的定义——
    再导入排在后面，于是后者覆盖前者：**改前面那个数静默无效**。

    这条按 AST 判：文件里既有顶层赋值、又出现在那条 `from .plugins_proc import (...)`
    名单里的名字，一个都不许有。按名字 grep 判不了这件事（两处都写着同一个名字）。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "routers" / "plugins.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    imported, assigned = set(), set()
    for node in tree.body:                      # 只看顶层
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("plugins_proc"):
            imported |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.Assign):
            assigned |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    assert imported, "没找到那条 from .plugins_proc import(...)，探测方式可能已过期"
    shadowed = sorted(imported & assigned)
    assert not shadowed, (
        f"这些名字在 plugins.py 里既被再导入、又被本地赋值：{shadowed}\n"
        "再导入排在后面会覆盖本地定义 ⇒ 改本地那份**静默无效**。"
        "唯一真相应当留在 plugins_proc.py。")


def test_a_plugin_result_survives_a_hostile_console_encoding(monkeypatch):
    r"""插件那行结果 JSON，必须**逐字**穿过管道回到核心——不管机器的 locale 是什么。

    父进程这边一直钉着 `encoding="utf-8"`，但子进程用什么编码**写出来**，
    在 2026-09-02 之前没人管：那由子进程自己的 locale 决定。
    POSIX 上碰巧不出事（PEP 538/540 会把 C/POSIX locale 强制成 UTF-8），
    **Windows 上没有这套兜底**，于是两头对不上，而且是静默的：

      · 中文 Windows（cp936）：`{"account":"闲鱼主号"}` 回来是 `{"account":"��������"}`。
      · 而 GBK 的次字节范围含 `0x5C`——常用汉字里 116 个中招（乗、僜、刓…），
        它们让 JSON 串里多出一个裸反斜杠 ⇒ `json.loads` 抛 `Invalid \escape`
        ⇒ `_self_reported_error` 的 `except ValueError: return False`
        ⇒ **插件自报了 error 却被当成没报**，黄色那一档整个失效。
      · 日文 Windows（cp932）：简体字编码不出来，子进程在 `print` 这最后一句上
        抛 `UnicodeEncodeError` ⇒ 退出码 1、stdout 空。而这时候**活已经干完了**
        （订单早就回灌进账本），用户看到的是一张报红的卡片。

    **怎么在 Linux 上把这个 bug 测出来**：`LC_ALL=C` 没用（PEP 538 会救它）。
    要模拟的是「环境里已经写着一个不是 utf-8 的口径」，所以直接把
    `PYTHONIOENCODING=gbk` 放进 `os.environ` —— 这既是最贴近 Windows 的模拟，
    也顺带钉住了真正要的那条性质：**`_child_env()` 必须压过环境里已有的值**，
    而不是「环境里没有时才补一个」。
    """
    import json as _json
    import subprocess
    import sys

    from app.routers import plugins as mod

    # 敌意环境：机器上已经写着一个非 utf-8 的口径（Windows 上这来自 locale）
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")

    # 乗 的 GBK 次字节正好是 0x5C；账号昵称是用户自己起的，中文是常态
    payload = {"ok": False, "account": "闲鱼主号", "error": "乗车券解析失败"}
    child = (
        "import json,sys;"
        "sys.stdout.write(json.dumps(%r, ensure_ascii=False));"
        "sys.stdout.write(chr(10));"
        "sys.stderr.write(sys.stdout.encoding)" % (payload,)
    )
    r = subprocess.run([sys.executable, "-c", child], capture_output=True,
                       encoding="utf-8", errors="replace", env=mod._child_env(), timeout=60)

    assert r.returncode == 0, f"子进程没能把结果打出来：{r.stderr[-300:]}"
    assert r.stderr.strip() == "utf-8", (
        f"子进程的 stdout 编码是 {r.stderr.strip()!r}，不是 utf-8——"
        "_child_env() 没有压过环境里已有的 PYTHONIOENCODING")

    # 两半都要钉：JSON 得解析得出来（0x5C 那一类），内容还得逐字相同（乱码那一类）
    got = _json.loads(r.stdout.strip())
    assert got == payload, f"结果行内容变了：{got!r} != {payload!r}"

    # 而核心据以判断「插件自己说出事了」的那条路，也必须跟着成立
    from app.routers import plugins_proc
    assert plugins_proc._self_reported_error(r.stdout.strip()), \
        "插件自报了 error，核心却没认出来——黄色那一档会失效，卡片显示成绿的"


def test_every_piped_subprocess_pins_both_ends_of_the_encoding():
    """`plugins.py` 里每一个**按文本解码**的子进程调用，两头都要钉住编码。

    父进程那头是 `encoding="utf-8"`，子进程那头是 `env=_child_env(...)`。
    只钉一头等于没钉——这正是上面那条测试描述的 bug 的成因。

    判据落在 AST 的**关键字实参**上，不是搜字符串：`plugins.py` 里 `_child_env`
    这个名字在注释与 docstring 里出现好几次，按文本搜的话，一个只在注释里
    提过它、实参根本没传的调用照样能过。

    **扫的是整个 `app/`，不是写死的某个文件。** 今天三个解码调用确实都在
    `plugins.py`，但「当前的集合」不是「集合的定义」——写死文件名的话，
    哪天别处起一个带管道的子进程，这条守卫会安静地不看它。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"

    # `env=` 常常传的是个**变量**（`env = _child_env({...})` 之后再 `env=env`），
    # 所以先把「哪些名字是从 _child_env 来的」收一遍，认到一层赋值为止。
    bad = []
    seen = 0
    for f in sorted(root.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        from_helper = {
            tgt.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
            for tgt in n.targets
            if isinstance(tgt, ast.Name) and "_child_env" in ast.unparse(n.value)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = ast.unparse(node.func)
            if fn not in ("subprocess.run", "subprocess.Popen"):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            # 只管「会被解码成 str」的那些：text=True 或显式给了 encoding。
            # 不解码的（拿 bytes 的）没有这个问题，别去烦它们。
            decodes = kw.get("encoding") is not None or (
                isinstance(kw.get("text"), ast.Constant) and kw["text"].value is True)
            if not decodes:
                continue
            seen += 1
            where = f"{f.name}:{node.lineno} 的 {fn}"
            enc = kw.get("encoding")
            if not (isinstance(enc, ast.Constant) and enc.value == "utf-8"):
                bad.append(f"{where}：父进程这头没钉 encoding=\"utf-8\"")
            if "errors" not in kw:
                bad.append(f"{where}：没给 errors —— 解码失败会抛 UnicodeDecodeError(ValueError)，"
                           "而这里的 except 多半只接 OSError/SubprocessError，接不住")
            env = kw.get("env")
            env_src = ast.unparse(env) if env is not None else ""
            if env is None or not ("_child_env" in env_src or env_src in from_helper):
                bad.append(f"{where}：子进程那头没钉 —— 要传 env=_child_env(...)，"
                           "否则子进程按自己的 locale 写出来（Windows 上就不是 utf-8）")

    # 反空转：一个都没找到多半是探测方式过期了，而不是「全都合规」
    assert seen >= 3, f"只找到 {seen} 个按文本解码的子进程调用，探测方式可能已过期"
    assert not bad, "子进程编码没有两头钉住：\n  " + "\n  ".join(bad)



def test_every_text_file_is_read_and_written_as_utf8():
    """全仓的文本 IO 一律显式 `encoding="utf-8"`，不许跟随机器 locale。

    不写的话，`open()` / `read_text()` / `write_text()` 用的是
    `locale.getpreferredencoding()`——**Linux 上是 UTF-8，中文 Windows 上是 cp936，
    日文 Windows 上是 cp932**。于是同一份代码在开发机上一切正常，
    到了用户机器上读出来是乱码、或者写出去的文件别的程序读不了，
    而这些文件里装的恰恰全是中文（插件清单、requirements、摘要、状态文件）。

    这与 `_child_env` 钉 `PYTHONIOENCODING` 是同一件事的两半：
    那一半管**进程之间的管道**，这一半管**磁盘上的文件**。

    `Image.open(...)` 这类不算——判据要求 `open` 是**内置函数**（裸名字调用），
    属性调用（`Image.open`、`gzip.open`）走的不是这条路。
    """
    import ast
    from pathlib import Path

    roots = [Path(__file__).resolve().parents[1] / "app"]
    bad, seen = [], 0
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                is_builtin_open = isinstance(n.func, ast.Name) and n.func.id == "open"
                is_path_text = (isinstance(n.func, ast.Attribute)
                                and n.func.attr in ("read_text", "write_text"))
                if not (is_builtin_open or is_path_text):
                    continue
                # 二进制模式没有编码可言
                mode = next((a.value for a in n.args
                             if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
                if is_builtin_open and "b" in mode:
                    continue
                seen += 1
                if not any(k.arg == "encoding" for k in n.keywords):
                    bad.append(f"{f.name}:{n.lineno} 的 {ast.unparse(n.func)}() 没写 encoding")

    # 反空转：探测方式一旦过期，`seen` 会掉到 0，上面那个循环一句都不验、照样绿
    assert seen >= 6, f"只找到 {seen} 处文本 IO，探测方式可能已过期"
    assert not bad, ("这些文本 IO 跟随机器 locale（Windows 上就不是 UTF-8）：\n  "
                     + "\n  ".join(bad))
