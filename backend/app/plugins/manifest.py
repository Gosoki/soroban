"""plugin.toml 的强类型解析。

**核心不认识任何具体插件**，只认识这里定义的几样东西：清单、参数、命令、账号维度。
插件页整张卡片按这些声明渲染——加一个插件不该动前端一行。

对老清单（没有 `[[commands]]` 的）做兼容：合成出 `login` / `fetch` 两条，
行为与硬编码时代逐字节相同。不做兼容的话，升级当天所有插件的按钮会一起消失。
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

log = logging.getLogger("soroban.plugins.manifest")

# 参数控件类型。刻意只有这几种——够描述「开关 + 数值 + 选项 + 文本 + 密钥」，
# 再多就该考虑声明式 UI 那条路了，不是往这里堆。
PARAM_TYPES = ("bool", "int", "str", "select", "secret")


class Param(NamedTuple):
    key: str
    label: str
    type: str = "str"
    default: Any = None
    hint: str = ""
    choices: Optional[list] = None       # select 用
    minimum: Optional[int] = None        # int 用
    maximum: Optional[int] = None
    secret: bool = False                 # True → 存下来但不回显（API 里返回掩码）


class Command(NamedTuple):
    name: str                            # 传给插件 CLI 的动词
    label: str                           # 按钮上的字
    hint: str = ""
    per: str = "plugin"                  # plugin=整体跑一次 | account=每个账号一次
    needs: tuple[str, ...] = ()          # 需要哪些 scope，缺了就不给点（而不是点了 403）
    confirm: str = ""                    # 非空 → 前端先弹确认，文案就是它
    primary: bool = False                # 卡片上的主按钮（高亮那个）


class Manifest(NamedTuple):
    id: str
    name: str
    version: str
    dir: Any                             # Path
    python: str
    entry: str
    accounts: bool                       # 是否按账号展开
    scopes: tuple[str, ...]              # 声明需要的权限
    settings: tuple[str, ...]            # 想读核心的哪几项设置
    params: tuple[Param, ...]
    commands: tuple[Command, ...]
    state_dir: str
    # 本插件的「账号」对应账本里的哪一列。声明了才支持「按账号改名/删单」那套操作——
    # 那些操作要把账号名迁到账本行上，核心得知道迁到哪一列。
    # 原先核心里写死的是 `if manifest["platform"] != "taobao"`，把一个插件的名字焊进了核心。
    ledger_field: str = ""
    # 插件**建议**的定时间隔（分钟）。只在这个插件还没有配置行时当默认值用；
    # 用户存过一次之后，以库里的为准（哪怕存的是 0 = 不定时）。
    # 没有它的话，装上汇率插件、开了开关、然后什么也不会发生——
    # 定时间隔默认 0，而「0 = 不定时」这件事只写在字段注释里，界面上看不出来。
    default_schedule_minutes: int = 0
    error: str = ""                      # 解析/校验失败时的原因（仍然进列表，不静默消失）

    def command(self, name: str) -> Optional[Command]:
        return next((c for c in self.commands if c.name == name), None)


# 老清单没有 [[commands]] 时合成这两条。与硬编码时代的行为逐字节相同：
# login 每账号一次、fetch 每账号一次（无账号插件则整体一次）。
_LEGACY_COMMANDS = (
    Command(name="login", label="扫码登录", per="account",
            hint="打开浏览器扫码，凭据存在插件自己的目录里，soroban 不碰"),
    Command(name="fetch", label="抓取", per="account", primary=True,
            hint="抓一次并回灌"),
)


def _param(raw: dict, plugin_id: str) -> Optional[Param]:
    key = str(raw.get("key") or "").strip()
    if not key:
        log.warning("插件 %s 有一项参数没写 key，已跳过", plugin_id)
        return None
    typ = str(raw.get("type") or "str")
    if typ not in PARAM_TYPES:
        log.warning("插件 %s 的参数 %s 用了未知类型 %r，按 str 处理", plugin_id, key, typ)
        typ = "str"
    if typ == "select" and not raw.get("choices"):
        log.warning("插件 %s 的参数 %s 是 select 却没给 choices，按 str 处理", plugin_id, key)
        typ = "str"
    return Param(
        key=key,
        label=str(raw.get("label") or key),
        type=typ,
        default=raw.get("default"),
        hint=str(raw.get("hint") or ""),
        choices=list(raw["choices"]) if raw.get("choices") else None,
        minimum=raw.get("min"),
        maximum=raw.get("max"),
        secret=bool(raw.get("secret") or typ == "secret"),
    )


def _command(raw: dict, plugin_id: str, has_accounts: bool) -> Optional[Command]:
    name = str(raw.get("name") or "").strip()
    if not name:
        log.warning("插件 %s 有一条命令没写 name，已跳过", plugin_id)
        return None
    per = str(raw.get("per") or ("account" if has_accounts else "plugin"))
    if per not in ("plugin", "account"):
        log.warning("插件 %s 的命令 %s 用了未知的 per=%r，按 plugin 处理", plugin_id, name, per)
        per = "plugin"
    if per == "account" and not has_accounts:
        # 声明了按账号跑、却没有账号维度 → 那条命令永远跑不起来。降级成整体跑一次并告警，
        # 比在界面上留一个点了没反应的按钮好。
        log.warning("插件 %s 的命令 %s 声明 per=account，但插件没有账号维度，改为整体执行",
                    plugin_id, name)
        per = "plugin"
    return Command(
        name=name,
        label=str(raw.get("label") or name),
        hint=str(raw.get("hint") or ""),
        per=per,
        needs=tuple(raw.get("needs") or ()),
        confirm=str(raw.get("confirm") or ""),
        primary=bool(raw.get("primary")),
    )


def _positive_int(v) -> int:
    """清单里的数字字段：写错了当没写，不让一份手写 toml 把插件页打崩。"""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def parse(raw: dict, directory) -> Manifest:
    """把 tomllib 读出来的 dict 变成强类型清单。

    **坏清单不静默消失**：缺 id 之类的硬伤会返回一个带 `error` 的清单，
    插件页照样列出它并显示原因。原先是 `continue` 掉——用户看到的是「插件不见了」，
    而不是「插件的清单写错了第几行」。
    """
    pid = str(raw.get("id") or "").strip()
    err = ""
    if not pid:
        pid = directory.name
        err = "plugin.toml 里缺 id"

    accounts = bool(raw.get("accounts"))
    params = tuple(p for p in (_param(x, pid) for x in (raw.get("params") or [])) if p)
    cmds = tuple(c for c in (_command(x, pid, accounts) for x in (raw.get("commands") or [])) if c)
    if not cmds:
        # 老清单：合成 login/fetch。无账号插件把 per 降级成整体一次。
        cmds = tuple(c._replace(per="account" if accounts else "plugin")
                     for c in _LEGACY_COMMANDS)
        if not accounts:
            cmds = tuple(c for c in cmds if c.name != "login")   # 没账号就没有「登录」可言

    dup = {c.name for c in cmds}
    if len(dup) != len(cmds):
        err = err or "有重名的命令"

    return Manifest(
        id=pid,
        name=str(raw.get("name") or pid),
        version=str(raw.get("version") or ""),
        dir=directory,
        python=str(raw.get("python") or ".venv/bin/python"),
        entry=str(raw.get("entry") or ""),
        accounts=accounts,
        scopes=tuple(raw.get("scopes") or ()),
        settings=tuple(raw.get("settings") or ()),
        params=params,
        commands=cmds,
        state_dir=str(raw.get("state_dir") or ".state"),
        ledger_field=str(raw.get("accounts_ledger_field") or ""),
        default_schedule_minutes=_positive_int(raw.get("default_schedule_minutes")),
        error=err,
    )


def describe_params(m: Manifest, values: dict) -> list[dict]:
    """给前端渲染参数表单用。secret 只回是否已设置，**不回值**。

    掩码而不是干脆不返回：前端要能显示「已设置」与「未设置」的区别，
    否则用户分不清「我填过了」和「它没保存上」。
    """
    out = []
    for p in m.params:
        cur = values.get(p.key, p.default)
        out.append({
            "key": p.key, "label": p.label, "type": p.type, "hint": p.hint,
            "choices": p.choices, "min": p.minimum, "max": p.maximum,
            "secret": p.secret, "default": None if p.secret else p.default,
            "value": ("__set__" if cur else "") if p.secret else cur,
        })
    return out
