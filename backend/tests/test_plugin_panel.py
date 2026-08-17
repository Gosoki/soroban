"""插件面板：所有插件长得一样，各自按清单声明渲染。

验收点是「加一个插件不用改前端一行」。所以这里钉的是**声明 → 渲染数据**这条链，
而不是某个插件的具体样子。
"""
from __future__ import annotations

import io
import itertools
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.models import PluginConfig
from app.plugins import manifest as pmanifest
from app.plugins import params as plugin_params

_REPO = Path(__file__).resolve().parents[2]


def _mf(**over):
    raw = {"id": "t", "name": "测试插件", "entry": "-m t"}
    raw.update(over)
    return pmanifest.parse(raw, _REPO / "plugins" / "soroban-plugin-fx")


# --- 清单解析与 v0 兼容 --------------------------------------------------------

def test_legacy_manifest_still_gets_buttons():
    """老清单（没有 [[commands]]）必须自动得到 login/fetch。

    不做兼容的话，升级当天所有插件的按钮会一起消失——而用户完全不知道发生了什么。
    """
    m = _mf(accounts=True)
    assert [c.name for c in m.commands] == ["login", "fetch"]
    assert all(c.per == "account" for c in m.commands)


def test_accountless_legacy_manifest_has_no_login():
    """没有账号维度就没有「登录」可言——合成时要把它去掉，
    否则卡片上会出现一个点了必然失败的按钮。"""
    m = _mf()
    assert [c.name for c in m.commands] == ["fetch"]
    assert m.commands[0].per == "plugin"


def test_account_command_on_accountless_plugin_is_downgraded():
    """声明 per=account 却没有账号维度 → 降级成整体执行并告警。
    留着的话那个按钮永远跑不起来，而界面上看不出为什么。"""
    m = _mf(commands=[{"name": "run", "per": "account"}])
    assert m.command("run").per == "plugin"


def test_bad_manifest_is_reported_not_hidden():
    """清单有问题的插件要能在界面上看到并说明原因，而不是凭空消失。"""
    m = pmanifest.parse({"name": "缺 id"}, _REPO / "plugins" / "soroban-plugin-x")
    assert m.id == "soroban-plugin-x"          # 用目录名兜底，至少能称呼它
    assert "id" in m.error


@pytest.mark.parametrize("bad,why", [
    ({"key": "k", "type": "nope"}, "未知类型退回 str"),
    ({"key": "k", "type": "select"}, "select 没给 choices 退回 str"),
])
def test_bad_param_declaration_degrades_instead_of_crashing(bad, why):
    """插件清单写错不该让整个插件页 500——降级 + 告警。"""
    m = _mf(params=[bad])
    assert m.params[0].type == "str", why


def test_param_without_key_is_dropped():
    m = _mf(params=[{"label": "没有 key"}])
    assert m.params == ()


# --- 参数：校验、存取、脱敏 ----------------------------------------------------

def test_param_values_are_coerced_and_bounded():
    m = _mf(params=[{"key": "n", "type": "int", "min": 1, "max": 10, "default": 5}])
    cfg = PluginConfig(plugin_id="t")
    assert plugin_params.save(m, cfg, {"n": "7"})["n"] == 7
    with pytest.raises(ValueError):
        plugin_params.save(m, cfg, {"n": 99})


def test_defaults_fill_in_so_plugins_never_write_them_twice():
    """插件拿到的永远是完整一份。让插件自己兜默认值 = 同一个默认值写两处，
    漂移的表现是「界面上显示 A、实际按 B 跑」。"""
    m = _mf(params=[{"key": "n", "type": "int", "default": 3},
                    {"key": "s", "type": "str", "default": "x"}])
    assert plugin_params.load(m, PluginConfig(plugin_id="t")) == {"n": 3, "s": "x"}


def test_unknown_param_is_dropped_not_fatal():
    """插件降级后界面上那份旧表单不该让保存整体失败。"""
    m = _mf(params=[{"key": "n", "type": "int", "default": 1}])
    cfg = PluginConfig(plugin_id="t")
    assert plugin_params.save(m, cfg, {"n": 2, "gone": "x"}) == {"n": 2}


def test_secret_value_never_leaves_the_backend():
    """secret 参数只回「已设置/未设置」，不回值；日志里也要脱敏。"""
    m = _mf(params=[{"key": "key", "type": "secret"}])
    cfg = PluginConfig(plugin_id="t")
    plugin_params.save(m, cfg, {"key": "s3cr3t"})
    shown = pmanifest.describe_params(m, plugin_params.load(m, cfg))
    assert shown[0]["value"] == "__set__" and "s3cr3t" not in json.dumps(shown)
    assert plugin_params.redact(m, plugin_params.load(m, cfg))["key"] == "***"


def test_corrupt_stored_params_fall_back_to_defaults(caplog):
    """库里存着坏值（手改过/插件降级）不该让插件页打不开。"""
    m = _mf(params=[{"key": "n", "type": "int", "default": 4}])
    cfg = PluginConfig(plugin_id="t", params_json="not json")
    with caplog.at_level("WARNING"):
        assert plugin_params.load(m, cfg) == {"n": 4}


# --- 面板渲染数据：加插件不用改前端 --------------------------------------------

_FAKE_TOML = """
id = "demo"
name = "演示插件"
python = "inherit"
entry = "-m demo"
scopes = ["fx:write"]

[[commands]]
name = "run"
label = "跑一下"
primary = true
needs = ["fx:write"]

[[commands]]
name = "probe"
label = "只测试"

[[params]]
key = "n"
label = "次数"
type = "int"
default = 3
min = 1
max = 9
"""


@pytest.fixture()
def fake_plugin(tmp_path, monkeypatch):
    """造一个插件目录并把 PLUGIN_DIR 指过去。

    刻意**不用**仓库里那两个真插件：那样测的是「我这台机器装了什么」，
    而这里要测的是「任意插件的声明能不能正确长成面板」。
    """
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-demo"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(_FAKE_TOML, encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()      # 60s 缓存按插件 id 存，不清的话拿到的是别的用例的探测结果
    return d


def test_list_gives_frontend_everything_it_needs(client, fake_plugin):
    """插件页整张卡片按这些字段渲染。少一样就得回去改前端——那正是要消灭的。"""
    got = client.get("/api/plugins").json()
    assert got, "一个插件都没发现"
    for p in got:
        for k in ("params", "commands", "accounts_enabled", "last_run", "scopes", "manifest_error"):
            assert k in p, f"插件 {p['id']} 的渲染数据缺 {k}"
        for c in p["commands"]:
            assert {"name", "label", "per", "blocked", "primary"} <= set(c)


def test_accountless_plugin_declares_no_account_section(client, fake_plugin):
    """没声明 accounts 的插件，卡片上不该出现账号区——那是纯噪音，
    还会让人以为自己漏配了什么（汇率插件原先就长这样）。"""
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert got["demo"]["accounts_enabled"] is False


def test_command_blocked_by_missing_scope(client, fake_plugin):
    """缺权限的命令在界面上直接禁用并说明缺哪一项，而不是让人点了收 403。"""
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    cmd = next(c for c in got["demo"]["commands"] if c["name"] == "run")
    assert "fx:write" in cmd["blocked"], "没授权却显示可执行"

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    cmd = next(c for c in got["demo"]["commands"] if c["name"] == "run")
    assert cmd["blocked"] == [], "授权后仍显示缺权限"


def test_unknown_command_is_404_with_the_known_list(client, fake_plugin):
    """插件比核心新时，报错要说清核心认识哪些命令——否则只能靠猜。"""
    r = client.post("/api/plugins/demo/run/not-a-command")
    assert r.status_code == 404
    assert "run" in r.json()["detail"]


def test_running_a_command_without_scope_is_409_not_403(client, fake_plugin):
    """缺权限时在**触发前**就拒绝，并说缺哪一项。
    让子进程跑起来再收一串 403 的话，用户只能去日志里找原因。"""
    # 自己建立前提：client 夹具的库在整个会话里共享，上一条用例授过权且没收回。
    # 依赖用例顺序的测试会在「单跑这一条」时表现不同——那种绿是骗人的。
    client.put("/api/plugins/demo/grants", json={"granted": []})
    # 先启用：「停用」那道闸排在权限之前，不打开的话这条测到的是另一件事
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    r = client.post("/api/plugins/demo/run/run")
    assert r.status_code == 409
    assert "fx:write" in r.json()["detail"]


# --- 结果摘要 -----------------------------------------------------------------

@pytest.mark.parametrize("line,code,want", [
    ('{"created": 3, "updated": 1}', 0, "新建 3、更新 1"),
    ('{"rate": "23.36940", "source": "boc"}', 0, "1元 = 23.36940円（boc）"),
    ('{"error": "全部汇率源都取不到"}', 1, "全部汇率源都取不到"),
    ("", 1, "退出码 1"),
    ("不是 JSON", 0, "不是 JSON"),
])
def test_summary_is_human_readable(line, code, want):
    """插件之间字段不统一（爬虫回 created/updated，汇率回 rate/source）。
    核心不规定插件必须回什么，但也不该让用户在卡片上看一坨 JSON。"""
    from app.routers.plugins import _summarize

    assert _summarize(line, code) == want


# --- 结构守卫：核心不许认识任何具体插件 ----------------------------------------

def test_core_does_not_hardcode_any_plugin_id():
    """核心代码里不许出现具体插件的 id。

    原先有三处 `if manifest["platform"] != "taobao"` 把一个插件的名字焊进了核心——
    于是「按账号改名/删单」这套能力**永远只有淘宝能用**，第二个有账号的插件来了得改核心。
    现在改成由清单声明 `accounts_ledger_field`。

    查的是**字符串字面量**（AST 取常量），不是全文 grep：解释这段历史的注释里
    必然会写到 "taobao"，文本匹配会被自己的注释绊倒。
    """
    import ast

    # 只查**插件子系统**，且只查无歧义的 id：
    #   · "fx" 在别处是领域词（services/fx.py 是汇率服务自己，日志名就叫 fx）；
    #   · "淘宝" 是 Order.platform 的合法取值，到处都有。
    # 查得太宽会产生假红，而假红的下场是有人把断言改松——那就等于没有守卫。
    known_plugin_ids = {"taobao"}
    roots = [Path(__file__).resolve().parents[1] / "app" / "routers" / "plugins.py",
             *(Path(__file__).resolve().parents[1] / "app" / "plugins").rglob("*.py")]
    bad = []
    for f in sorted(roots):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in known_plugin_ids:
                bad.append(f"{f.name}:{node.lineno} 出现了插件 id {node.value!r}")
    assert not bad, (
        "核心里写死了具体插件的 id：\n  " + "\n  ".join(bad)
        + "\n改成由 plugin.toml 声明能力（如 accounts_ledger_field），核心只认声明。")


def test_manifest_declares_everything_the_panel_needs():
    """元断言：Manifest 上少一个字段，面板就有一块渲染不出来。

    这条钉的是「面板完全由声明驱动」这件事本身——将来有人往前端塞一个
    写死的判断（比如 `if plugin.id === 'taobao'`），这里不会红，
    但 test_core_does_not_hardcode_any_plugin_id 的前端版会（见 test_consistency）。
    """
    fields = set(pmanifest.Manifest._fields)
    need = {"id", "name", "accounts", "params", "commands", "scopes", "settings",
            "ledger_field", "error"}
    assert need <= fields, f"清单缺字段：{sorted(need - fields)}"


def test_frontend_does_not_hardcode_any_plugin_id():
    """前端同理：卡片必须按下发的声明渲染，不许按插件 id 分支。"""
    import re

    src = (_REPO / "frontend" / "src" / "views" / "Plugins" / "index.vue").read_text(encoding="utf-8")
    tpl = src.split("<script", 1)[0] + src.split("</script>", 1)[-1]
    bad = [m for m in re.findall(r"['\"](taobao|fx)['\"]", src)]
    assert not bad, f"前端按插件 id 分支了：{set(bad)}——应当只按后端下发的声明渲染"


def test_disabled_plugin_cannot_be_run(client, fake_plugin):
    """「启用」是总开关：停用的插件手动也执行不了。

    界面上按钮会禁用，但接口不能只靠界面把关——别的调用方（或手滑的 curl）
    照样能把一个用户明确停用的插件跑起来。原先这个开关只管定时，
    名字叫「启用定时」，管得比看上去窄。
    """
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": False, "schedule_minutes": 0})
    r = client.post("/api/plugins/demo/run/run")
    assert r.status_code == 409 and "停用" in r.json()["detail"]


def test_enabled_plugin_passes_the_switch(client, fake_plugin):
    """反面：启用之后这道闸不该再挡（挡住的话就成了「开关打不开」）。"""
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    r = client.post("/api/plugins/demo/run/run")
    # 走到这一步说明过了「停用」与「权限」两道闸；再往后是真起子进程（本机没有 demo 的入口）
    assert r.status_code != 409 or "停用" not in str(r.json().get("detail", ""))


def test_token_carries_only_the_scopes_the_command_declared(client, fake_plugin, monkeypatch):
    """令牌只带**这条命令**声明的 needs，不是插件的全部授权。

    `needs` 原先只用来禁按钮，令牌照发全量：于是 `needs = []` 的「只测试、不写入」
    命令拿到的是能写汇率的完整令牌。卡片上写着这条命令不需要任何权限，
    实际下发的却是插件的全部能力——授权说明与真实能力对不上，
    而这个字段的字面意思正是「我要用哪些权限」。
    """
    from app.routers import plugins as mod

    issued = []
    monkeypatch.setattr(mod.scopes, "issue",
                        lambda user, pid, scps, **kw: (issued.append(set(scps)), ("tok", "jti"))[1])
    monkeypatch.setattr(mod, "_launch", lambda *a, **kw: 4242)

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})

    assert client.post("/api/plugins/demo/run/probe").status_code == 200
    assert issued == [set()], f"needs=[] 的命令拿到了 {issued}"

    issued.clear()
    assert client.post("/api/plugins/demo/run/run").status_code == 200
    assert issued == [{"fx:write"}], f"声明了 fx:write 的命令拿到了 {issued}"


# --- 插件被删掉之后 -------------------------------------------------------------

def test_deleted_plugin_leaves_a_visible_leftover(client, fake_plugin, tmp_path, monkeypatch):
    """删掉插件目录之后，它在库里的配置（**含授权**）必须在界面上看得见。

    这是用户问出来的一个真问题：装了汇率插件、授了权、跑过一次，然后把目录删掉——
    `pluginconfig` 里留下一行 `granted_scopes=["fx:write","meta:read"]`，
    而界面上完全看不到。以后放一个同 id 的插件进来（别人写的、或被改过的），
    它会**静默继承**那份授权，用户从没重新批准过。
    """
    from app.routers import plugins as mod

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 30})

    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "gone"))
    mod._needs_cache.clear()
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert "demo" in got, "插件删掉后，它残留的配置从界面上消失了"
    assert got["demo"]["missing"] is True
    assert got["demo"]["scopes"]["granted"] == ["fx:write"], "残留的授权没暴露出来"
    assert got["demo"]["commands"] == [], "插件都不在了，不该还列出可执行的命令"


def test_leftover_config_can_be_cleaned(client, fake_plugin, tmp_path, monkeypatch):
    """清理之后那份授权就真的没了——下次装同 id 的插件必须重新授权。"""
    from app.routers import plugins as mod

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "gone"))
    mod._needs_cache.clear()
    assert client.delete("/api/plugins/demo/config").status_code == 200
    assert "demo" not in {p["id"] for p in client.get("/api/plugins").json()}


def test_installed_plugin_config_cannot_be_wiped_by_accident(client, fake_plugin):
    """还装着的插件不许用这个接口清配置——误点一下把授权和账号全清掉太伤。
    要停用就用卡片上的开关。"""
    r = client.delete("/api/plugins/demo/config")
    assert r.status_code == 409 and "还装着" in r.json()["detail"]


def test_fx_card_tells_the_truth_about_who_provides_rates(client, fake_plugin, tmp_path, monkeypatch):
    """`GET /api/fx` 要如实说「汇率现在**真的会不会**自动更新」——三态，不是两态。

    「声明了 fx:write」只是第一关。原先判到这里就返回插件名，于是把插件**停用**之后，
    设置页仍写着「自动获取由『X』负责」，而它永远不会再跑。
    这条假话很贵：汇率停更时账本会继续用兜底值建单，而用户以为一切正常。

    三态：能跑 → auto_provider=名字；装了但跑不起来 → auto_blocked=原因；
    压根没装 → 两个都空。
    """
    from app.routers import plugins as mod

    # (1) 装了、但没授权 fx:write → 不能说「由它负责」
    client.put("/api/plugins/demo/grants", json={"granted": []})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    got = client.get("/api/fx").json()
    assert got["auto_provider"] == "", "没授权也说在自动取汇率"
    assert "授权" in got["auto_blocked"], got["auto_blocked"]

    # (2) 授权了、但总开关关着 → 同样不算
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": False, "schedule_minutes": 0})
    got = client.get("/api/fx").json()
    assert got["auto_provider"] == "", "停用了也说在自动取汇率"
    assert "停用" in got["auto_blocked"], got["auto_blocked"]

    # (3) 授权 + 启用 → 这才是真的
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    got = client.get("/api/fx").json()
    assert got["auto_provider"] == "演示插件" and got["auto_blocked"] == ""

    # (4) 插件目录没了 → 两个都空，界面回到「没有能自动取汇率的插件」
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "gone"))
    mod._needs_cache.clear()
    got = client.get("/api/fx").json()
    assert got["auto_provider"] == "" and got["auto_blocked"] == "", "插件没了却还有话说"


# --- 文案：标签要短，说明进提示 ------------------------------------------------

def test_scope_labels_stay_short():
    """权限的 `label` 是勾选框后面的短名，别往里塞解释。

    它要跟在复选框后排成一列——长了就把每一行撑成一根长条，长短不一还扫不出重点。
    要说的话放 `hint`（悬停才显示，能换行、能限宽）。
    """
    from app.plugins.scopes import SCOPES

    # 五个字上下：短到能扫、长到能读懂。放宽到 8 是给「插件自有存储」这类留余地，
    # 真正要拦的是有人把整句解释塞进 label。
    long = {k: v.label for k, v in SCOPES.items() if len(v.label) > 8}
    assert not long, f"这些权限名太长了，把解释挪进 hint：{long}"


def test_scope_hints_do_not_claim_one_row_per_day():
    """汇率一天可以有多条了（每次抓取追加一条）。

    权限说明里若还写着「每天最多一行」，那是在向用户描述一个已经不存在的限制——
    比不写更糟。这条守的是「文案跟着行为走」，不是措辞洁癖。
    """
    from app.plugins.scopes import SCOPES

    stale = [k for k, v in SCOPES.items() if "每天最多" in v.hint or "一天一行" in v.hint]
    assert not stale, f"这些权限说明与「一天多条」的现行行为矛盾：{stale}"


def test_long_hints_are_shown_in_a_wrapping_tooltip():
    """长说明必须走会换行的提示气泡，不能塞在行内。

    Element 的 tooltip 默认不换行，长句会拉成一根横贯屏幕的长条；popper 挂在 body 上，
    scoped 样式够不着，所以限宽与换行放在全局的 `.wrap-tip`。
    这条同时钉住「有 .wrap-tip 这个类」和「用到它的地方确实带上了」。
    """
    import re

    css = (_REPO / "frontend" / "src" / "styles" / "tokens.css").read_text(encoding="utf-8")
    assert "wrap-tip" in css, "缺全局的 .wrap-tip 样式，提示会拉成一根长条"
    assert "max-width" in css.split("wrap-tip")[1][:200], ".wrap-tip 没限宽"

    bad = []
    for f in sorted((_REPO / "frontend" / "src").rglob("*.vue")):
        for tag in re.findall(r"<el-tooltip\b[^>]*>", f.read_text(encoding="utf-8"), re.S):
            if "popper-class" not in tag:
                bad.append(f"{f.name}: {tag[:60]}")
    assert not bad, "这些 tooltip 没带 popper-class（长内容会不换行）：\n  " + "\n  ".join(bad)


# --- 清单建议的定时间隔 --------------------------------------------------------

def _with_default_schedule(session, fake_plugin, minutes="360"):
    """给 demo 插件的清单加上 default_schedule_minutes，并**清掉库里的残留配置行**。

    清这一步是必须的：整个测试会话共用一个库，同文件前面的用例已经给 demo
    存过配置了。不清的话这几条测的是「上一个用例留下的值」，而不是清单建议值——
    而且会以「通过」的形式骗过去（残留恰好是 0 时）。
    """
    f = fake_plugin / "plugin.toml"
    # 必须插在**最前面**：_FAKE_TOML 末尾是 [[params]]，追加的顶层键会被 TOML
    # 归到那张表里，插件的建议间隔就永远读不出来（而且不报错）。
    f.write_text(f'default_schedule_minutes = {minutes}\n{_FAKE_TOML}', encoding="utf-8")
    row = session.get(PluginConfig, "demo")
    if row is not None:
        session.delete(row)
        session.commit()
    return fake_plugin


def test_manifest_can_suggest_a_schedule_so_enabling_actually_fetches(client, session, fake_plugin):
    """清单声明了建议间隔 → 还没配置过的插件，页面上读到的就是它。

    没有这条的话，装上插件、打开开关，然后**什么也不会发生**——
    schedule_minutes 默认 0 而「0=不定时」只写在字段注释里，界面上看不出来。
    """
    _with_default_schedule(session, fake_plugin)
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert got["demo"]["config"]["schedule_minutes"] == 360


def test_user_saved_zero_beats_the_manifest_suggestion(client, session, fake_plugin):
    """用户明确存了 0（=不要定时），建议值不许把它顶回去。"""
    _with_default_schedule(session, fake_plugin)
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert got["demo"]["config"]["schedule_minutes"] == 0, "用户的选择被清单建议覆盖了"


def test_granting_first_does_not_swallow_the_suggested_schedule(client, session, fake_plugin):
    """先点授权（会先建出配置行）再看间隔，仍应是清单建议的值。

    这是真实的点击顺序：装好插件 → 勾权限 → 打开开关。
    配置行如果在授权那步被建成 schedule_minutes=0，建议值就再没机会生效了。
    """
    _with_default_schedule(session, fake_plugin)
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert got["demo"]["config"]["schedule_minutes"] == 360, "授权那步把建议间隔吃掉了"


def test_garbage_schedule_in_manifest_does_not_break_the_page(client, session, fake_plugin):
    """手写 toml 把间隔写成字符串/负数，插件页照常打开，按 0 处理。"""
    _with_default_schedule(session, fake_plugin, minutes='"六小时"')
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert got["demo"]["config"]["schedule_minutes"] == 0


# --- 账号落在账本的哪一列，由清单说了算 ------------------------------------------

_ACCT_TOML = """
id = "demo"
name = "演示插件"
python = "inherit"
entry = "-m demo"
accounts = true
accounts_ledger_field = "platform"
"""


def test_account_delete_uses_the_column_the_manifest_declared(client, mk, tmp_path, monkeypatch):
    """插件声明 `accounts_ledger_field = "platform"` → 按账号删单必须按 **platform** 那一列删。

    改之前 `ledger_field` 只被当成「有没有声明」的开关用，真正查的列在下游写死成
    `platform_account`。声明成别的列的插件会顺利通过校验，然后删掉**另一列**同名的行——
    半截抽象比没抽象更危险，因为它看起来是通用的。

    造数刻意把两列的值交叉写：按对的列删会命中 A，按写死的列删会命中 B。
    """
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-demo"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(_ACCT_TOML, encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()

    a = mk("/api/orders", {"date": "2026-05-01", "title": "按 platform 命中",
                           "platform": "甲", "platform_account": "乙", "price_cny": 1})
    b = mk("/api/orders", {"date": "2026-05-01", "title": "按 platform_account 命中",
                           "platform": "乙", "platform_account": "甲", "price_cny": 1})

    r = client.delete("/api/plugins/demo/account/orders", params={"account": "甲"})
    assert r.status_code == 200, r.text

    alive = {o["id"] for o in client.get("/api/orders", params={"limit": 200}).json()["items"]}
    assert a["id"] not in alive, "声明的列是 platform，platform=甲 的那单没被删"
    assert b["id"] in alive, "删的是写死的 platform_account 那一列，清单声明没生效"


def test_dependency_probe_uses_distribution_names_not_import_names(tmp_path):
    """依赖探测按 requirements.txt 里的**发行包名**判，不去猜 import 名。

    `beautifulsoup4` 要 `import bs4`、`Pillow` 要 `import PIL`、`PyYAML` 要 `import yaml`。
    按「名字里的 - 换成 _」当模块名去 import 的话，这类包**装好了也永远报「缺依赖」**：
    插件页一直显示未就绪，点安装又装不出变化，等于这个插件是块砖。

    用 pytest 自己（一定装了、且 import 名与包名恰好一致的对照）+ 一个肯定不存在的包
    来验证探测的方向是对的，再用一个 import 名不同的真包验证关键场景。
    """
    import sys

    from app.routers import plugins as mod

    py = Path(sys.executable)
    # 对照组：装了的不该被报缺；没装的必须被报缺。
    assert mod._missing_dists(py, ["pytest"]) == []
    assert mod._missing_dists(py, ["definitely-not-a-real-package-xyz"]) == \
        ["definitely-not-a-real-package-xyz"]
    # 关键场景：这几个都**装着**，但 import 名和发行包名对不上
    # （PyYAML→yaml、PyMySQL→pymysql、MarkupSafe→markupsafe）。
    # 旧规则会把它们统统报成「缺依赖」，插件因此永远装不完。
    for dist in ("PyYAML", "PyMySQL", "MarkupSafe"):
        assert mod._missing_dists(py, [dist]) == [], f"{dist} 装着却被报成缺依赖"

    req = tmp_path / "requirements.txt"
    req.write_text("SQLAlchemy>=2.0\n# 注释\n\n-e .\nhttpx[http2]\n", encoding="utf-8")
    assert mod._declared_dists(req) == ["SQLAlchemy", "httpx"], "解析出的应是发行包名，不是 import 名"


def test_each_account_gets_its_own_token(client, session, tmp_path, monkeypatch):
    """多账号扇出必须**一个子进程一枚令牌**。

    共用一枚的话，先跑完的那个账号在 `_reap` 里 `revoke(jti)`，还在跑的兄弟当场全部 401——
    它们已经抓到的订单再也回灌不进来，而且是静默的：用户只看到少了几单。
    """
    from app.plugins import scopes
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-demo"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(_ACCT_TOML + '\nscopes = ["staging:write"]\n', encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_python", lambda m: Path(sys.executable))
    mod._needs_cache.clear()

    seen = []
    monkeypatch.setattr(mod, "_launch",
                        lambda *a, **kw: seen.append((kw.get("token"), kw.get("jti"))) or 1234)

    for name in ("甲号", "乙号"):
        assert client.post("/api/plugins/demo/account", params={"name": name}).status_code == 200
    client.put("/api/plugins/demo/grants", json={"granted": ["staging:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})

    r = client.post("/api/plugins/demo/run/fetch")
    assert r.status_code == 200, r.text
    assert len(seen) == 2, f"两个账号应该起两个进程，实际 {len(seen)}"
    jtis = [j for _, j in seen]
    assert len(set(jtis)) == 2, "两个账号共用了同一枚令牌"
    # 收割掉第一个（模拟它先跑完）——第二个必须还活着
    scopes.revoke(jtis[0])
    assert not scopes.alive(jtis[0]) and scopes.alive(jtis[1]), \
        "先跑完的账号把还在跑的兄弟的令牌一起吊销了"


def test_command_needs_are_exposed_so_the_panel_can_re_enable_buttons(client, session, fake_plugin):
    """命令要带上 `needs`：前端勾完授权得就地重算 blocked，否则按钮停在禁用态。"""
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    cmd = got["demo"]["commands"][0]
    assert "needs" in cmd, "命令没给 needs，前端无法在勾授权后重算 blocked"
    assert set(cmd["blocked"]) <= set(cmd["needs"])


def test_button_disabled_state_uses_the_same_set_as_the_execution_gate(client, session, tmp_path, monkeypatch):
    """按钮的「缺权限」判据必须与实际执行闸同源。

    命令声明了一个**没在顶层 scopes 里声明**的 need 时：执行闸用
    token_scopes（声明 ∩ 授权 ∩ 已知）算得它缺，按钮也必须显示缺。
    若按钮用裸 granted 判，勾上授权后按钮会变可点，点下去照收 409——
    更糟的是反过来：勾选框只列 declared，那一项根本无从勾，按钮却永远灰着。
    """
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-demo"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('''
id = "demo"
name = "演示插件"
python = "inherit"
entry = "-m demo"
scopes = ["fx:write"]

[[commands]]
name = "run"
label = "跑一下"
needs = ["fx:write", "orders:read"]
''', encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()

    # 把 orders:read 也「授权」上——它没在清单顶层声明，token_scopes 会滤掉
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write", "orders:read"]})
    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    cmd = next(c for c in got["demo"]["commands"] if c["name"] == "run")
    assert "orders:read" in cmd["blocked"], \
        "按钮判据用了裸 granted：显示可点，实际执行闸会 409"


def _sched_plugin(tmp_path, monkeypatch, toml_body):
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-demo"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(toml_body, encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_python", lambda m: Path(sys.executable))
    mod._needs_cache.clear()
    return mod


def test_scheduler_takes_the_verb_from_the_manifest(client, session, tmp_path, monkeypatch):
    """定时跑哪条命令由**清单**决定，不是核心里写死的 "fetch"。

    写死的话，没声明 fetch 的插件会被一遍遍拉起来跑一个它不认识的动词，
    而 `launched` 只数「进程起没起来」→ last_run_at 照常推进，
    每个周期白跑一次，卡片上还显示「已触发」。
    """
    from app.models import PluginConfig

    mod = _sched_plugin(tmp_path, monkeypatch, '''
id = "demo"
name = "演示插件"
python = "inherit"
entry = "-m demo"
scopes = ["fx:write"]

[[commands]]
name = "refresh"
label = "刷新"
primary = true
needs = ["fx:write"]
''')
    seen = []
    monkeypatch.setattr(mod, "_launch", lambda m, command, *a, **kw: seen.append(command) or 1)

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 1})
    row = session.get(PluginConfig, "demo")
    row.last_run_at = None
    session.add(row)
    session.commit()

    mod._run_due(session)
    assert seen == ["refresh"], f"定时跑的动词是 {seen}，没按清单声明来"


def test_scheduler_skips_when_permissions_are_missing(client, session, tmp_path, monkeypatch):
    """缺权限就别起进程，也别推进 last_run_at。

    不拦的话子进程跑起来收一串 403，用户在卡片上只看到「失败」，
    看不出是**没勾授权**——而且 last_run_at 一直在走，看起来像在正常跑。
    """
    from app.models import PluginConfig

    mod = _sched_plugin(tmp_path, monkeypatch, '''
id = "demo"
name = "演示插件"
python = "inherit"
entry = "-m demo"
scopes = ["fx:write"]

[[commands]]
name = "fetch"
label = "抓取"
needs = ["fx:write"]
''')
    seen = []
    monkeypatch.setattr(mod, "_launch", lambda m, command, *a, **kw: seen.append(command) or 1)

    client.put("/api/plugins/demo/grants", json={"granted": []})     # 明确不授权
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 1})
    row = session.get(PluginConfig, "demo")
    row.last_run_at = None
    session.add(row)
    session.commit()

    mod._run_due(session)
    assert seen == [], "缺权限还是把子进程拉起来了"
    session.expire_all()
    assert session.get(PluginConfig, "demo").last_run_at is None, \
        "没真的跑却推进了 last_run_at，看起来像在正常跑"


def test_broken_toml_still_shows_up_with_its_error(client, tmp_path, monkeypatch):
    """`plugin.toml` 语法写坏 → 插件**不能**从列表里消失。

    静默 continue 的后果有三层：插件凭空不见（零日志）；它的库内配置被当成孤儿、
    卡片上写「插件目录已不在」而目录还在；最要命的是 `forget_plugin` 的护栏正是
    「discover() 里还有没有它」——于是一个只是打错了个引号的插件，
    它的授权和账号可以被一键清掉。
    """
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-brokenx"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text('id = "brokenx"\nname = "坏的\n', encoding="utf-8")  # 引号没闭合
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()

    got = {p["id"]: p for p in client.get("/api/plugins").json()}
    assert "brokenx" in got, "清单写坏的插件从列表里消失了"
    assert "plugin.toml" in got["brokenx"]["manifest_error"]

    # 护栏：还装着的插件不许清配置，哪怕它的清单是坏的
    client.put("/api/plugins/brokenx/config", json={"enabled": False, "schedule_minutes": 0})
    r = client.delete("/api/plugins/brokenx/config")
    assert r.status_code == 409, "清单坏掉就绕过了「还装着不许清」的护栏"


def test_forgetting_a_plugin_also_wipes_its_private_storage(client, tmp_path, monkeypatch):
    """「清理残留配置」必须连**私有存储**一起删。

    `pluginrecord` 原先没有任何删除入口。卸载插件之后那些行会永久留着，
    而以后往 plugins/ 里放一个**同 id** 的插件（别人写的、或被改过的版本），
    它一上来就能读到前一个插件攒下的全部私有数据——用户从没批准过，界面上也看不见。
    """
    from sqlmodel import Session, select

    from app.database import get_engine
    from app.models import PluginRecord
    from app.routers import plugins as mod

    with Session(get_engine()) as s:
        for k in ("a", "b"):
            s.add(PluginRecord(plugin_id="ghostp", kind="note", key=k, data="{}"))
        s.commit()

    # 目录里没有这个插件 → 允许清理
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()

    r = client.delete("/api/plugins/ghostp/config")
    assert r.status_code == 200, r.text
    assert r.json()["records_removed"] == 2

    with Session(get_engine()) as s:
        left = s.exec(select(PluginRecord).where(PluginRecord.plugin_id == "ghostp")).all()
    assert left == [], f"私有存储没删干净，同 id 的新插件会静默继承：{left}"


def test_token_from_the_real_run_path_can_open_the_doors_the_plugin_needs(
        client, fake_plugin, monkeypatch):
    """**端到端**：走 `run_command` 真实签发的那枚令牌，去敲插件真正要敲的门。

    为什么必须这么测：`tests/test_plugin_paths.py::_plugin_client` 把最终 scope 集合
    **当参数直接传给** `scopes.issue()`，于是它绕过了链路中段的推导——
        run_command → token_scopes(清单 ∩ 授权) → & cmd.needs → issue()
    而「令牌按 needs 收窄」这个回归恰恰发生在推导里：所有守卫照旧全绿，
    真实插件却连 `/api/plugins/contract`（自我投影的地基）都进不去。

    这条测试守的是「用户在界面上做完全套操作之后，插件**实际**能干什么」。
    """
    from app.routers import plugins as mod

    captured = {}

    def fake_launch(manifest, command, extra, token=None, config=None, jti=None, on_done=None):
        captured["token"] = token
        return 4242

    monkeypatch.setattr(mod, "_launch", fake_launch)

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    assert client.post("/api/plugins/demo/run/run").status_code == 200
    tok = captured.get("token")
    assert tok, "run_command 没把令牌交给 _launch"

    h = {"Authorization": f"Bearer {tok}"}
    # 1) 自我投影的地基：任何插件都必须进得去（baseline）
    assert client.get("/api/plugins/contract", headers=h).status_code == 200, \
        "真实签发的令牌读不到 ingest 契约——插件自我投影这个设计跑不起来"
    # 2) 状态机规则：淘宝插件靠它同步已导入订单的状态
    assert client.get("/api/meta/status-rules", headers=h).status_code == 200
    # 3) 声明过、也授权了的那扇门开着
    assert client.post("/api/plugins/ingest", headers=h, json={
        "kind": "fx.rate", "items": [{"rate": "21.25", "source": "boc"}]}).status_code == 200
    # 4) 没声明的门仍然关着——收窄本身不能被 baseline 顺手放开
    assert client.get("/api/fx", headers=h).status_code == 403, \
        "命令没声明 fx:read，令牌却能读汇率"


# --- 权限计数：分子分母必须取自同一个集合 --------------------------------------

def test_scope_ratio_counts_only_what_the_user_can_tick(client, fake_plugin):
    """「已授权 X / 声明 Y」的 X **不能**把 baseline 算进去。

    卡片上的分子曾经用的是 `scopes.effective`（含 baseline），分母是 `declared`。
    baseline 不在 declared 里、勾选框里也没有它，于是一个**一项都没勾**的插件
    显示成「1/1」——用户唯一能得出的结论是「权限已经全给了」。
    用户自己看出了不对（「是不是有一个默认权限一直批准」），那正是这条比值在说假话。
    """
    def ratio():
        sc = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["scopes"]
        # 前端 grantedCount() 的口径：分子分母都取自 declared
        return len(set(sc["declared"]) & set(sc["granted"])), len(sc["declared"]), sc

    # 授权状态是库里存着的，别的用例可能已经勾过——显式清成已知状态再断言。
    client.put("/api/plugins/demo/grants", json={"granted": []})
    num, den, sc = ratio()
    assert den == 1, "夹具插件的声明数变了，下面的数字断言会失去意义"
    assert num == 0, "一项都没勾，比值的分子却不是 0"
    assert set(sc["effective"]) - set(sc["declared"]), \
        "effective 里没有 baseline，这条用例测不到它想测的东西"

    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    assert ratio()[:2] == (1, 1), "勾满之后应当是 1/1"


def test_baseline_scopes_are_shown_not_hidden(client, fake_plugin):
    """baseline 不进比值，但**必须在界面上说出来**。

    只把它从分子里减掉会走到另一个极端：用户看到「已授权 0 / 声明 1」会问
    「那它现在到底能干什么」，而勾选框里找不到答案。所以单列一行「默认持有」。
    """
    sc = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["scopes"]
    assert sc["baseline"], "卡片拿不到 baseline 的展示信息，那一行渲染不出来"
    keys = {b["key"] for b in sc["baseline"]}
    assert keys == set(sc["effective"]) - set(sc["declared"]), \
        "baseline 清单与令牌里多出来的那些对不上——界面会漏说或多说"
    for b in sc["baseline"]:
        assert b["label"] and b["hint"], "默认持有的权限没有说明，等于只报了个 key"
    assert not (keys & {c["key"] for c in sc["catalog"]}), \
        "baseline 同时出现在勾选表里——用户会以为它可以取消，而勾了也没用"


# --- 「执行中」必须收尾 ---------------------------------------------------------

def test_stale_running_is_reclaimed_on_startup(session):
    """进程重启后，库里遗留的「执行中」必须被收掉。

    `last_outcome` 是跨进程持久化的，而唯一会把它从 running 改走的是收割线程
    ——daemon 线程，主进程一没它就没了。于是「插件还在跑时关掉 soroban」
    会在库里留下一个**永久**的 running：卡片顶着黄色「执行中」，
    而没有任何进程会再来改它，刷新和重启都没用。
    """
    from app.routers.plugins import reclaim_stale_runs

    session.add(PluginConfig(plugin_id="stale-demo", last_outcome="running",
                             last_summary="抓取 执行中…", last_finished_at=None))
    session.commit()
    try:
        assert reclaim_stale_runs() >= 1
        session.expire_all()
        cfg = session.get(PluginConfig, "stale-demo")
        assert cfg.last_outcome == "failed", "遗留的「执行中」没被收掉，卡片会一直挂着"
        assert cfg.last_finished_at is not None, "没有结束时间，界面上仍显示成在跑"
        assert "重跑" in cfg.last_summary, "只改了状态却没告诉用户该怎么办"
    finally:
        session.delete(session.get(PluginConfig, "stale-demo"))
        session.commit()


def test_reclaim_leaves_finished_rows_alone(session):
    """只收 running。把 ok/failed 也一起改掉的话，重启一次就把上一次真实的
    成功结果抹成失败——那比不收还糟。"""
    from app.routers.plugins import reclaim_stale_runs

    session.add(PluginConfig(plugin_id="done-demo", last_outcome="ok",
                             last_summary="抓取：本次 ✓ 新增 3 单"))
    session.commit()
    try:
        reclaim_stale_runs()
        session.expire_all()
        cfg = session.get(PluginConfig, "done-demo")
        assert cfg.last_outcome == "ok" and "新增 3 单" in cfg.last_summary
    finally:
        session.delete(session.get(PluginConfig, "done-demo"))
        session.commit()


def test_frontend_scope_ratio_never_mixes_two_sets():
    """前端那条比值不许再拿 `scopes.effective` 当分子。

    这是本项目最典型的一类 bug——不是算错，是**分子分母取自两个集合**：
    effective 含 baseline、declared 不含，于是一项没勾也显示「1/1」。
    这种数字没有任何一种读法是对的，而它在界面上完全看不出异常。
    """
    import re

    src = (_REPO / "frontend" / "src" / "views" / "Plugins" / "index.vue").read_text(encoding="utf-8")
    tpl = src.split("<script", 1)[0]
    bad = re.findall(r"scopes\.effective[^\n]*\.length", tpl)
    assert not bad, f"模板又把 effective 当计数用了：{bad}——分子请用 grantedCount()"
    assert "grantedCount(p)" in tpl, \
        "比值的分子不是 grantedCount()，这条守卫已经守不住它本来要守的东西"


# --- inherit 类插件（与 soroban 共用环境）--------------------------------------

_INHERIT_TOML = _FAKE_TOML.replace('id = "demo"', 'id = "inh"').replace(
    'entry = "-m demo"', 'entry = "-m inh_mod"')


@pytest.fixture()
def inherit_plugin(tmp_path, monkeypatch):
    from app.routers import plugins as mod

    d = tmp_path / "plugins" / "soroban-plugin-inh"
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(_INHERIT_TOML, encoding="utf-8")
    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setattr(mod, "_SOROBAN_ROOT", tmp_path)
    mod._needs_cache.clear()
    return d


def test_inherit_uses_sorobans_own_interpreter_not_a_system_one(monkeypatch):
    """`python = "inherit"` 只能是 `sys.executable`，不许借道 `_base_python()`。

    `_base_python()` 找的是「能用来**建 venv** 的系统 Python」。冻结态下它返回
    PATH 里的 python3——而 inherit 的字面意思是「继承 soroban 的环境」，
    系统 python3 里没有 httpx。走通了是 ModuleNotFoundError，
    走不通（机器没装 python）则回落到 exe 自己，每跑一次插件就把 soroban 再启动一遍。
    """
    from app.routers import plugins as mod

    monkeypatch.setattr(mod, "_base_python", lambda: "/usr/bin/python3-not-ours")
    m = {"python": "inherit", "_dir": Path("/tmp/x"), "entry": "-m x"}
    assert str(mod._python(m)) == sys.executable


def test_packaged_inherit_plugin_goes_through_the_exe_verb(monkeypatch):
    """冻结态下 inherit 插件必须走 `--run-plugin`，而不是 `exe -m 模块`。

    PyInstaller 的 bootloader **不解释 `-m`**：那条命令的实际效果是
    把 soroban 又启动一遍（建 .env、连库跑迁移、卡在端口占用），
    而用户在卡片上看到的失败原因与汇率毫无关系。
    """
    from app.routers import plugins as mod

    m = {"python": "inherit", "_dir": Path("/tmp/plug"), "entry": "-m soroban_fx"}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    got = mod._plugin_argv(m, "fetch", ["--soroban-url", "http://x"])
    assert got == [sys.executable, "--run-plugin", "/tmp/plug", "-m", "soroban_fx",
                   "fetch", "--soroban-url", "http://x"]

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert mod._plugin_argv(m, "fetch", []) == [sys.executable, "-m", "soroban_fx", "fetch"], \
        "源码态不该多绕一层——那时 sys.executable 就是一个真的 python"


def test_normal_plugin_argv_is_unchanged(monkeypatch):
    """独立 venv 的插件不受影响：拼法必须与重构前逐字节相同。"""
    from app.routers import plugins as mod

    m = {"python": ".venv/bin/python", "_dir": Path("/tmp/plug"), "entry": "-m taobao_scraper"}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert mod._plugin_argv(m, "login", ["--account", "a"]) == [
        "/tmp/plug/.venv/bin/python", "-m", "taobao_scraper", "login", "--account", "a"]


def test_inherit_plugin_never_asks_to_build_a_venv(client, inherit_plugin):
    """inherit 插件没有 venv 可建，就不该报「缺 Python 环境」。

    报了的话用户会去点「一键安装」，而那会在插件目录里建一个**永远不会被用到**的
    .venv——一个有进度条、有成功提示、却什么也没改变的按钮。
    """
    got = {p["id"]: p for p in client.get("/api/plugins").json()}["inh"]
    assert "venv" not in {n["key"] for n in got["needs"]}, \
        f"inherit 插件被要求建 venv：{got['needs']}"
    r = client.post("/api/plugins/inh/install")
    assert r.status_code == 409 and "共用运行环境" in r.json()["detail"]


def test_inherit_needs_are_probed_in_process(tmp_path):
    """inherit 插件的依赖在**本进程**里查，且要如实报缺。

    不能 spawn 子进程去问：解释器就是 soroban 自己，冻结态下
    `[soroban.exe, "-c", ...]` 会把 soroban 再启动一遍——而这条路径挂在
    前端**轮询**的 GET /api/plugins 上。
    """
    from app.routers.plugins import _inherit_needs

    assert _inherit_needs(tmp_path) == [], "没有 requirements.txt 时不该凭空报缺"
    (tmp_path / "requirements.txt").write_text(
        "httpx\nthis-package-does-not-exist-anywhere\n", encoding="utf-8")
    got = _inherit_needs(tmp_path)
    assert len(got) == 1 and got[0]["key"] == "deps"
    assert "this-package-does-not-exist-anywhere" in got[0]["hint"]
    assert "httpx" not in got[0]["hint"], "soroban 环境里装着的包被误报成缺失"
    # 提示不能指向「一键安装」——那个按钮对这类插件是 409
    assert "共用运行环境" in got[0]["hint"]


def test_fx_plugin_declares_its_dependency():
    """汇率插件必须有 requirements.txt。

    没有的话依赖探测**整段跳过**，卡片上 installed 恒为 true：
    界面写着「已就绪」，每次运行却 ModuleNotFoundError。
    这是「界面说假话」里代价最大的一种——用户不会去查一个显示正常的东西。

    插件各自成库（`.gitignore` 里 `/plugins/*/`），所以本机可能根本没 checkout 它。
    跳过的口径与 test_consistency.plugin_source 完全一致：**找不到就红**，
    真没有插件仓请显式设 `SOROBAN_NO_PLUGINS=1`——要跳过必须是有人明确表示，
    不能是「目录恰好不在」的副作用（那正是守卫悄悄归零的经典形态）。
    """
    import os

    d = _REPO / "plugins" / "soroban-plugin-fx"
    if not d.is_dir():
        if os.environ.get("SOROBAN_NO_PLUGINS"):
            pytest.skip("显式声明了本机没有插件仓（SOROBAN_NO_PLUGINS=1）")
        raise AssertionError(
            "找不到 plugins/soroban-plugin-fx/。本条守卫钉的是「卡片上的『已就绪』是不是真的」，"
            "不能静默跳过——真没有插件仓请设 SOROBAN_NO_PLUGINS=1。")
    req = d / "requirements.txt"
    assert req.is_file(), "汇率插件没声明依赖 → 卡片上的「已就绪」是假的"
    assert "httpx" in req.read_text(encoding="utf-8")


# --- 打进 exe 的插件：释放到磁盘 -------------------------------------------------

def _fake_bundle(tmp_path, version="1.0.0", body="v1"):
    """伪造 exe 内那份插件（_MEIPASS/plugins/…）。"""
    src = tmp_path / "meipass" / "plugins" / "soroban-plugin-demo"
    src.mkdir(parents=True, exist_ok=True)
    (src / "plugin.toml").write_text(
        f'id = "demo"\nname = "演示"\nversion = "{version}"\nentry = "-m demo"\n', encoding="utf-8")
    (src / "code.py").write_text(body, encoding="utf-8")
    return src


def test_bundled_plugins_are_released_on_first_run(tmp_path, monkeypatch):
    from app.routers import plugins as mod

    _fake_bundle(tmp_path)
    dst = tmp_path / "run" / "plugins"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setattr(mod, "plugin_dir", lambda: dst)

    assert mod.seed_bundled_plugins() == {"soroban-plugin-demo": "new"}
    assert (dst / "soroban-plugin-demo" / "code.py").read_text(encoding="utf-8") == "v1"
    # 幂等：再跑一次不该重复释放（每次启动都覆盖等于每次都可能盖掉用户改的东西）
    assert mod.seed_bundled_plugins() == {}


def test_release_updates_on_version_change_but_keeps_venv_and_session(tmp_path, monkeypatch):
    """换 exe 就该换到新插件；但**绝不能**碰 .venv 与 .state。

    那两样一个是几百 MB 的已装依赖、一个是扫码登录换来的会话。
    带删除逻辑的「同步」会把它们一起清掉，而用户看到的是
    「升级了一下，插件要重新装、还要重新扫码」。
    """
    from app.routers import plugins as mod

    _fake_bundle(tmp_path, version="1.0.0", body="v1")
    dst = tmp_path / "run" / "plugins"
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setattr(mod, "plugin_dir", lambda: dst)
    mod.seed_bundled_plugins()

    live = dst / "soroban-plugin-demo"
    (live / ".venv" / "bin").mkdir(parents=True)
    (live / ".venv" / "bin" / "python").write_text("#!/bin/sh", encoding="utf-8")
    (live / ".state").mkdir()
    (live / ".state" / "a.json").write_text('{"cookie": "…"}', encoding="utf-8")

    _fake_bundle(tmp_path, version="1.1.0", body="v2")
    assert mod.seed_bundled_plugins() == {"soroban-plugin-demo": "updated"}
    assert (live / "code.py").read_text(encoding="utf-8") == "v2", "版本变了却没更新代码"
    assert (live / ".venv" / "bin" / "python").exists(), "把插件已装的依赖删掉了"
    assert (live / ".state" / "a.json").exists(), "把用户的登录会话删掉了"


def test_release_is_a_noop_without_a_bundle(tmp_path, monkeypatch):
    """源码运行没有 _MEIPASS——这条路径必须是彻底的空操作，
    否则开发时每次启动都会去动仓库里的 plugins/。"""
    from app.routers import plugins as mod

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(mod, "plugin_dir", lambda: tmp_path / "nope")
    assert mod.seed_bundled_plugins() == {}
    assert not (tmp_path / "nope").exists(), "源码模式下不该凭空建出插件目录"


# --- 结果的第三档：warn（T4）----------------------------------------------------

@pytest.mark.parametrize("line,ok,want", [
    ('{"created": 3}', True, "ok"),
    ('{"created": 3, "error": "3 个号里 1 个登录过期"}', True, "warn"),
    ('{"error": "全部源都取不到"}', False, "failed"),
    ("", True, "ok"),
])
def test_outcome_has_a_third_state_for_self_reported_errors(line, ok, want):
    """退出码 0 但自报了 error → 第三档 warn，不是绿色的「成功」。

    **退出码是跨进程契约，不改**：淘宝插件的 `already_running` 刻意 return 0，
    那是「这次没什么可做」而不是失败，信 JSON 会把它刷成红色。
    但绿色同样不行——用户看到绿字就不会再点开摘要，而那句话里写着出了什么事。
    """
    from app.routers.plugins import _batch_text, _self_reported_error

    warn = ok and _self_reported_error(line)
    outcome, _ = _batch_text("抓取", [("a", ok, "x", warn)], 1)
    assert outcome == want


def test_warn_does_not_swallow_a_real_failure():
    """一批里既有失败又有警告 → 整批算 failed。
    警告压过失败的话，那个真正需要人处理的号会从卡片上消失。"""
    from app.routers.plugins import _batch_text

    outcome, text = _batch_text("抓取", [("a", True, "部分成功", True),
                                         ("b", False, "登录过期", False)], 2)
    assert outcome == "failed"
    assert "登录过期" in text and "部分成功" in text, "合并摘要吞掉了其中一个号"


# --- 「一直显示执行中」的直接原因：写「执行中」写晚了 ----------------------------

def test_a_command_that_fails_instantly_does_not_get_stuck_on_running(
        client, fake_plugin, monkeypatch, session):
    """子进程**在请求返回之前就结束**时，卡片不能停在「执行中」。

    这是一个跑得越快越必中的竞态：会话过期的插件会在毫秒级打印一行
    「无会话，请先授权登录」就退出。收割线程随即把 failed 写进库，
    而请求接着用 "running" 把它盖掉并提交——批次此刻已经从 `_BATCHES` 里弹掉，
    **再没有任何人会来改它**，卡片永久停在「执行中…」。
    越是「一点就失败」的情形越稳定复现，而那正是用户最想看到失败原因的时候。
    """
    from app.routers import plugins as mod

    def instant_launch(manifest, command, extra, token=None, config=None, jti=None,
                       on_done=None):
        if on_done:                       # 同步回调 = 模拟「进程比请求还快」
            on_done(False, "无会话，请先在 soroban「插件管理」授权登录")
        return 4242

    monkeypatch.setattr(mod, "_launch", instant_launch)
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    assert client.post("/api/plugins/demo/run/run").status_code == 200

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "failed", \
        f"跑完了却还显示 {got['outcome']}——卡片会永久停在「执行中」"
    assert "无会话" in got["summary"], "失败原因被盖掉了，用户看不到该去做什么"
    assert got["at"], "没有结束时间，界面上仍会当成在跑"


def test_when_nothing_could_be_launched_the_card_says_so(
        client, fake_plugin, monkeypatch):
    """一个进程都没起来 → 也要有交代。

    「执行中」现在写在起进程**之前**，所以这条路径上库里已经是 running 了，
    而不会有任何回调到来——不显式收尾就是又一个永久的「执行中」。
    """
    from app.routers import plugins as mod

    def boom(*a, **k):
        raise RuntimeError("起不来")

    monkeypatch.setattr(mod, "_launch", boom)
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    # TestClient 默认把服务端异常原样抛出来；这里只关心「卡片上留下了什么」，
    # 所以吞掉它——请求怎么失败是另一回事，`finally` 里的收尾必须已经跑过。
    with pytest.raises(RuntimeError):
        client.post("/api/plugins/demo/run/run")

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "failed", f"一个都没起来却显示 {got['outcome']}"


def test_a_still_running_command_does_show_running(client, fake_plugin, monkeypatch):
    """反面：真的还在跑时**必须**显示「执行中」。
    上面两条如果用「干脆不写 running」来满足，这一条会红。"""
    from app.routers import plugins as mod

    monkeypatch.setattr(mod, "_launch",
                        lambda *a, **k: 4243)          # 起来了，但永不回调
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    assert client.post("/api/plugins/demo/run/run").status_code == 200

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "running" and not got["at"]


def test_the_card_finishes_when_the_process_ends_after_the_request(
        client, fake_plugin, monkeypatch):
    """**真实时序**：请求先返回，子进程稍后才结束——卡片必须收尾，不能永久「执行中」。

    上面那条 `test_a_fast_failing_plugin_does_not_stay_running` 用的桩是**同步**调
    `on_done`（「进程比请求还快」），那是刻意造的极端。可 `_launch` 是 fire-and-forget：
    Popen 之后由收割线程等进程退出，所以**日常路径恰恰相反**——扇出循环 `finally`
    里的 `seal_and_report` 一定跑在所有回调之前。

    那条路径上曾经是这样断的：`_batch_seal` 发现 `_BATCHES` 里还没有这个批次
    （回调一个都还没到），就 `return total <= 0, []` —— **把 total 整个丢掉**；
    之后回调用 `setdefault` 新建 `{"total": None}`，而 `_batch_text` 拿到
    `total=None` 恒返回 `"running"`。于是每一次抓取都停在「执行中…」，
    前端每 4 秒轮询一次不停，`_BATCHES` 每执行一次泄漏一条，只有重启后端才被
    `reclaim_stale_runs` 刷成 failed（而且写的理由是假的：「soroban 在它跑完之前退出了」）。

    两条测试的差别只有**回调的时机**，而那正是决定成败的那个变量——
    只测同步那一支等于把唯一会出错的顺序排除在外。
    """
    from app.routers import plugins as mod

    pending = []

    def deferred_launch(manifest, command, extra, token=None, config=None, jti=None,
                        on_done=None):
        pending.append(on_done)           # 只登记，不调用 = 进程还在跑
        return 4242

    monkeypatch.setattr(mod, "_launch", deferred_launch)
    # 只盯**本条自己**造出来的批次：同文件别的用例把 _launch 打桩成「永不回调」，
    # 它们的批次本来就该留在表里（进程还在跑）。断言整张表为空是把别人的状态算到自己头上。
    before = set(mod._BATCHES)
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})
    assert client.post("/api/plugins/demo/run/run").status_code == 200

    # 请求已经返回，进程还没结束——此刻显示「执行中」是对的
    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "running", "进程还在跑，这时候就不该是终态"

    for cb in pending:                    # 进程陆续结束
        cb(True, "抓到 3 单")

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "ok", \
        f"进程都跑完了，卡片却还是 {got['outcome']}——永久「执行中」"
    assert got["at"], "没有结束时间，界面上仍会当成在跑"
    leaked = set(mod._BATCHES) - before
    assert not leaked, f"批次跑完了没被清掉，每执行一次泄漏一条：{leaked}"


class _FakeProc:
    """假子进程。**只替换 `subprocess.Popen`，让真的 `_launch` / `_reap` 跑起来。**

    第一版这两条测试是把 `_launch` 整个 monkeypatch 掉、并在桩里**重写了一遍**互斥逻辑
    （算 key → 查 `_INFLIGHT` → 抛 `PluginBusy`）。于是测的是桩不是产品代码：
    把 `_launch` 里的查重删掉、把 `_reap` 里的放键删掉，两条**都不会红**。
    守卫必须打在产品代码上，不能在桩里把被测逻辑再实现一遍。

    `gate` 未 set = 进程还在跑（`wait` 超时）；set 之后 `_reap` 才会走到 finally 放键。
    """

    _pids = itertools.count(9000)

    def __init__(self, gate):
        self.pid = next(self._pids)
        self.stdout = io.StringIO('{"ok": true, "created": 1}\n')
        self.stderr = io.StringIO("")
        self.returncode = None
        self._gate = gate

    def wait(self, timeout=None):
        if not self._gate.wait(timeout):
            raise subprocess.TimeoutExpired("demo", timeout)
        self.returncode = 0
        return 0

    def poll(self):
        # 关停路径会调它。假 Popen 就该把 Popen 的接口补全，
        # 少一个方法的表现是**别的测试文件**报 AttributeError，看不出跟这里有关。
        return self.returncode


def _popen_gate(monkeypatch):
    """把 Popen 换成假的，返回那个「进程什么时候结束」的开关。"""
    from app.routers import plugins as mod

    gate = threading.Event()
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: _FakeProc(gate))
    return gate


def _grant_and_enable(client):
    client.put("/api/plugins/demo/grants", json={"granted": ["fx:write"]})
    client.put("/api/plugins/demo/config", json={"enabled": True, "schedule_minutes": 0})


def test_the_same_account_cannot_be_launched_twice_at_once(
        client, fake_plugin, monkeypatch):
    """同一插件/命令/账号**同时只许有一个子进程**。

    这不是「优化」，是这个项目自己写下的风控红线：并发多开有头浏览器登同一个淘宝账号，
    正是最容易触发平台风控的动作（见 scheduler_loop 的说明与淘宝插件的
    docs/风控与对策.md）。而此前核心一侧一道闸都没有——「授权登录」按钮的 `:disabled`
    只判插件装没装，连点三下就是三个 chromium。

    互斥原先被推给每个插件各自实现，那等于把一条安全边界交给第三方代码去记得；
    而淘宝插件的 `_account_lock` 只包了 fetch，**login 恰恰没包**。
    """
    gate = _popen_gate(monkeypatch)
    _grant_and_enable(client)
    try:
        assert client.post("/api/plugins/demo/run/run").status_code == 200
        second = client.post("/api/plugins/demo/run/run")
        assert second.status_code == 409, \
            f"同一个命令被并发起了两次（{second.status_code}）——连点就是多开浏览器"
        assert "已经在跑" in second.text, second.text
    finally:
        gate.set()          # 放收割线程走，别把它留在测试会话里干等


def test_the_mutex_key_is_released_when_the_process_ends(
        client, fake_plugin, monkeypatch):
    """进程结束后互斥键必须放掉——否则这个账号**永久**再也起不来。

    这一条是上一条的反面。只加闸不放闸的话，功能测试全绿（第一次照样能跑），
    用户却在第二次点击时永远撞 409，而且重启才能恢复。
    """
    from app.routers import plugins as mod

    gate = _popen_gate(monkeypatch)
    _grant_and_enable(client)
    assert client.post("/api/plugins/demo/run/run").status_code == 200

    gate.set()                                  # 进程结束 → 真的 _reap 走 finally 放键
    for _ in range(200):
        if not mod._INFLIGHT:
            break
        time.sleep(0.02)
    assert not mod._INFLIGHT, "进程结束了，互斥键还占着"

    assert client.post("/api/plugins/demo/run/run").status_code == 200, \
        "进程结束后互斥键没放掉——这个账号再也起不来了"
    gate.set()


def test_launch_itself_refuses_a_duplicate_even_when_the_route_check_passed(
        client, fake_plugin, monkeypatch):
    """`_launch` 自己那道闸必须独立成立——它才是**防并发**的那一道。

    路由里还有一道预筛（「已经在跑的目标先摘掉，全在跑就 409」），但那只是为了给用户
    一句人话。两个请求并发时会**双双通过预筛**，然后双双走到 `_launch`——
    真正阻止「同时起两个 chromium」的是 `_launch` 里「查重与登记在同一把锁内」那三行。

    这条测试是直接打在 `_launch` 上的，因为通过路由根本够不着它：
    顺序发两个请求时预筛先返回 409，`_launch` 的第二次调用压根不会发生。
    （删掉 `_launch` 里的查重、只留预筛，整套测试曾经全绿——那正是这条补上的原因。）
    """
    from app.routers import plugins as mod

    gate = _popen_gate(monkeypatch)
    m = mod._find_manifest("demo")
    extra = ["--account", "甲"]
    try:
        assert mod._launch(m, "run", extra) > 0
        with pytest.raises(mod.PluginBusy):
            mod._launch(m, "run", extra)
        # 换个账号不受影响：互斥键是「插件/命令 [账号]」，不是整个插件
        assert mod._launch(m, "run", ["--account", "乙"]) > 0
    finally:
        gate.set()


def test_a_busy_account_does_not_take_down_its_siblings(client, fake_plugin, monkeypatch):
    """扇出时某个账号被互斥挡下 → **只跳过它**，兄弟照常起；令牌当场作废。

    循环里没有 per-account 的 try 的话，一个 `PluginBusy` 会掀掉整个扇出——
    「甲还在跑」变成「乙丙也别想跑」。而令牌是**为那个没起来的进程签的**，
    没人会去 revoke 它，每被挡一次就泄漏一枚能用二十几分钟的凭据。
    """
    from app.routers import plugins as mod

    gate = _popen_gate(monkeypatch)
    m = mod._find_manifest("demo")
    revoked = []
    monkeypatch.setattr(mod.scopes, "revoke", lambda jti: revoked.append(jti))
    try:
        mod._launch(m, "run", [])                 # 先把「无账号」这个键占住
        _grant_and_enable(client)
        r = client.post("/api/plugins/demo/run/run")
        assert r.status_code == 409, r.text       # 只有一个目标，全被挡 ⇒ 409
        # 预筛在签发令牌**之前**就挡下了，所以这条路径上一枚令牌都不该签出去
        assert not revoked, f"被挡下还签了令牌并且泄漏了：{revoked}"
    finally:
        gate.set()


def test_a_failed_spawn_releases_the_key_instead_of_wedging_the_account(
        client, fake_plugin, monkeypatch):
    """`Popen` 抛错时必须把刚登记的互斥键放掉，否则这个账号**永久**起不来。

    登记要在 Popen **之前**（否则两个并发请求会双双查到「没人在跑」再双双起进程），
    代价就是：起失败的那条路径必须自己把键收回来。漏了不会有任何报错——
    第一次点击如实报 500，之后每一次都撞 409「已经在跑」，而其实一个进程都没有，
    只有重启后端才能恢复。功能测试一条都不会红。
    """
    from app.routers import plugins as mod

    boom = {"on": True}

    def popen(*a, **k):
        if boom["on"]:
            raise OSError("模拟 fork 失败")
        return _FakeProc(threading.Event())

    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    _grant_and_enable(client)
    assert client.post("/api/plugins/demo/run/run").status_code == 500
    assert not mod._INFLIGHT, f"进程没起来，键却占着：{mod._INFLIGHT}"

    boom["on"] = False                      # 环境恢复了，应当能重新跑
    assert client.post("/api/plugins/demo/run/run").status_code == 200, \
        "启动失败一次之后这个账号再也起不来了"


# 基名逐个不同：PluginConfig 跨用例存活，两轮共用一个名字的话第二轮建首个账号就 409，
# 红在「前提没成立」而不是「守卫没生效」——看起来一样，含义完全不同。
@pytest.mark.parametrize("first,second", [("abc", "ABC"), ("xyz", "xYz")])
def test_account_names_that_differ_only_in_case_are_refused(client, fake_plugin, first, second):
    """只有大小写不同的账号名必须挡掉——它们在 Windows/macOS 上**共用同一份会话文件**。

    账号名同时是 `<state_dir>/<账号>.json` 的文件名，而 NTFS / APFS 大小写不敏感。
    加得进去的话，两个账号共用一份 cookie：给其中一个注销，`delete_account` 的 unlink
    删掉的是**另一个的真身会话**，而返回的 `removed_session: true` 看着完全正常，
    用户只会发现「另一个账号莫名其妙掉登录了」。

    这条闸在 Linux 上永远不会自己触发（ext4 区分大小写），所以只能靠守卫钉住。
    """
    assert client.post("/api/plugins/demo/account", params={"name": first}).status_code == 200
    r = client.post("/api/plugins/demo/account", params={"name": second})
    assert r.status_code == 409, f"{second} 被当成了新账号——它和 {first} 会共用一份会话文件"


def test_the_ledger_column_keeps_its_case_sensitivity(client, fake_plugin):
    """反面：**账本那一列的大小写敏感是刻意的**，不许被这条闸顺手折掉。

    迁移 f2a3b4c5d6e7 专门把 `platform_account` 改成 `utf8mb4_0900_bin`，
    理由就是不该把 Alice 和 alice 当成同一个人。两件事的约束来自不同的层：
    账号名受**文件系统**约束，账本列受**业务语义**约束——
    把它们一起折叠，就是拿一个理由去改另一件事。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "app" / "routers" / "plugins.py"
    body = src.read_text(encoding="utf-8")
    i = body.index("def rename_account")
    seg = body[i:body.index("\n@router", i)]
    assert "tag_value_in_use(session, field, new)" in seg, \
        "账本占用检查被改动了——它必须保持逐字节比较"
    assert "casefold" not in seg.split("tag_value_in_use(session, field, new)")[1][:200], \
        "账本那一半也被折叠了大小写"


@pytest.mark.parametrize("toml,expect", [
    # ① scope 名拼错：勾上后被 `& set(SCOPES)` 丢掉 → 「需要新授权」的黄标永远消不掉，
    #    而前端还会弹「已授予「fx:wirte」」——一句明确的假话。
    ('id = "demo"\npython = "inherit"\nentry = "-m demo"\nscopes = ["fx:wirte"]\n'
     '[[commands]]\nname = "run"\nlabel = "跑"\nneeds = ["fx:wirte"]\n', "不认识"),
    # ② 命令要了清单没声明的权限：blocked 恒含它，而权限区只按 declared 渲染——
    #    悬停提示「先在下面勾选授权」指向一个**根本不存在**的勾选框。
    ('id = "demo"\npython = "inherit"\nentry = "-m demo"\nscopes = ["fx:write"]\n'
     '[[commands]]\nname = "run"\nlabel = "跑"\nneeds = ["fx:write", "staging:read"]\n', "没声明"),
])
def test_a_broken_scope_declaration_shows_up_as_a_manifest_error(
        client, fake_plugin, toml, expect):
    """清单里的 scope 写错，必须变成**界面上看得见**的清单错误。

    两种写错的表现一模一样，而且都让用户**无路可解**：按钮永远灰着、
    「需要新授权」永远消不掉，而没有任何一处提示说得出为什么。用户能做的只有猜。

    仓库里那两个 plugin.toml 都写对了，所以这个洞至今没炸——那是运气不是防护。
    而 `conftest.py` 把 PLUGIN_DIR 指向空目录，**没有任何测试解析真实 plugin.toml**：
    把 `fx:write` 敲错一个字母，近千条测试照样全绿。
    """
    (fake_plugin / "plugin.toml").write_text(toml, encoding="utf-8")
    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]
    assert got["manifest_error"], "清单里的 scope 写错了，界面上却一点痕迹都没有"
    assert expect in got["manifest_error"], got["manifest_error"]


def test_the_real_plugin_manifests_in_this_repo_pass_the_scope_lint():
    """仓库里**真实的** plugin.toml 必须过 lint。

    上面两条用的是造出来的坏清单（测「坏的能不能被发现」）；这一条测「好的还是不是好的」。
    没有它，把 `fx:write` 在真清单里敲错一个字母，整套测试依然全绿——
    因为其它测试全都指着一个空的插件目录。
    """
    import tomllib

    from app.plugins import manifest as pm
    from app.routers.plugins import _scope_lint

    root = Path(__file__).resolve().parents[2] / "plugins"
    tomls = sorted(root.glob("soroban-plugin-*/plugin.toml")) if root.is_dir() else []
    if not tomls:
        import os
        if os.environ.get("SOROBAN_NO_PLUGINS"):
            pytest.skip("显式声明了本机没有插件仓（SOROBAN_NO_PLUGINS=1）")
        raise AssertionError(
            "找不到任何真实 plugin.toml。这条守卫钉的正是「真清单没人解析过」，"
            "不能静默跳过——真没有插件仓请设 SOROBAN_NO_PLUGINS=1。")
    for f in tomls:
        mf = pm.parse(tomllib.loads(f.read_text(encoding="utf-8")), f.parent)
        assert not _scope_lint(mf), f"{f.parent.name} 的 plugin.toml：{_scope_lint(mf)}"


def test_the_card_says_so_when_the_core_rejected_everything(
        client, fake_plugin, monkeypatch):
    """插件自报成功、而核心逐条拒收时，卡片**不许**显示成绿色的成功。

    这是最贵的一种「界面说假话」：插件推 30 条、核心一条没写、插件不看回执照常
    `print({"ok": true, "created": 30})` 并 `exit 0` ⇒ 卡片绿字「已导入 30 单」，
    而库里零写入。用户看到绿字就不会再点开摘要，唯一能发现的途径是翻后端日志。

    拒收信息其实一直返回给插件了（`/api/ingest` 的 summary + 逐项 results），
    核心也一直记着日志——问题在于**卡片显示的是插件自报的那句话**。
    仓库里那个汇率插件被改成了会判回执，但那是**插件的自觉、不是核心的强制**：
    换一个第三方插件，或者哪天有人重写它时忘了那段，同一个洞立刻复发。

    修法是让核心自己记一笔（`plugins/runlog`），收尾时并进文案并压低颜色，
    **插件说什么都盖不掉它**。
    """
    from app.plugins import runlog
    from app.routers import plugins as mod

    runlog.note_rejected("JTI-1", "demo", "汇率", 30, "缺少必填字段 rate")
    done = mod._result_writer("demo", "抓取", run="JTI-1")
    done(True, "已导入 30 单")                    # 插件自报：成功

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] != "ok", \
        "插件自报成功就显示成功——而核心一条都没写进去"
    assert "核心拒收 30 条" in got["summary"], got["summary"]
    assert "缺少必填字段 rate" in got["summary"], "没说清第一条为什么被拒"


def test_a_clean_run_is_not_downgraded(client, fake_plugin):
    """反面：核心没拒收任何东西时，绿色还是绿色。

    没有这一条，「凡是收尾都降一档」也能让上面那条绿，而那会让每一次正常的抓取
    都显示成黄色的警告——警告一旦变成常态就没人看了。
    """
    from app.routers import plugins as mod

    done = mod._result_writer("demo", "抓取", run="JTI-CLEAN")
    done(True, "已导入 5 单")
    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "ok", got
    assert "核心拒收" not in got["summary"], got["summary"]


def test_a_core_fact_survives_the_running_phase(client, fake_plugin):
    """批次还在跑的那一刻并不写进文案，但那条事实**不许因此丢掉**。

    这里有两件事必须分开看，第一版把它们混在一起而写错过：
      · **用**（并进卡片文案）只能在终态做——running 时写进去会被下一次回调整个盖掉；
      · **取**（从 runlog 拿出来）必须在**每个账号收尾时立刻**做，因为
        「一个子进程一枚令牌」意味着 runlog 是按账号分开记的，
        封口那一次只知道自己那枚 jti。晚取 = 先跑完的账号那条永远没人取。
    所以取出来之后先累进批次，封口时再一次性并进文案。
    """
    from app.plugins import runlog
    from app.routers import plugins as mod

    _grant_and_enable(client)
    runlog.reset()
    runlog.note_rejected("JTI-2", "demo", "汇率", 7, "格式不对")
    mod._batch_seal("B-2", 2)                     # 两个子进程
    mod._result_writer("demo", "抓取", who="甲", batch="B-2", run="JTI-2")(True, "ok")

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] == "running", "批次没跑完就写终态了"
    assert "核心拒收" not in (got["summary"] or ""), \
        "running 阶段就把核心事实写进文案了——会被下一次回调盖掉"

    mod._result_writer("demo", "抓取", who="乙", batch="B-2", run="JTI-乙-无")(True, "ok")
    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert "核心拒收 7 条" in got["summary"], \
        f"甲的拒收在封口时丢了：{got['summary']}"
    runlog.reset()


def test_runlog_drops_the_oldest_entry_not_the_newest():
    """表满时丢**最早**的一条，不是最新的。

    第一版用的是 `dict.popitem()`——它在 Python 3.7+ 是 **LIFO**，丢的是刚刚插进来的那条，
    也就是「正在跑、马上要在卡片上报出来」的那一次，恰恰是最要紧的。
    而当时注释和日志都写着「丢弃最早的」：**方向和说法双双错**，
    而且从代码上看不出来——`popitem` 读起来完全像是「弹掉一个」。

    这条守卫只能靠真跑：读代码时 `popitem()` 与 `next(iter())` 都很像「取一个」。
    """
    from app.plugins import runlog

    orig = runlog._MAX_RUNS
    runlog.reset()
    try:
        runlog._MAX_RUNS = 3
        for i in range(3):
            runlog.note_rejected(f"run{i}", "demo", "汇率", 1, "x")
        runlog.note_rejected("run-new", "demo", "汇率", 1, "x")

        assert runlog.peek("run-new"), "刚发生的那条被丢了——它才是马上要报到卡片上的"
        assert runlog.peek("run0") is None, "该丢最早的那条"
        assert runlog.peek("run1") and runlog.peek("run2"), "丢多了"
    finally:
        runlog._MAX_RUNS = orig
        runlog.reset()


def test_rejections_from_every_account_in_a_fanout_are_reported(client, fake_plugin):
    """扇出时**每个账号**的核心拒收都要报出来，不能只报最后收尾那个。

    这是「一个子进程一枚令牌」这条设计（见 run_command 的注释）带来的连带后果：
    runlog 按 run（jti）聚合，而 jti 是**每账号各一枚**。
    于是「批次未封口时不要 take」这条规则会漏掉先收尾的那些账号——
      · 甲先跑完 → 批次还在 running → 跳过 take → 甲的拒收留在 runlog 里；
      · 乙最后跑完 → 批次封口 → 只 take **乙自己那一枚**；
      · 甲那条永远没人取，**它的拒收从此不会出现在任何地方**（只剩后端日志）。
    表现是：3 个号里 2 个被核心全拒，卡片上只提 1 个——而且是随机的哪一个。
    """
    from app.plugins import runlog
    from app.routers import plugins as mod

    _grant_and_enable(client)          # 卡片那一行要先存在，_write_outcome 才写得进去
    runlog.reset()
    runlog.note_rejected("JTI-甲", "demo", "汇率", 3, "甲的原因")
    runlog.note_rejected("JTI-乙", "demo", "汇率", 5, "乙的原因")

    mod._batch_seal("B-FAN", 2)
    mod._result_writer("demo", "抓取", who="甲", batch="B-FAN", run="JTI-甲")(True, "ok")
    mod._result_writer("demo", "抓取", who="乙", batch="B-FAN", run="JTI-乙")(True, "ok")

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] != "ok", got
    assert "核心拒收 8 条" in got["summary"], \
        f"只报了一部分账号的拒收（甲 3 + 乙 5 = 8）：{got['summary']}"
    runlog.reset()


def test_core_facts_survive_when_every_callback_beats_the_seal(client, fake_plugin):
    """**所有回调都比 `seal_and_report` 先到**时，核心拒收照样要报出来。

    这是与上面那条相反的时序，而且是 `run_command` 自己的注释里讲的那条竞态——
    子进程可能毫秒级就结束（会话过期的插件打印一行就退），于是回调全部先到、
    `finally` 里的 seal 最后才跑。此时**唯一那次终态写入发生在 `seal_and_report` 里**。

    第一版漏了它：各账号的拒收确实被取进了批次，但 `_batch_seal` 把批次整个 pop 掉、
    而 seal 那一行直接 `_batch_text(...)` 写库、不过 `_apply_core_facts` ⇒
    卡片写的是绿色的「甲 ✓ ok；乙 ✓ ok」，零提示。
    核心拒了 8 条而界面说成功——正是 runlog 这个模块存在的唯一理由，原样复发。

    我此前所有的批次测试都是**先 seal、后回调**，把这半边完全排除在外了；
    这与 F16（永久「执行中」）是同一类错误：桩的时序掩盖了真实时序。
    """
    from app.plugins import runlog
    from app.routers import plugins as mod

    _grant_and_enable(client)
    runlog.reset()
    runlog.note_rejected("J-甲", "demo", "汇率", 3, "甲的原因")
    runlog.note_rejected("J-乙", "demo", "汇率", 5, "乙的原因")

    mod._result_writer("demo", "抓取", who="甲", batch="B-SEAL", run="J-甲")(True, "ok")
    mod._result_writer("demo", "抓取", who="乙", batch="B-SEAL", run="J-乙")(True, "ok")
    mod.seal_and_report("B-SEAL", 2, "demo", "抓取")     # ← 最后才封口

    got = {p["id"]: p for p in client.get("/api/plugins").json()}["demo"]["last_run"]
    assert got["outcome"] != "ok", f"核心拒了 8 条，卡片却是绿色的成功：{got}"
    assert "核心拒收 8 条" in got["summary"], got["summary"]
    runlog.reset()


def test_a_failed_reaper_thread_releases_the_mutex_key(client, fake_plugin, monkeypatch):
    """起收割线程失败时，互斥键必须放掉——否则这个账号**只有重启才能再跑**。

    全仓只有两处会清 `_INFLIGHT`：`_reap` 的 finally，和 Popen 抛错那一支。
    `threading.Thread(...).start()` 抛 `RuntimeError`（OS 拒绝建线程）时两处都没走到，
    于是那个键永久占着：此后每次点都是 409「已经在跑了」，而其实一个进程都没有。

    **进程本身不回滚**：`_ALIVE_PROCS` / `_OWN_GROUP` 里那条留给关停收——
    回滚它们会丢掉刚验证过的 pgid，它拉起的孙进程从此失联。
    也**不调 on_done**：那会让批次提前封口，卡片永久停在「执行中」（见 _batch_seal）。
    """
    from app.routers import plugins as mod

    gate = _popen_gate(monkeypatch)
    m = mod._find_manifest("demo")

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(mod.threading, "Thread", Boom)
    try:
        with pytest.raises(Exception) as e:
            mod._launch(m, "run", ["--account", "甲"])
        assert getattr(e.value, "status_code", None) == 500, e.value
        assert not mod._INFLIGHT, \
            f"起线程失败却把互斥键留下了，这个账号再也起不来：{mod._INFLIGHT}"
        # 进程留着交给关停收——它的 pgid 已经验证过，丢了就再也定位不到孙进程
        assert mod._ALIVE_PROCS, "进程被回滚了，它拉起的孙进程从此失联"
    finally:
        gate.set()
        with mod._PROCS_LOCK:
            mod._ALIVE_PROCS.clear()
            mod._OWN_GROUP.clear()
            mod._INFLIGHT.clear()
