"""插件管理层：发现、账号增删改、路径穿越防护、按账号清理。
用临时 SCRAPER_DIR 造一个假插件，不触碰真实 scraper/、不起子进程。"""
import json
from pathlib import Path

import pytest

from app.config import settings
from app.routers import plugins as plug

PLUGIN_ID = "taobao"


@pytest.fixture()
def fake_plugin(tmp_path, monkeypatch):
    """造 <tmp>/soroban-scraper-taobao/{plugin.toml,.state/}，把 SCRAPER_DIR 指过去。"""
    d = tmp_path / "soroban-scraper-taobao"
    (d / ".state").mkdir(parents=True)
    (d / "plugin.toml").write_text(
        'id = "taobao"\nname = "淘宝订单"\nplatform = "taobao"\nversion = "0.1.0"\n'
        'python = ".venv/bin/python"\nentry = "-m taobao_scraper"\nstate_dir = ".state"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "SCRAPER_DIR", str(tmp_path))
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


@pytest.mark.parametrize("bad", ["../evil", "a/b", "../../etc/passwd", "sub/dir"])
def test_path_traversal_account_names_rejected(client, fake_plugin, bad):
    """账号名会变成 <state_dir>/<name>.json，必须挡住跳出目录。"""
    for ep, params in (
        (f"/api/plugins/{PLUGIN_ID}/account", {"name": bad}),
        (f"/api/plugins/{PLUGIN_ID}/login", {"account": bad}),
    ):
        r = client.post(ep, params=params)
        assert r.status_code == 400, f"{bad!r} @ {ep} → {r.status_code} {r.text[:120]}"


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
    r = client.post(f"/api/plugins/{PLUGIN_ID}/fetch", params={"account": "novenv"})
    assert r.status_code == 400 and "venv" in r.json()["detail"]


def test_fetch_with_no_accounts_is_400(client, fake_plugin):
    r = client.post(f"/api/plugins/{PLUGIN_ID}/fetch")
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


def test_broken_plugin_toml_is_skipped(tmp_path, monkeypatch, client):
    d = tmp_path / "soroban-scraper-broken"
    d.mkdir()
    (d / "plugin.toml").write_text("this is not = valid toml [[[", encoding="utf-8")
    d2 = tmp_path / "soroban-scraper-noid"
    d2.mkdir()
    (d2 / "plugin.toml").write_text('name = "缺 id"\n', encoding="utf-8")
    monkeypatch.setattr(settings, "SCRAPER_DIR", str(tmp_path))
    assert client.get("/api/plugins").json() == []


def test_missing_scraper_dir_is_empty(tmp_path, monkeypatch, client):
    monkeypatch.setattr(settings, "SCRAPER_DIR", str(tmp_path / "does-not-exist"))
    assert client.get("/api/plugins").json() == []


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
