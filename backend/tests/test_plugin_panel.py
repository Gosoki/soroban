"""插件面板：所有插件长得一样，各自按清单声明渲染。

验收点是「加一个插件不用改前端一行」。所以这里钉的是**声明 → 渲染数据**这条链，
而不是某个插件的具体样子。
"""
from __future__ import annotations

import json
import sys
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
    """`GET /api/fx` 要如实说「现在有没有插件能自动取汇率」。

    没有插件却还写着「自动获取由插件负责」的话，用户点过去是个空页面——
    而这恰好是最需要他去填手填汇率的时刻。
    """
    from app.routers import plugins as mod

    # fake_plugin 声明的是 fx:write，正好算「能提供汇率的插件」
    assert client.get("/api/fx").json()["auto_provider"] == "演示插件"

    monkeypatch.setattr(mod.settings, "PLUGIN_DIR", str(tmp_path / "gone"))
    mod._needs_cache.clear()
    assert client.get("/api/fx").json()["auto_provider"] == "", "插件没了却还说有人在自动取汇率"


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
