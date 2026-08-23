"""插件（soroban 做管理层）。

soroban 扫 `PLUGIN_DIR` 下的 `soroban-plugin-*` 目录（各含 plugin.toml），负责：
发现、存配置/定时、触发它的标准 CLI。插件本体是独立进程/venv，soroban 只按 manifest 调它。
调用为子进程；触发时把 soroban 短期 token 下发给插件，插件无需存 soroban 密码。

**插件不等于爬虫**。淘宝订单抓取只是第一个；汇率、国际快递查询都没有「爬」的语义。
所以目录、前缀、术语一律用「插件/plugin」——旧的 `scraper/` 与 `soroban-scraper-*`
仍会被扫描（老部署不至于突然找不到插件），但新插件一律放 `plugins/soroban-plugin-*`。
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from ..auth import get_current_user
# 别名 pmanifest：本文件里 `manifest` 这个名字被多个函数的 dict 参数占着，
# 模块同名会在那些函数体内被静默遮蔽（今天没用到只是运气）。
from ..plugins import manifest as pmanifest
from ..plugins import params as plugin_params, runlog, scopes
from ..config import settings
from ..database import get_engine, get_session
from ..models import Order, PluginConfig, PluginRecord, User, utcnow
from ..schemas import PluginConfigIn
from .tags import (
    delete_account_staging,
    rename_tag_value,
    soft_delete_account_orders,
    tag_value_in_use,
)

log = logging.getLogger("soroban.plugins")

router = APIRouter(
    prefix="/api/plugins", tags=["plugins"], dependencies=[Depends(get_current_user)]
)

# …/soroban；PyInstaller 打包后 scraper/ 不打入 exe，放 exe 同级目录随包分发。
_SOROBAN_ROOT = (
    Path(sys.executable).resolve().parent           # 打包后：exe 同级
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[3]        # 源码：…/soroban
)
_SELF_URL = f"http://127.0.0.1:{os.environ.get('BACKEND_PORT', '8620')}"   # soroban 自身地址（插件同机回灌用）
# 账号名最终落进 Order/OrderStaging.platform_account，长度以那一列为准（超长在 MySQL 是 500）
_ACCOUNT_MAX = Order.__table__.columns["platform_account"].type.length


# 目录名前缀：新的在前。两个都扫是为了老部署平滑过渡，不是长期形态。
_PREFIXES = ("soroban-plugin-*", "soroban-scraper-*")

# 子进程最长跑多久。**令牌 TTL 必须由它推导**（见每个 scopes.issue 的 timeout_s）：
# `_REAP_TIMEOUT` **不在这里定义**——唯一真相在 `plugins_proc.py`，本文件下面从那里再导入。
# 这里曾经也写了一份 `= 1800`，而再导入排在它后面 ⇒ 后者覆盖前者 ⇒ 改这里那个数**静默无效**。
# （更讽刺的是：它上面原本挂的那段注释说的正是「两个数字分别写在两个文件里，就是这么长出来的」。）


def plugin_dir() -> Path:
    """插件根目录。PLUGIN_DIR > 旧的 SCRAPER_DIR > 仓库下的 plugins/。"""
    if settings.PLUGIN_DIR:
        return Path(settings.PLUGIN_DIR)
    if settings.SCRAPER_DIR:                 # 老 .env 里可能还写着它
        return Path(settings.SCRAPER_DIR)
    return _SOROBAN_ROOT / "plugins"


def plugin_roots() -> list[Path]:
    """要扫的根目录：新的 plugins/ + 旧的 scraper/（若还在）。

    保留旧目录是为了「升级后插件突然消失」不会发生——那种失败很吓人：
    界面上插件列表空了，用户以为配置丢了。
    """
    roots = [plugin_dir()]
    legacy = _SOROBAN_ROOT / "scraper"
    if legacy.is_dir() and legacy not in roots:
        roots.append(legacy)
    return roots


def _scope_lint(mf) -> str:
    """清单里的 scope 声明有没有硬伤。没有返回空串（调用方据此保留原有 error）。

    三种都查，因为三种的表现一模一样——**按钮永远灰着，而且没有任何提示说得出为什么**：
      ① 命令 needs 里的 scope 核心不认识（拼错）；
      ② 清单 scopes 里的 scope 核心不认识（拼错）；
      ③ 命令 needs 了一个清单自己没声明、也不是 baseline 的 scope。
    ③ 尤其阴：`save_grants` 会 `& set(manifest.scopes)` 把它丢掉，
    于是用户就算猜到要授权也授不了——他能勾的那些框里根本没有这一项。
    """
    known = set(scopes.SCOPES)
    declared = set(mf.scopes)
    needed = {n for c in mf.commands for n in c.needs}
    problems = []
    if bad := sorted((needed | declared) - known):
        problems.append(f"核心不认识这些权限（拼错了？）：{'、'.join(bad)}")
    baseline = {d["key"] for d in scopes.describe_baseline()}
    if missing := sorted(needed - declared - baseline):
        problems.append(f"命令要了清单没声明的权限：{'、'.join(missing)}"
                        f"（把它们加进 plugin.toml 的 scopes）")
    return "；".join(problems)


def discover() -> list[dict]:
    """扫描插件目录，读各 plugin.toml。返回 manifest 列表（附 _dir）。坏的跳过。

    同一个 id 在多个根目录下出现时，**先扫到的赢**（新目录优先）——
    搬家搬到一半时不至于出现两条同名插件。
    """
    out, seen = [], set()
    for base in plugin_roots():
        if not base.is_dir():
            continue
        for pattern in _PREFIXES:
            for d in sorted(base.glob(pattern)):
                f = d / "plugin.toml"
                if not (d.is_dir() and f.is_file()):
                    continue
                try:
                    m = tomllib.loads(f.read_text(encoding="utf-8"))
                except Exception as e:                  # noqa: BLE001  语法错 / 编码错都算
                    # **不 continue**。静默跳过的后果有三层，一层比一层糟：
                    #   1. 插件从列表里凭空消失，零日志——用户看到的是「插件不见了」，
                    #      而不是「plugin.toml 第几行写错了」。
                    #   2. 它在库里的配置被当成孤儿，卡片上写「插件目录已不在」，
                    #      而目录明明还在。
                    #   3. `forget_plugin` 的护栏正是「discover() 里还有没有它」——
                    #      于是一个只是打错了个引号的插件，它的授权与账号可以被一键清掉。
                    # 清单本身解析不出来时 id 只能从目录名推（约定就是 soroban-plugin-<id>，
                    # 见 tests/test_plugins.py::test_canonical_plugin_layout）。
                    pid = d.name
                    for pre in ("soroban-plugin-", "soroban-scraper-"):
                        pid = pid.removeprefix(pre)
                    m = {}
                    mf = pmanifest.parse({"id": pid}, d)._replace(
                        error=f"plugin.toml 读不了：{e}")
                    log.warning("插件目录 %s 的 plugin.toml 解析失败：%s", d.name, e)
                else:
                    mf = pmanifest.parse(m, d)
                    # 清单里的 scope 名写错 / 命令要了没声明的 scope，都要变成**可见的**清单错误。
                    # 不校验的话这两种写错都零报错，而且用户在界面上**无路可解**：
                    #   · needs 里有 scopes 没声明的 → blocked 恒含它，悬停提示「先在下面勾选授权」
                    #     指向一个根本不存在的勾选框（权限区只按 declared 渲染）；
                    #   · scope 名拼错（fx:wirte）→ 勾上后被 `& set(SCOPES)` 丢掉，
                    #     「需要新授权」的黄标永远消不掉，而 toggleGrant 还会弹「已授予 fx:wirte」——
                    #     一句明确的假话。
                    # 放在这里而不是 manifest.py：那边不该反向依赖 scopes 模块。
                    mf = mf._replace(error=mf.error or _scope_lint(mf))
                if mf.id in seen:
                    continue
                seen.add(mf.id)
                # 同时挂上强类型清单与原始 dict：老代码继续按 dict 取键，
                # 新代码走 `m["_m"]`。一次性全改的话这个文件要动几十处，风险不划算。
                m["_dir"] = d
                m["_m"] = mf
                # 把归一后的 id/name 写回原始 dict：清单缺 id 时 parse() 会用目录名兜底，
                # 不写回的话所有按 m["id"] 取值的老代码都会 KeyError。
                m["id"], m["name"] = mf.id, mf.name
                m["accounts"] = mf.accounts
                m["scopes"] = list(mf.scopes)
                m["settings"] = list(mf.settings)
                out.append(m)
    return out


def _find_manifest(plugin_id: str) -> dict:
    """按 id 找插件清单（plugin.toml 解析结果）。找不到 → 404。"""
    for m in discover():
        if m.get("id") == plugin_id:
            return m
    raise HTTPException(status_code=404, detail=f"未发现插件：{plugin_id}")


def _load_params(cfg: Optional[PluginConfig]) -> dict:
    if not cfg:
        return {}
    try:
        return json.loads(cfg.params_json)
    except Exception:                                   # params_json 被手改坏也不 500
        return {}


def _account_list(cfg: Optional[PluginConfig]) -> list[dict]:
    """结构化账号 [{name, platform, enabled}]。platform 加账号时定、之后不可改；enabled=False 即暂停。
    兼容旧格式「accounts 是逗号字符串 + 顶层 platform」——读时转成结构化（各账号沿用旧顶层平台）。"""
    params = _load_params(cfg)
    raw = params.get("accounts")
    if isinstance(raw, list):
        out = []
        for a in raw:
            name = str(a.get("name", "")).strip() if isinstance(a, dict) else ""
            if name:
                out.append({
                    "name": name,
                    "platform": (str(a.get("platform", "")).strip() or "淘宝"),
                    "enabled": bool(a.get("enabled", True)),
                })
        return out
    default_platform = str(params.get("platform", "")).strip() or "淘宝"   # 旧格式顶层平台
    return [{"name": n.strip(), "platform": default_platform, "enabled": True}
            for n in str(raw or "").split(",") if n.strip()]


def _account_names(cfg: Optional[PluginConfig]) -> list[str]:
    return [a["name"] for a in _account_list(cfg)]


def _save_accounts(session: Session, cfg: PluginConfig, accounts: list[dict]) -> None:
    """把结构化账号写回 params_json（顺带清掉旧的顶层 platform）。**不提交**，由调用方 commit。"""
    params = _load_params(cfg)
    params["accounts"] = accounts
    params.pop("platform", None)
    cfg.params_json = json.dumps(params, ensure_ascii=False)
    cfg.updated_at = utcnow()
    session.add(cfg)


# 会话文件「能不能解析」的缓存：(路径, mtime_ns, 大小) → bool。
# 卡片每次刷新都会问一遍每个账号，而会话文件能到 MB 级——不缓存就是每次刷新
# 都把它们全解析一遍。文件一变（mtime/size 变）键就变，天然失效。
_SESSION_OK: dict[tuple, bool] = {}


def _authorized(manifest: dict, account: str) -> bool:
    """该账号是否已授权：state 目录下有 `<account>.json`，**且它能解析成 JSON**。

    只判 `is_file()` 是不够的，而且这个「不够」是插件作者已经踩过并修掉的：
    淘宝插件的 `session.has_session()` 注释写着「会话文件存在**且**能解析成 JSON——
    坏文件不算已授权，避免『显示已授权却永远抓不了』」。
    但**用户看到的绿标来自核心这一份**。断电/磁盘满把 `.state/<账号>.json` 截断之后：
      · 核心 `is_file()` → True → 卡片绿标「已授权」、「抓这个号」可点；
      · 插件 `has_session()` → False → 每次 fetch 立刻退出。
    插件那条修复对用户唯一会看的那个界面完全无效。两份实现、核心那份更弱、
    且核心那份才是可见的——所以这里跟插件对齐，而不是反过来。
    """
    f = manifest["_dir"] / manifest.get("state_dir", ".state") / f"{account}.json"
    try:
        st = f.stat()
    except OSError:
        return False
    key = (str(f), st.st_mtime_ns, st.st_size)
    hit = _SESSION_OK.get(key)
    if hit is None:
        try:
            json.loads(f.read_text(encoding="utf-8"))
            hit = True
        except (OSError, ValueError):
            hit = False
        if len(_SESSION_OK) > 256:          # 账号改名/换插件会留下旧键，别让它无限长
            _SESSION_OK.clear()
        _SESSION_OK[key] = hit
    return hit


def _state_accounts(manifest: dict) -> list[str]:
    """扫 state 目录里已有的会话文件，返回其账号名（<account>.json 的名字）。
    用于 soroban 库被重置/换机后，磁盘上残留的授权仍能被发现、显示、复用（.tmp/.lock 不匹配 *.json）。"""
    d = manifest["_dir"] / manifest.get("state_dir", ".state")
    if not d.is_dir():
        return []
    return sorted(f.stem for f in d.glob("*.json"))


def _known_names(cfg: Optional[PluginConfig], manifest: dict) -> list[str]:
    """配置账号名 ∪ 磁盘会话名（配置在前、去重）。供校验与单账号抓取。"""
    names = _account_names(cfg)
    for n in _state_accounts(manifest):
        if n not in names:
            names.append(n)
    return names


def _display_accounts(cfg: Optional[PluginConfig], manifest: dict) -> list[dict]:
    """展示用账号列表：配置账号（结构化，含平台/启用）+ 磁盘有会话但不在配置里的孤儿（configured=false）。"""
    accs = _account_list(cfg)
    names = {a["name"] for a in accs}
    out = [{
        "account": a["name"], "platform": a["platform"], "enabled": a["enabled"],
        "configured": True, "authorized": _authorized(manifest, a["name"]),
    } for a in accs]
    for n in _state_accounts(manifest):                 # 磁盘残留会话：DB 重置/换机后仍可见、可复用
        if n not in names:
            out.append({
                "account": n, "platform": None, "enabled": False,
                "configured": False, "authorized": _authorized(manifest, n),
            })
    return out


def _state_file(manifest: dict, account: str) -> Path:
    """该账号会话文件的绝对路径，带目录穿越校验（account 可能来自用户手填的配置，别让它跳出 state 目录）。"""
    d = (manifest["_dir"] / manifest.get("state_dir", ".state")).resolve()
    f = (d / f"{account}.json").resolve()
    if f.parent != d:
        raise HTTPException(status_code=400, detail=f"非法账号名：{account}")
    return f


def _check_account_name(manifest: dict, account: str) -> Path:
    """账号名合法性统一校验：非空、不含逗号（逗号是历史 accounts 分隔符），且不穿越 state 目录。
    add/login/fetch 共用同一把尺子，避免 login/fetch 收下 add 不允许的名字而产生孤儿会话文件。"""
    if not account or "," in account:
        raise HTTPException(status_code=400, detail="账号昵称不能为空、且不能含逗号")
    return _state_file(manifest, account)


def _remove_account_state(manifest: dict, account: str) -> bool:
    """删该账号在 state 目录里的全部痕迹：会话 <account>.json、半成品 .tmp、文件锁 .lock。
    返回是否真的删到了登录会话（.json 是否存在过），供前端提示。"""
    f = _state_file(manifest, account)
    existed = f.is_file()
    for p in (f, f.with_name(f.name + ".tmp"), f.with_name(f"{account}.lock")):
        p.unlink(missing_ok=True)
    return existed


def _rename_state(manifest: dict, old: str, new: str) -> bool:
    """把磁盘登录会话 <old>.json 原子改名成 <new>.json，并清理旧的 .tmp/.lock（新名下会各自重建）。
    返回是否真搬了会话（old 有会话才搬；new 侧占用已在上层拒绝，不会覆盖）。"""
    of, nf = _state_file(manifest, old), _state_file(manifest, new)
    moved = False
    if of.is_file():
        of.replace(nf)                                      # 同目录 os.replace，原子
        moved = True
    of.with_name(of.name + ".tmp").unlink(missing_ok=True)
    of.with_name(f"{old}.lock").unlink(missing_ok=True)
    return moved


def _inherits(manifest: dict) -> bool:
    """这个插件是不是「跟 soroban 用同一个环境」（`python = "inherit"`）。

    单独成函数是因为它决定了**四件事**的分支：用哪个解释器、命令行怎么拼、
    依赖怎么探测、能不能点「安装」。散在四处各写一遍 `== "inherit"`，
    只要有一处漏了，表现就是「界面说已就绪、点了 ModuleNotFoundError」。
    """
    return manifest.get("python", ".venv/bin/python") == "inherit"


def _python(manifest: dict) -> Path:
    """跑这个插件用哪个解释器。

    `python = "inherit"` = 用 soroban 自己的解释器，**不建 venv、不用安装**。
    只给「依赖已在 soroban 里」的轻插件用（汇率只要 httpx）。这条是刻意留的：
    汇率是记账的必要供给方，不该让「Windows 上装不了 venv」变成「记不了账」——
    打包版 exe 在系统没有 Python 时一个插件都装不上，那时唯一能跑的就是这类。
    重依赖插件（浏览器、OCR）照旧走独立 venv，隔离仍然成立。

    ⚠️ inherit **必须**是 `sys.executable`，不能借道 `_base_python()`。
    那个函数找的是「能用来建 venv 的**系统** Python」，冻结态下它返回的是
    PATH 里的 python3——而 inherit 的字面意思是「继承 soroban 的环境」，
    系统 python3 里根本没有 httpx。这条路走通的表现是 ModuleNotFoundError，
    走不通（机器上没装 python）的表现更糟：回落到 exe 自己，
    于是每次跑插件都把 soroban 又启动一遍。
    冻结态下用 exe 当解释器是**刻意的**，配套的是 run.py 的 `--run-plugin`
    内部动词（见 `_plugin_argv`）。
    """
    if _inherits(manifest):
        return Path(sys.executable)
    want = manifest.get("python", ".venv/bin/python")
    p = manifest["_dir"] / want
    if not p.exists() and os.name == "nt":          # Windows 的 venv 是 Scripts/python.exe
        alt = manifest["_dir"] / ".venv" / "Scripts" / "python.exe"
        if alt.exists():
            return alt
    return p


def _plugin_argv(manifest: dict, command: str, extra: list[str]) -> list[str]:
    """跑一次插件的完整命令行。

    普通插件：`<插件venv的python> <entry…> <command> <extra…>`，就是 python 的老规矩。

    inherit 插件**在冻结态**要绕一下：`sys.executable` 是 soroban.exe，
    而 bootloader 不认 `-m`。改走 exe 的内部动词：
    `soroban.exe --run-plugin <插件目录> <entry…> <command> <extra…>`，
    由 run.py 的 `_run_plugin` 按 python 的语义解释 `entry`（见那边的说明）。
    源码态不需要这一层——那时 `sys.executable` 就是一个真的 python。
    """
    entry = shlex.split(manifest.get("entry", ""))
    if _inherits(manifest) and getattr(sys, "frozen", False):
        return [sys.executable, "--run-plugin", str(manifest["_dir"]),
                *entry, command, *extra]
    return [str(_python(manifest)), *entry, command, *extra]


_needs_cache: dict[str, tuple[float, list[dict]]] = {}
# 探测的单飞锁，按插件 id 一把。`setdefault` 建锁本身是原子的（GIL 下 dict 操作不会
# 交错出两把锁），所以不需要再套一把全局锁去保护它。
_NEEDS_LOCKS: dict[str, "threading.Lock"] = {}
_NEEDS_TTL = 60.0                                    # 秒


def needs_cached(manifest: dict) -> list[dict]:
    """`probe_needs` 的带缓存版本（60s）。

    名字必须自解释「带不带缓存」：叫 needs/_needs 时，下划线在 Python 里读作「私有」，
    读不出真正的区别，而两处调用哪一处写反都是静默故障——装完依赖用缓存版会报「仍缺依赖」，
    轮询接口用非缓存版会每次 spawn 三四个探测子进程。

    必须缓存：探测依赖要在**插件自己的解释器**里 `import`（每个模块一次子进程）再问一次
    Playwright 浏览器路径。而 GET /api/plugins 是前端**轮询**的接口——不缓存的话，每次轮询
    都要 spawn 三四个进程，装依赖时前端还在秒级轮询，直接把机器拖垮。
    安装结束会显式失效（见 _install_worker），所以不会拿着过期结论不放。

    **按插件单飞**：原先是裸的「先查后写」，缓存一过期，同一瞬间到达的每个请求都会
    各 spawn 一整套探测子进程（实测 6 路并发冷缓存 → `probe_needs` 被调 8 次，插件才 2 个）。
    而这个接口正是前端秒级轮询的那个——缓存刚好在轮询间隙过期是常态，不是边缘情形。
    锁按插件 id 分，不同插件之间照常并行；拿到锁之后**再查一次缓存**，
    因为等锁期间前一个持有者多半已经把结果填好了。"""
    key = manifest["id"]

    def _fresh():
        hit = _needs_cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < _NEEDS_TTL else None

    val = _fresh()
    if val is not None:
        return val
    with _NEEDS_LOCKS.setdefault(key, threading.Lock()):
        val = _fresh()                      # 等锁期间别人可能已经探好了
        if val is not None:
            return val
        val = probe_needs(manifest)
        _needs_cache[key] = (time.monotonic(), val)
    return val


def probe_needs(manifest: dict) -> list[dict]:
    """插件还缺哪些依赖。返回 [{key, label, hint}]；空列表 = 可以用了。

    分三档而不是笼统一句「未安装」：三者的补法完全不同（建 venv / 装 pip 包 / 下浏览器），
    用户看到「缺什么」才知道点下去会发生什么，也才看得懂失败在哪一步。
    """
    d, out = manifest["_dir"], []
    if _inherits(manifest):
        return _inherit_needs(d)                     # 没有 venv 可探，见那边
    py = _python(manifest)
    # 判据是「解释器能跑」而不是「文件存在」：venv 建到一半失败（例如系统缺 ensurepip）时，
    # bin/python 这个符号链接**已经在了**，只看存在与否会把半成品当成装好了，
    # 于是前端不提示重建 venv，却在下一步莫名其妙地报缺依赖。
    if not py.exists() or not _runs(py):
        out.append({"key": "venv", "label": "Python 环境（venv）",
                    "hint": "插件用独立 venv 隔离重依赖；现在没有或已损坏，需要（重新）建立"})
        return out                                   # venv 都不能用，后两项无从谈起
    req = d / "requirements.txt"
    if req.exists():
        missing = _missing_dists(py, _declared_dists(req))
        if missing:
            out.append({"key": "deps", "label": "Python 依赖",
                        "hint": "缺少：" + "、".join(missing)})
    if _wants_browser(d) and not _browser_ready(py):
        out.append({"key": "browser", "label": "浏览器内核（Chromium）",
                    "hint": "Playwright 需要一份自带的 Chromium（约 150MB），系统装的浏览器不算"})
    return out


def _inherit_needs(d: Path) -> list[dict]:
    """inherit 插件缺什么。**在本进程里问**，不 spawn 任何子进程。

    两条都不能走原来那套：
      · venv 检查：inherit 插件根本没有 venv，报「缺 Python 环境」会让用户去点安装，
        而安装建出来的 venv 永远不会被用到——一个点了没有任何效果的按钮。
      · `_missing_dists` 的子进程探测：解释器就是 soroban 自己；冻结态下
        `[soroban.exe, "-c", ...]` 会**把 soroban 又启动一遍**，而这条路径挂在
        前端**轮询**的 GET /api/plugins 上（60 秒缓存也拦不住每分钟一个影子实例）。
        本进程里 `importlib.metadata` 问的是同一个环境，答案更准且零开销。

    汇率插件原先连 `requirements.txt` 都没有 ⇒ 这一整段被跳过 ⇒ 卡片上
    `installed` 恒为 `true`：**界面写着「已就绪」，每次运行却 ModuleNotFoundError**。
    现在它声明了 httpx，这里如实检查 soroban 自己有没有。
    """
    req = d / "requirements.txt"
    if not req.exists():
        return []
    from importlib.metadata import PackageNotFoundError, distribution

    missing = []
    for name in _declared_dists(req):
        try:
            distribution(name)
        except PackageNotFoundError:
            missing.append(name)
        except Exception:                            # noqa: BLE001  元数据坏了不算缺
            pass
    if not missing:
        return []
    return [{"key": "deps", "label": "Python 依赖",
             # 刻意不给「一键安装」那套话术：这类插件跟 soroban 同环境，
             # 缺的包要装进 soroban 自己，不是建一个插件 venv 能解决的。
             "hint": "本插件与 soroban 共用运行环境，缺少：" + "、".join(missing)
                     + "。需要把它装进 soroban 自己的环境（打包版则要重新打包）"}]


def _declared_dists(req: Path) -> list[str]:
    """requirements.txt 的行 → **发行包名**（不是 import 名）。只做最直白的解析，够本项目用。

    刻意不去猜 import 名：`beautifulsoup4` 要 `import bs4`、`Pillow` 要 `import PIL`、
    `PyYAML` 要 `import yaml`。原先按 `名字.replace("-", "_")` 当模块名去 import，
    这类包**装好了也永远报「缺依赖」**——插件页一直显示未就绪，点安装又装不出变化，
    等于这个插件是块砖。requirements.txt 里写的本来就是发行包名，照着查就行。
    """
    out = []
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            out.append(name)
    return out


def _runs(py: Path) -> bool:
    """这个解释器能不能真跑起来（区分「文件在」与「venv 可用」）。"""
    try:
        return subprocess.run([str(py), "-c", "pass"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _has_pip(py: Path) -> bool:
    try:
        return subprocess.run([str(py), "-m", "pip", "--version"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# 在插件自己的解释器里问「这些发行包装了没」。一次问完所有的：
# 原先每个依赖一个子进程，而 GET /api/plugins 是前端轮询的接口。
_PROBE_DISTS = """
import sys
from importlib.metadata import PackageNotFoundError, distribution
miss = []
for name in sys.argv[1:]:
    try:
        distribution(name)
    except PackageNotFoundError:
        miss.append(name)
    except Exception:
        pass                      # 元数据坏了不算缺，别把用户卡在「装不完」的循环里
print("\\n".join(miss))
"""


def _missing_dists(py: Path, names: list[str]) -> list[str]:
    """这些发行包在**插件自己的解释器**里哪些没装。不能用 soroban 的解释器判断——两个环境是隔离的。

    探测本身失败（解释器挂了/超时）时返回空：报「缺依赖」会让用户去点安装，
    而问题根本不在依赖上，点了也不会变——那比不报更让人摸不着头脑。
    """
    if not names:
        return []
    try:
        r = subprocess.run([str(py), "-c", _PROBE_DISTS, *names],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _wants_browser(d: Path) -> bool:
    req = d / "requirements.txt"
    return req.exists() and "playwright" in req.read_text(encoding="utf-8").lower()


def _browser_ready(py: Path) -> bool:
    """Playwright 的 Chromium 是否已下载。问 playwright 自己要路径，别猜缓存目录
    （它随 PLAYWRIGHT_BROWSERS_PATH / 平台 / 版本变）。"""
    code = ("from playwright.sync_api import sync_playwright\n"
            "import os,sys\n"
            "with sync_playwright() as p: sys.exit(0 if os.path.exists(p.chromium.executable_path) else 1)")
    try:
        return subprocess.run([str(py), "-c", code], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# 起进程/收割/读输出这一层搬到了 plugins_proc.py（见那个文件的模块说明）。
# **在这里再导出**：测试与 main.py 大量按 `plugins.<名字>` 引用，
# 而 `monkeypatch.setattr(mod, "_launch", …)` 这类替身也依赖「名字挂在本模块上」。
from .plugins_proc import (  # noqa: F401
    PluginBusy,
    _ALIVE_PROCS,
    _COUNTERS,
    _INFLIGHT,
    _KNOWN_KEYS,
    _MAX_CAPTURE,
    _OWN_GROUP,
    _PROCS_LOCK,
    _REAP_TIMEOUT,
    _drain,
    _extra_notes,
    _group_has_members,
    _kill_tree,
    _reap,
    _remember_group,
    _run_account,
    _run_key,
    _run_label,
    _self_reported_error,
    _summarize,
    _sweep_group,
    shutdown_plugins,
)
def plugin_settings(session: Session, manifest: dict) -> dict:
    """插件声明它关心哪些设置项（plugin.toml 的 `settings = [...]`），这里取出它们的当前值。

    **设置项本身放在核心的注册表里**（`services/prefs.SPECS`），不放 plugin.toml。理由：
    设置页已经能按注册表自动渲染出标签、说明、取值范围、联动禁用；搬进 plugin.toml
    等于把这些退回成一个要手工编辑的文本文件。插件只声明「我要读哪几项」。

    声明了但注册表里没有的键会被跳过并告警——插件与核心版本不匹配时，
    宁可少给一项配置，也不该让整次触发失败。
    """
    from ..services import prefs

    wanted = manifest.get("settings") or []
    if not wanted:
        return {}
    conf = prefs.load(session)
    out, unknown = {}, []
    for key in wanted:
        if key in conf:
            out[key] = conf[key]
        else:
            unknown.append(key)
    if unknown:
        log.warning("插件 %s 声明了核心不认识的设置项 %s（版本不匹配？），已跳过",
                    manifest.get("id", "?"), unknown)
    return out


def _launch(manifest: dict, command: str, extra: list[str], token: Optional[str] = None,
            config: Optional[dict] = None, jti: Optional[str] = None,
            on_done=None) -> int:
    """子进程调插件 CLI（fire-and-forget；返回 pid，后台线程收割其结果写日志）。

    token 走**环境变量** SOROBAN_TOKEN 下发，不进 argv——避免短期凭据出现在进程表(ps)/日志里。
    config 同理走 SOROBAN_CONFIG（JSON）：设置项可能含 API key 之类，同样不该进 argv；
    而且它是结构化的（数组/对象），塞进命令行还得各自序列化一遍。
    """
    python = _python(manifest)
    if not python.exists():
        raise HTTPException(status_code=400, detail=f"插件未安装：缺 venv（{python}）。见插件 README。")
    cmd = _plugin_argv(manifest, command, extra)
    # **哨兵无条件设**：它说的是「这个进程是 soroban 起的」，与要不要下发令牌无关。
    #
    # 为什么要有：插件为了「手动跑」方便，通常会在没拿到 `SOROBAN_TOKEN` 时回落成
    # 账密登录（仓库里那个淘宝插件就是，默认账密还写在它自己的 config 里）。
    # 那条回落拿到的是一枚**完整用户令牌**，`_plugin_gate` 对非插件令牌直接放行
    # ⇒ 整套 scope 判定不适用。今天核心总是下发令牌，所以正常路径踩不到——
    # 但这意味着**任何让令牌下发失效的回归，表现不是响亮的 401，而是静默的全权限运行**，
    # 与本项目一贯的 fail-closed 取向正相反，而且日志和卡片上完全看不出区别。
    # 设了哨兵之后，插件能分清「没人给我令牌」和「我是被手动跑起来的」，
    # 前者当场失败、后者照旧回落。**这不是权限模型，是让一类回归有声音。**
    env = {**os.environ, "SOROBAN_LAUNCHED": "1"}
    if token:
        env["SOROBAN_TOKEN"] = token
    if config:
        env["SOROBAN_CONFIG"] = json.dumps(config, ensure_ascii=False)
    label = _run_label(manifest, command, extra)
    key = _run_key(manifest, command, extra)
    # **登记必须在 Popen 之前**，且与查重在同一把锁里：分两步做的话，两个并发请求
    # 会双双查到「没人在跑」，然后双双起进程——而这正是要防的那件事。
    with _PROCS_LOCK:
        holder = _INFLIGHT.get(key)
        if holder:
            raise PluginBusy(holder)        # 报**占着的那个**，不是自己——挡住 fetch 的可能是 login
        _INFLIGHT[key] = label
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(manifest["_dir"]), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError) as e:
        with _PROCS_LOCK:                       # 没起来就别占着键，否则这个账号永久起不来
            _INFLIGHT.pop(key, None)
        raise HTTPException(status_code=500, detail=f"启动插件失败：{e}")
    # jti 传给收割线程：进程一结束就把令牌作废。否则插件跑完之后那枚令牌还能用二十几分钟，
    # 而它此刻已经落在插件的日志/环境里了。
    with _PROCS_LOCK:
        _ALIVE_PROCS[proc.pid] = (proc, label)
        _remember_group(proc.pid, label)
    try:
        threading.Thread(target=_reap, args=(proc, label, key, jti, on_done), daemon=True).start()
    except RuntimeError as e:               # OS 拒绝建线程（fd/线程数耗尽）
        # **必须把互斥键放掉。** 全仓只有 `_reap` 的 finally 与上面 Popen 失败那一支会清它，
        # 而这里两者都没走到——于是这个账号的键永久占着，此后每次点都是 409「已经在跑了」，
        # 而其实一个进程都没有，只有重启才能恢复。
        # 进程本身**留在 `_ALIVE_PROCS`/`_OWN_GROUP` 里交给关停收**：
        # 回滚它们会丢掉刚验证过的 pgid，它拉起的孙进程从此失联。
        # 也**绝不调 on_done**——那会让批次提前封口，卡片永久停在「执行中」（见 _batch_seal）。
        with _PROCS_LOCK:
            _INFLIGHT.pop(key, None)
        log.error("插件 %s 起收割线程失败：%s（子进程 %s 已在跑，留给关停回收）",
                  label, e, proc.pid)
        raise HTTPException(status_code=500, detail=f"启动插件失败（无法创建收割线程）：{e}")
    return proc.pid


# --- 安装（建 venv / 装依赖 / 下浏览器）-------------------------------------------
# 为什么要有：插件自带独立 venv（plugin.toml 的 python 字段），但建它一直得用户自己开终端。
# 结果就是「把插件放进目录 → 面板上全是灰按钮 → 只写着『未安装(缺 venv)』」，没有下一步。
# 安装是**长任务且要联网**（pip 拉包、playwright 下 ~150MB 浏览器），所以后台跑 + 轮询状态，
# 绝不能卡住 HTTP 请求。

_install_state: dict[str, dict] = {}                 # plugin_id → {running, step, log, error, done_at}
_install_lock = threading.Lock()


def _base_python() -> Optional[str]:
    """能用来建插件 venv 的**真** Python 解释器路径；打包版找不到时返回 None。

    ⚠️ 不能直接用 `sys.executable`：PyInstaller 打包后它是 soroban.exe 自己。
    拿它去跑 `[soroban.exe, "-m", "venv", ...]`，bootloader 根本不解释 `-m`，
    结果是**把 soroban 自己又启动了一遍**（实测子进程真的跑了一遍 run.py：chdir、
    建 .env、连库跑迁移，最后卡在端口占用），而用户在界面上看到的报错是
    「建立 Python 环境失败：address already in use」——和建 venv 毫无关系。
    Windows 上更糟：uvicorn 无条件设 SO_REUSEADDR，影子实例会真的绑上端口常驻。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    import shutil
    for cand in ("python3", "python"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _venv_cmd(d: Path) -> list[str]:
    """建插件 venv 的命令。用 soroban 自己的解释器建——它一定存在、且与插件同机同 Python 系列。

    先试标准方式，让 venv 自带 pip（用户后续想手工 pip install 什么也方便）。
    只有当这台机器的 ensurepip 不可用时才退到 `--without-pip`——Debian/Ubuntu 上
    「装了 python3 却没装 python3-venv」很常见，标准 venv 会建到一半失败并留下半成品。
    没有 pip 也不影响安装：_pip_cmd 会借 soroban 的 pip 用 `--python` 装进去（pip 23.1+）。
    """
    py = _base_python()
    if py is None:      # 打包版且 PATH 里没有系统 python —— 调用方会先拦下，这里兜底
        raise RuntimeError("找不到可用的 Python 解释器")
    base = [py, "-m", "venv", str(d / ".venv")]
    try:
        if subprocess.run([py, "-c", "import ensurepip"],
                          capture_output=True, timeout=30).returncode == 0:
            return base
    except (OSError, subprocess.SubprocessError):
        pass
    log.info("本机 ensurepip 不可用，插件 venv 改用 --without-pip 建立")
    return [*base[:-1], "--without-pip", base[-1]]


def _pip_cmd(target_py: Path, args: list[str]) -> list[str]:
    """往目标 venv 装包的命令。

    优先用它**自己的** pip；没有（我们刻意用 --without-pip 建的）就借 soroban 的 pip、
    用 `--python` 指过去（pip 23.1+ 支持，实测装进去后目标 venv 能正常 import）。
    这样即便系统 python3-venv 不完整，安装依然能走完，不必要求用户先 apt install。"""
    if _has_pip(target_py):
        return [str(target_py), "-m", "pip", "install", *args]
    return [_base_python() or sys.executable, "-m", "pip", "--python", str(target_py), "install", *args]


def _install_worker(manifest: dict, with_browser: bool) -> None:
    """按 venv → pip → 浏览器 的顺序装，每步失败即停并把 stderr 尾巴留给前端。"""
    pid, d = manifest["id"], manifest["_dir"]
    req = d / "requirements.txt"
    # 每步的命令**延迟到执行时才拼**：建完 venv 之后解释器路径才存在，而它在 Windows 上是
    # Scripts/python.exe、在 POSIX 上是 bin/python——提前拼好会在 Windows 上指向不存在的路径。
    steps: list[tuple[str, callable]] = []
    py0 = _python(manifest)
    if not py0.exists() or not _runs(py0):
        steps.append(("建立 Python 环境", lambda: _venv_cmd(d)))
    if req.exists():
        steps.append(("安装 Python 依赖", lambda: _pip_cmd(_python(manifest), ["-q", "-r", str(req)])))
    if with_browser and _wants_browser(d):
        steps.append(("下载浏览器内核",
                      lambda: [str(_python(manifest)), "-m", "playwright", "install", "chromium"]))

    def fail(msg: str) -> None:
        with _install_lock:
            _install_state[pid].update(running=False, error=msg)
        _needs_cache.pop(pid, None)
        log.warning("插件 %s 安装失败：%s", pid, msg)

    for label, build in steps:
        with _install_lock:
            _install_state[pid]["step"] = label
        log.info("插件 %s 安装：%s", pid, label)
        try:
            r = subprocess.run(build(), cwd=str(d), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
        except Exception as e:                       # noqa: BLE001
            return fail(f"{label}失败：{e}")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-500:]
            return fail(f"{label}失败：{tail or '未知错误'}")
    _needs_cache.pop(pid, None)                      # 装完立刻失效缓存，别让前端看着旧结论
    remaining = probe_needs(manifest)
    with _install_lock:
        _install_state[pid].update(
            running=False, step="完成", done_at=utcnow().isoformat(),
            error=None if not remaining else "装完仍缺：" + "、".join(x["label"] for x in remaining))
    log.info("插件 %s 安装完成，仍缺 %s", pid, [x["key"] for x in remaining] or "无")


@router.post("/{plugin_id}/install")
def install_plugin(
    plugin_id: str,
    with_browser: bool = Query(True, description="是否一并下载 Playwright 浏览器内核（约 150MB）"),
):
    """补齐插件依赖。后台执行，用 GET /api/plugins 轮询 install 字段看进度。"""
    m = _find_manifest(plugin_id)
    if _inherits(m):
        # 这类插件跟 soroban 共用环境，没有 venv 可建。原先照样往下走：
        # 在插件目录里建一个**永远不会被用到**的 .venv，装完还是缺什么缺什么——
        # 一个点了有进度条、有成功提示、却什么也没改变的按钮。
        raise HTTPException(
            status_code=409,
            detail=f"「{m['_m'].name}」与 soroban 共用运行环境，没有独立环境可安装。"
                   "它缺的依赖要装进 soroban 自己（打包版则需重新打包）。",
        )
    if _base_python() is None:
        # 打包版（exe）里没有可用解释器。与其拿 exe 去当 python 跑出一个影子实例，
        # 不如明说——用户至少知道该装什么。
        raise HTTPException(
            status_code=409,
            detail="打包版内没有可用的 Python 解释器，无法自动建立插件环境。"
                   "请在本机安装 Python 3.11/3.12 后重试，或按插件 README 手工建 venv。",
        )
    with _install_lock:
        st = _install_state.get(plugin_id)
        if st and st.get("running"):
            raise HTTPException(status_code=409, detail="该插件正在安装中，请稍候")
        _install_state[plugin_id] = {"running": True, "step": "准备", "error": None, "done_at": None}
    threading.Thread(target=_install_worker, args=(m, with_browser), daemon=True).start()
    return {"ok": True}


def _last_run(cfg) -> dict:
    """卡片上「上次跑得怎么样」那一块。**两条分支共用一份**（已安装的插件、
    只剩残留配置的插件）——原先各拼各的，于是给字典加一个键时很容易只加一处，
    而测试环境里只走得到其中一条，另一条漏了不会红。

    `at` 是「上次跑完」（成败都推进），`ok_at` 是「上次**成功**跑完」。
    两个都要给：只有 `at` 的话，一个登录过期两周、每次定时都照跑照失败的插件，
    卡片上依然显示一个很新的时间戳。
    """
    return {
        "outcome": cfg.last_outcome if cfg else "",
        "summary": cfg.last_summary if cfg else "",
        "at": cfg.last_finished_at if cfg else None,
        "ok_at": cfg.last_ok_at if cfg else None,
    }


@router.get("")
def list_plugins(session: Session = Depends(get_session)):
    # **先把探测全做完，再碰数据库。**
    # 依赖探测要在插件自己的解释器里 spawn 子进程（缓存过期时每个插件最多 30-60 秒），
    # 而原先的循环是「先 `session.get(PluginConfig)`、后 `needs_cached(m)`」——
    # 第一轮就 checkout 一条连接，然后**跨所有探测一直握着**。
    # `database.py` 自己立的规矩就是「任何在 session 生命周期内做慢活的路由
    # （插件执行、爬虫、汇率抓取都算）都要先把连接还回去」，而 `shipment.py`、
    # `staging.py` 已经按这条先例显式 rollback 了——这个端点是仅剩的、
    # 而且还在被前端**秒级轮询**的违反者。
    #
    # 用「重排」而不是「循环中间插一句 rollback」：后者会 expire 掉已经读出来的 `cfg`，
    # 之后每读一个属性都重新 checkout，行要是被并发删掉还会 ObjectDeletedError。
    # 这个端点全程只读，重排是安全的。
    plugins = list(discover())
    # 这一句是**冗余的保险，不是机制本身**：`get_current_user` 末尾已经 rollback 过
    # （见 auth.py 的说明），所以此刻本来就没占着连接——删掉它那条守卫也不会红，我验过。
    # 留着是因为「探测之前不许有任何 DB 读」这件事，靠的是上面那次重排，
    # 而重排是脆的：谁在这行上面加一句 `session.get(...)`，连接就又被攥住了。
    # 有它在，那种改动最多多一次 checkout，不会退回「跨探测一直握着」。
    session.rollback()
    probed = {m["_m"].id: needs_cached(m) for m in plugins}

    out, seen_ids = [], set()
    for m in plugins:
        seen_ids.add(m["_m"].id)
        cfg = session.get(PluginConfig, m["_m"].id)
        need = probed[m["_m"].id]
        with _install_lock:
            st = dict(_install_state.get(m["_m"].id) or {})
        # 真正生效的那组权限：声明 ∩ 授权 ∩ 核心已知。命令的 blocked 与下面的
        # scopes.effective 都用它，保证界面上的禁用判据与执行闸是同一个集合。
        effective = scopes.token_scopes(m, cfg)
        out.append({
            "id": m["_m"].id, "name": m["_m"].name, "version": m["_m"].version,
            "installed": not need,                  # 「装好了」= 一样不缺，而非只看 venv 在不在
            "needs": need,                          # 缺什么，逐项给前端说清
            "install": st,                           # 安装进度（running/step/error）
            "python": str(_python(m)),
            "config": {
                "enabled": bool(cfg.enabled) if cfg else False,
                # 还没配置过 → 用清单建议的间隔（插件自己最清楚多久抓一次合适）；
                # 配置过 → 一律以库里的为准，用户存的 0 就是「明确不要定时」。
                "schedule_minutes": cfg.schedule_minutes if cfg else m["_m"].default_schedule_minutes,
                "last_run_at": cfg.last_run_at if cfg else None,
            },
            "missing": False,
            "accounts": _display_accounts(cfg, m),
            # 权限三件套：清单要什么、用户给了什么、实际生效的是什么。
            # 三者分开给，前端才能把「插件升级后多要了一项」显示成「需要新授权」，
            # 而不是悄悄按新清单放行。
            # 卡片按这两样渲染：参数表单 + 命令按钮。加插件不用动前端。
            "params": pmanifest.describe_params(m["_m"], plugin_params.load(m["_m"], cfg)),
            "commands": [
                {"name": c.name, "label": c.label, "hint": c.hint, "per": c.per,
                 "confirm": c.confirm, "primary": c.primary,
                 # 账号行据此决定「没授权时要不要灰掉这个按钮」。
                 # 不下发的话前端只能写死「login 之外的都要会话」——那是把一个具体插件的
                 # 动词名焊进界面，第二个插件的 status/probe 会被无端灰掉。
                 "needs_session": c.needs_session,
                 # needs 也给出去：前端在卡片上勾授权之后要就地重算 blocked。
                 # 只给 blocked 的话，勾完权限按钮仍停在「缺权限」禁用态，
                 # 得手动刷新整页才活过来——用户会以为授权没生效。
                 "needs": sorted(c.needs),
                 # 缺权限的命令直接禁用并说明，而不是让用户点了收 403。
                 # 判据必须与执行闸（run_command 的 `set(cmd.needs) - granted`）**同源**：
                 # 那边用的是 token_scopes = 声明 ∩ 授权 ∩ 核心已知，这里若用裸 granted，
                 # 作者把 needs 写成没在顶层 scopes 声明的项时，按钮永远灰、
                 # 勾选框（来自 declared）里又没那项可勾——用户没有任何操作能解锁。
                 "blocked": sorted(set(c.needs) - effective)}
                for c in m["_m"].commands
            ],
            "manifest_error": m["_m"].error,
            # 前端据此决定要不要渲染账号区。不给这个字段的话，无账号插件的卡片上
            # 会出现「添加账号」「账号（0）」——纯噪音，还会让人以为自己漏配了什么。
            "accounts_enabled": m["_m"].accounts,
            # 声明了就让前端渲染下拉，不再是自由文本（打错一个字就是一个新平台，
            # 而平台是账本活跃唯一键的一半）。空数组 = 没声明，前端沿用自由输入。
            "account_platforms": list(m["_m"].account_platforms),
            "last_run": _last_run(cfg),
            "scopes": {
                "declared": sorted(m.get("scopes") or []),
                "granted": sorted(json.loads(cfg.granted_scopes or "[]")) if cfg else [],
                "effective": sorted(effective),
                "catalog": scopes.describe(),
                # baseline 单独给：卡片上要能明说「这一项默认持有、勾选框里没有它」。
                # 不给的话前端只能在 effective 里看到一个 declared 里没有的 key，
                # 唯一能做的就是把它算进比值——那正是「一项没勾却显示 1/1」的来历。
                "baseline": scopes.describe_baseline(),
            },
        })

    # 目录里已经没有、但库里还留着配置的插件。**必须列出来**：
    # 它带着用户当初给的授权（granted_scopes）。留在库里不显示的话，
    # 以后放一个**同 id** 的插件进来（别人写的、或被改过的），它会静默继承那份授权——
    # 而整套权限的原则是「默认拒绝、升级不静默扩权」。列出来 + 给个清理按钮。
    for cfg in session.exec(select(PluginConfig)).all():
        if cfg.plugin_id in seen_ids:
            continue
        out.append({
            "id": cfg.plugin_id, "name": cfg.plugin_id, "version": "",
            "installed": False, "missing": True, "needs": [], "install": {},
            "python": "", "params": [], "commands": [], "accounts": [],
            "accounts_enabled": False,
            "account_platforms": [],
            "manifest_error": "插件目录已不在，这是库里残留的配置",
            "config": {"enabled": bool(cfg.enabled),
                       "schedule_minutes": cfg.schedule_minutes,
                       "last_run_at": cfg.last_run_at},
            "last_run": _last_run(cfg),
            "scopes": {"declared": [], "granted": sorted(json.loads(cfg.granted_scopes or "[]")),
                       "effective": [], "catalog": scopes.describe(),
                       # 残留配置没有清单、也不会再签发令牌，baseline 对它没有意义——
                       # 给空列表而不是省掉这个键：前端一份渲染代码走两条分支。
                       "baseline": []},
        })
    return out


@router.delete("/{plugin_id}/config")
def forget_plugin(plugin_id: str, session: Session = Depends(get_session)):
    """清理某个插件在库里的残留配置（授权、定时、账号、上次结果）。

    只允许清理**目录里已经不存在**的插件——还装着的插件要停用就用开关，
    误点一下把授权和账号全清掉太伤。
    """
    if any(m["_m"].id == plugin_id for m in discover()):
        raise HTTPException(status_code=409, detail="该插件还装着，先删掉它的目录再清理配置")
    cfg = session.get(PluginConfig, plugin_id)
    records = session.exec(
        select(PluginRecord).where(PluginRecord.plugin_id == plugin_id)
    ).all()
    if cfg is None and not records:
        raise HTTPException(status_code=404, detail="没有这个插件的配置")
    # **私有存储必须一起删。** 它原先没有任何删除入口：卸载插件之后
    # `pluginrecord` 里那些行会永久留着，而以后往 plugins/ 里放一个**同 id** 的插件
    # （别人写的、或被改过的版本），它一上来就能读到前一个插件攒下的全部私有数据
    # ——用户从没批准过这件事，界面上也完全看不到。
    # 与授权同一条理由：清理残留 = 把这个 id 恢复成「从没装过」的状态。
    for r in records:
        session.delete(r)
    granted = cfg.granted_scopes if cfg else "（无配置行）"
    if cfg is not None:
        session.delete(cfg)
    session.commit()
    log.info("已清理插件 %s 的残留：授权 %s、私有存储 %d 条", plugin_id, granted, len(records))
    return {"plugin_id": plugin_id, "removed": True, "records_removed": len(records)}


def _launch_conf(session: Session, m: dict, cfg: Optional[PluginConfig]) -> dict:
    """下发给子进程的 SOROBAN_CONFIG：核心设置 + **插件自己的参数**。

    手动与定时必须用同一份。原先只有手动路径带 `params`，定时那条只发 `plugin_settings()`——
    而 fx 的清单写的是 `settings = []`，于是 `plugin_settings()` 返回 `{}`、`_launch` 里
    `if config:` 判假、`SOROBAN_CONFIG` 干脆不设置，插件全部回落内置默认。

    后果不是报错而是**账本落错口径**：fx 默认 6 小时一轮，而当天取「手填优先、其次最后一条」，
    用户手点的那条几乎必然被后面的定时条盖掉——卡片上明明白白显示着用户设的源，
    实际写进账本的是默认源。安静地错，是本轮唯一会写坏账本数据的一条。
    """
    return {**plugin_settings(session, m), "params": plugin_params.load(m["_m"], cfg)}


def _ledger_field(m: dict) -> str:
    """插件清单声明的「账号名落在账本的哪一列」。

    单独一个函数是为了让 `m["_m"].ledger_field` 只出现在一处：调用点原先各自
    写死字符串 `"platform_account"`，清单里的声明只被当成「有没有」的开关用——
    以后哪个插件声明成别的列，会顺利通过校验然后操作**另一列**的数据。
    """
    return m["_m"].ledger_field


def _config_row(session: Session, plugin_id: str) -> PluginConfig:
    """取插件的配置行，没有就新建一行——**新建时带上清单建议的定时间隔**。

    不在这里统一建的话，谁先写谁定调：先点「授权」会建出一行 schedule_minutes=0，
    之后插件页读到的就是 0，清单里的建议值再也没机会生效，
    表现为「开关打开了但永远不抓」——而且看不出是哪一步弄丢的。
    """
    cfg = session.get(PluginConfig, plugin_id)
    if cfg:
        return cfg
    cfg = PluginConfig(plugin_id=plugin_id)
    try:
        cfg.schedule_minutes = _find_manifest(plugin_id)["_m"].default_schedule_minutes
    except HTTPException:
        pass                                  # 插件不在了也能建残留行的清理入口，不该在这儿炸
    return cfg


@router.put("/{plugin_id}/grants")
def save_grants(plugin_id: str, payload: dict, session: Session = Depends(get_session)):
    """授予/收回插件权限。只接受清单声明过、且核心认识的项。

    传进来的多余项**静默丢弃而不是报错**：插件降级或核心删了某个 scope 之后，
    界面上那份旧勾选不该让保存整个失败——用户会以为是别的地方坏了。
    """
    m = _find_manifest(plugin_id)
    want = set(payload.get("granted") or [])
    keep = sorted(want & set(m.get("scopes") or []) & set(scopes.SCOPES))
    cfg = _config_row(session, plugin_id)
    before = set(json.loads(cfg.granted_scopes or "[]"))
    cfg.granted_scopes = json.dumps(keep, ensure_ascii=False)
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    log.info("插件 %s 的授权改为：%s", plugin_id, keep or "（无）")

    # **收回了权限，就要把在飞的那枚令牌一起作废。**
    # 用户唯一一次真的去点撤销，恰好是他正看着这个插件在乱写的时候——
    # 而那一刻如果只改数据库里的 `granted_scopes`，正在跑的子进程会拿着旧令牌
    # 一直写到跑完（`_reap` 最长等 30 分钟），**卡片上却已经显示「未授权」了**。
    # 「用户唯一一次真的去用它，恰好是它什么都不做的那一次」。
    #
    # 只在**变窄**时撤：加授权不该打断正在跑的那次（那反而是帮倒忙）。
    revoked = scopes.revoke_plugin(plugin_id) if (before - set(keep)) else 0
    if revoked:
        log.info("插件 %s 收回了权限，已作废在飞令牌 %d 枚", plugin_id, revoked)
    return {"plugin_id": plugin_id, "granted": keep,
            "dropped": sorted(want - set(keep)), "revoked_running": revoked}


@router.put("/{plugin_id}/config")
def save_config(plugin_id: str, payload: PluginConfigIn, session: Session = Depends(get_session)):
    """只存插件级设置：启用定时 + 定时间隔。账号（昵称/平台/启用）走专用增删改端点，这里不碰。"""
    _find_manifest(plugin_id)                                    # 确认插件存在
    cfg = _config_row(session, plugin_id)
    cfg.enabled = payload.enabled
    cfg.schedule_minutes = max(0, payload.schedule_minutes)
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    return {"ok": True}


@router.post("/{plugin_id}/account")
def add_account(
    plugin_id: str,
    # 账号名会写进 Order/OrderStaging.platform_account，按该列长度限长（见 tags.check_value_fits）
    name: str = Query(..., max_length=_ACCOUNT_MAX, description="账号昵称"),
    platform: str = Query("", description="导入平台（加时定，之后不可改）"),
    session: Session = Depends(get_session),
):
    """添加一个账号：绑定昵称 + 平台（写一次即锁定），默认启用。之后在下面列表登录授权。"""
    m = _find_manifest(plugin_id)
    name = name.strip()
    _check_account_name(m, name)                        # 非空+无逗号+合法文件名（会话文件按此名存）
    # **平台不再由核心替所有插件默认成「淘宝」。**
    # 那个默认值对**每个**插件生效：装一个京东插件、加账号时不改平台，
    # 抓回来的单就带着 platform="淘宝" 进账本，而 `platform_provider`
    # （OCR 用它决定说哪句话）会据此报出**京东插件的名字**在管淘宝截图。
    # 清单声明了 `account_platforms` ⇒ 按它校验（前端也据此渲染下拉，不再是自由文本）；
    # 没声明 ⇒ 沿用旧行为（自由文本，空则回落「淘宝」），老插件与已有数据不受影响。
    choices = m["_m"].account_platforms
    platform = (platform or "").strip()
    if choices:
        if platform not in choices:
            raise HTTPException(status_code=422, detail=(
                f"「{m['_m'].name}」的账号平台只能是 {list(choices)} 之一，收到 {platform or '（空）'}"))
    else:
        platform = platform or "淘宝"
    cfg = _config_row(session, plugin_id)
    accs = _account_list(cfg)
    # **查重折叠大小写**：账号名同时是**会话文件名**（`<state_dir>/<账号>.json`），
    # 而 NTFS / APFS 大小写不敏感——`abc` 与 `ABC` 在 Windows 上是同一个文件。
    # 于是「加一个只有大小写不同的账号」会让两个账号共用一份 cookie：
    # 给其中一个注销（`delete_account` 的 unlink）删掉的是另一个的真身会话，
    # 而返回的 `removed_session: true` 看着完全正常。
    #
    # ⚠️ **只折叠这里，不折叠账本那一列。** `platform_account` 的大小写敏感是刻意的
    # （迁移 f2a3b4c5d6e7 特意把它改成 utf8mb4_0900_bin，理由就是不该把 Alice/alice 折成一个人）。
    # 这两件事的约束来自不同的层：一个是文件系统，一个是业务语义。
    if any(a["name"].casefold() == name.casefold() for a in accs):
        raise HTTPException(
            status_code=409,
            detail=f"账号已存在：{name}（账号名不区分大小写——它同时是本地会话文件名，"
                   f"而 Windows/macOS 的文件系统不区分）")
    accs.append({"name": name, "platform": (platform or "").strip() or "淘宝", "enabled": True})
    _save_accounts(session, cfg, accs)
    session.commit()
    return {"ok": True}


@router.patch("/{plugin_id}/account")
def set_account_enabled(
    plugin_id: str,
    account: str = Query(..., description="账号昵称"),
    enabled: bool = Query(..., description="是否启用（未启用=定时/全部抓取都跳过）"),
    session: Session = Depends(get_session),
):
    """启用/停用某账号。停用后定时与「抓取全部账号」都跳过它（仍可单独「抓这个号」）。"""
    _find_manifest(plugin_id)
    cfg = session.get(PluginConfig, plugin_id)
    accs = _account_list(cfg)
    if not any(a["name"] == account for a in accs):
        raise HTTPException(status_code=404, detail=f"该插件下没有账号：{account}")
    for a in accs:
        if a["name"] == account:
            a["enabled"] = enabled
    _save_accounts(session, cfg, accs)
    session.commit()
    return {"ok": True, "enabled": enabled}


@router.put("/{plugin_id}/params")
def save_params(plugin_id: str, payload: dict, session: Session = Depends(get_session)):
    """保存插件私有参数。清单里没声明的键丢弃（插件降级时不该让保存整体失败）。"""
    m = _find_manifest(plugin_id)
    cfg = _config_row(session, plugin_id)
    try:
        values = plugin_params.save(m["_m"], cfg, payload.get("params") or {})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    log.info("插件 %s 参数已更新：%s", plugin_id, plugin_params.redact(m["_m"], values))
    return {"plugin_id": plugin_id,
            "params": pmanifest.describe_params(m["_m"], values)}


@router.post("/{plugin_id}/run/{command}")
def run_command(plugin_id: str, command: str, account: Optional[str] = None,
                session: Session = Depends(get_session),
                current: User = Depends(get_current_user)):
    """执行插件声明的任意命令。**取代写死的 login / fetch。**

    加一个插件、或给已有插件加一个动词，都不用再往这里加端点——
    命令在 plugin.toml 里声明，界面按声明长按钮，这里按声明调。
    """
    m = _find_manifest(plugin_id)
    mf = m["_m"]
    cmd = mf.command(command)
    if cmd is None:
        raise HTTPException(status_code=404, detail=(
            f"插件 {plugin_id} 没有声明命令 {command}（有：{[c.name for c in mf.commands]}）"))
    if not _python(m).exists():
        raise HTTPException(status_code=400, detail=f"插件未安装：缺 venv（{_python(m)}）")

    cfg = _config_row(session, plugin_id)
    if not cfg.enabled:
        # 「启用」是这个插件的**总开关**：停用后定时不跑、手动也执行不了。
        # 界面上按钮已经禁用，但接口不能只靠界面把关——那样别的调用方（或手滑的 curl）
        # 照样能把一个用户明确停用的插件跑起来。
        raise HTTPException(status_code=409, detail=f"插件「{m['_m'].name}」已停用，先在卡片上打开开关")
    granted = scopes.token_scopes(m, cfg)
    missing = set(cmd.needs) - granted
    if missing:
        # 缺权限就明确拒绝并说缺哪一项，而不是让子进程跑起来再收一串 403。
        raise HTTPException(status_code=409, detail=(
            f"「{cmd.label}」需要权限 {sorted(missing)}，请先在插件卡片上勾选授权"))

    fan = _fan_targets(m, cfg, cmd, account)
    if not fan:
        raise HTTPException(status_code=400, detail="没有可用账号：先添加账号并启用")

    # **令牌只带这条命令声明要用的权限**，不是插件的全部授权。
    # `needs` 原先只用来禁按钮，令牌照发全量：于是一条 `needs = []`（「只测试、不写入」）
    # 的命令拿到的是能写暂存表、能改汇率的完整令牌——用户在卡片上看到的授权说明
    # 与实际下发的能力对不上，而 `needs` 这个字段的字面意思正是「我要用哪些权限」。
    # 少声明的命令会收 403 而不是悄悄越权，那是正确的失败方向：修法是去 plugin.toml 补声明。
    cmd_scopes = granted & set(cmd.needs)
    conf = _launch_conf(session, m, cfg)
    batch = uuid.uuid4().hex
    pids = []
    # **「执行中」必须写在起进程之前**，而不是之后。
    #
    # 这是「点了抓取以后一直显示执行中」的直接原因，而且是一个**跑得越快越必中**的竞态：
    # 子进程可能在毫秒级就结束（会话过期 → 插件打印一行「无会话，请先授权登录」当场退出，
    # 日志里就是这种）。收割线程随即 `_write_outcome(..., "failed")` 把结果写进库；
    # 而那时这个请求还没走到下面几行，紧接着就用 `"running"` 把它**盖掉并提交**。
    # 批次此刻已经从 `_BATCHES` 里弹掉了 ⇒ 再没有任何人会来改它 ⇒ 卡片永久停在「执行中…」。
    # 越是「一点就失败」的情形越稳定复现，而那恰恰是用户最想看到失败原因的时候。
    # **已经在跑的目标先摘掉。** 全都在跑就直接 409 —— 此刻一个字节都还没写，
    # 卡片上那次执行的进度不会被这次点击盖掉。
    # 这只是「说人话」的那一半；真正的互斥在 `_launch` 里（查重与登记在同一把锁内），
    # 因为这里到 `_launch` 之间仍有窗口。
    with _PROCS_LOCK:
        running = [w for extra, w in fan if _run_key(m, cmd.name, extra) in _INFLIGHT]
        fan = [(extra, w) for extra, w in fan if _run_key(m, cmd.name, extra) not in _INFLIGHT]
    if not fan:
        raise HTTPException(
            status_code=409,
            detail=f"「{cmd.label}」已经在跑了" + (f"：{'、'.join(w for w in running if w)}" if any(running) else "")
                   + "。等它跑完，或刷新看结果。")

    cfg.last_outcome, cfg.last_summary = "running", f"{cmd.label} 执行中…"
    cfg.last_finished_at = None
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    stolen = 0                      # 预筛之后被别的请求抢走的目标数，见下面 finally 的分支
    try:
        for extra, who in fan:
            # **一个子进程一枚令牌**。共用一枚的话，先跑完的那个账号在 `_reap` 里
            # `revoke(jti)`，还在跑的兄弟当场全部 401——它们已经抓到的订单再也回灌不进来，
            # 而且是静默的：插件那边只看到「soroban 拒绝了我」，用户只看到少了几单。
            # 令牌 TTL **必须**用 `_REAP_TIMEOUT`（收割超时），不能用 issue 的默认值。
            # 原先走默认 600s → TTL 恒为 12 分钟，而收割等 30 分钟：任何超过 12 分钟的抓取，
            # 最后那次回灌必然 401，整批订单静默丢失——单账号也中。
            token, jti = scopes.issue(current, plugin_id, cmd_scopes, timeout_s=_REAP_TIMEOUT)
            try:
                pids.append(_launch(m, cmd.name, [*extra, "--soroban-url", _SELF_URL],
                                    token=token, config=conf, jti=jti,
                                    on_done=_result_writer(plugin_id, cmd.label,
                                                           who=who, batch=batch, run=jti)))
            except PluginBusy:
                # 上面那次筛选之后才抢进来的（并发点击）。跳过这个账号，别拖累兄弟——
                # 而且**令牌必须当场作废**：它是为这个进程签的，进程没起来就没人会 revoke 它，
                # 否则每被挡一次就泄漏一枚能用二十几分钟的凭据。
                scopes.revoke(jti)
                running.append(who)
                stolen += 1
            except BaseException:
                # **别的失败也要作废这枚令牌**，理由与上面逐字相同。
                # 原先只有 `except PluginBusy` 那一支收，于是 `_launch` 因为别的原因失败时
                # （比如「无法创建收割线程」——那一支在 `Popen` **成功之后**才抛，
                # 子进程已经带着 SOROBAN_TOKEN 跑起来了、却没有任何收割线程会去 revoke），
                # 这枚令牌会一直被 `_plugin_gate` 认到 TTL 到期（最长 30 分钟）。
                # 用 BaseException 而不是 Exception：KeyboardInterrupt / SystemExit 时同样不该留。
                scopes.revoke(jti)
                raise
    finally:
        # **finally**：中途抛异常（缺 venv、令牌签发失败）时，已经起来的兄弟仍会回调，
        # 而批次还在等一个永远到不了的计划数——卡片会永久停在「执行中…」。
        if pids or stolen != len(fan):
            seal_and_report(batch, len(pids), plugin_id, cmd.label)
        else:
            # **全部目标都是在预筛之后被抢走的 ⇒ 这次点击当作没发生过。**
            # 与 `_run_due` 的 `if not launched and busy: continue` 同源。
            # 照常 seal 的话会写「一个任务都没能启动」+ failed，
            # 而此刻真正在跑的是**抢走它的那次执行**——用户看到的是自己正在跑的卡片
            # 突然变红说没启动起来，比什么都不显示更误导。
            # 批次要主动丢掉：没有任何子进程会来填它，留着就是一条永不回收的记录。
            _BATCHES.pop(batch, None)
            log.info("插件 %s 的「%s」全部 %d 个目标在预筛后被抢占，本次点击不改卡片",
                     plugin_id, cmd.label, stolen)
    if not pids and stolen:
        # 一个字节都没写。回 409 与「预筛就全被挡」那一支保持同一种回答——
        # 前端据此提示「已经在跑了」，而不是把「launched: true、pids: []」当成成功。
        raise HTTPException(
            status_code=409,
            detail=f"「{cmd.label}」已经在跑了" + (f"：{'、'.join(w for w in running if w)}" if any(running) else "")
                   + "。等它跑完，或刷新看结果。")
    return {"launched": True, "command": cmd.name, "pids": pids,
            "targets": [w for _, w in fan if w],
            # 被互斥挡掉的目标也如实回报：前端那句「已触发抓取」否则是半句假话。
            "skipped_running": [w for w in running if w],
            "scopes": sorted(cmd_scopes)}


# batch_id -> {"done": [(账号, ok, 摘要)], "total": 最终子进程数 or None（还没封口）}
#
# **total 必须是「实际起来的」数量，不是计划数。** 原先直接传 `len(fan)`：
# 扇出中途有账号启动失败（缺 venv、令牌签发异常）时，实际只有 k 个子进程会回调，
# 而这里在等 N 个 —— 永远凑不齐 ⇒ 卡片永久停在「执行中…」、这条记录也永不回收。
# 所以改成两段式：子进程各自 `_batch_add` 登记，扇出循环结束后 `_batch_seal` 告知最终数量。
# 两边都要判「是不是已经齐了」：子进程可能在封口**之前**就全跑完了。
_BATCHES: dict[str, dict] = {}
_BATCH_LOCK = threading.Lock()


def _batch_add(batch: str, who: str, ok: bool, summary: str,
               warn: bool = False) -> tuple[bool, list, Optional[int]]:
    """登记一个子进程的结果。返回 (是否已全部完成, 当前全部结果, 最终数量或 None)。"""
    with _BATCH_LOCK:
        b = _BATCHES.setdefault(batch, {"done": [], "total": None, "core": None})
        b["done"].append((who, ok, summary, warn))
        total, parts = b["total"], list(b["done"])
        finished = total is not None and len(parts) >= total
        if finished:
            _BATCHES.pop(batch, None)
    return finished, parts, total


def _batch_seal(batch: str, total: int) -> tuple[bool, list, Optional[dict]]:
    """扇出结束，告知最终有几个子进程。返回 (此刻是否已全部完成, 全部结果)。

    **登记用 setdefault 而不是 get。** `_launch` 是 fire-and-forget，所以日常路径上
    这一句跑在**所有回调之前**——`_BATCHES` 里此刻还是空的。原先那句
    `if b is None: return total <= 0, []` 于是把 total 整个丢掉，之后回调用 setdefault
    新建 `{"total": None}`，而 `_batch_text` 拿到 total=None 恒返回 "running"：
    **每一次执行都永久停在「执行中」**，前端 4 秒一轮询不停，批次每次泄漏一条。
    （当时的三条回归测试全都用同步 `on_done` 的桩，把这个顺序反了过来，所以一直是绿的。）
    """
    with _BATCH_LOCK:
        if total <= 0:                      # 一个都没起来：没有回调会来，清干净直接收工
            _BATCHES.pop(batch, None)
            return True, [], None
        b = _BATCHES.setdefault(batch, {"done": [], "total": None, "core": None})
        b["total"] = total
        parts = list(b["done"])
        core = dict(b["core"]) if b.get("core") else None   # **必须在 pop 之前读**
        finished = len(parts) >= total
        if finished:
            _BATCHES.pop(batch, None)
    return finished, parts, core


def _batch_text(label: str, parts: list, total: Optional[int]) -> tuple[str, str]:
    """把一批结果拼成卡片上的一行。返回 (outcome, summary)。

    outcome 是**三档**：ok / warn / failed。warn = 全都正常退出，但至少有一个
    自己报了 error（部分成功、软跳过）。少了这一档，那种结果只能二选一：
    算成功 → 绿字，用户不会再点开摘要；算失败 → 把插件作者刻意的软跳过刷成红色。
    """
    def _mark(o, w):
        return "✗" if not o else ("!" if w else "✓")

    body = "；".join(f"{w or '本次'} {_mark(o, wn)} {s}" for w, o, s, wn in parts)
    if total is not None and len(parts) >= total:
        if not all(o for _, o, _, _ in parts):
            outcome = "failed"
        elif any(wn for _, _, _, wn in parts):
            outcome = "warn"
        else:
            outcome = "ok"
        return outcome, f"{label}：{body}"
    n = f"{len(parts)}/{total}" if total else f"{len(parts)}"
    return "running", f"{label}（{n}）：{body}"


def _write_outcome(plugin_id: str, outcome: str, text: str) -> None:
    """把一次执行的结果写回 PluginConfig 供卡片显示。**开自己的 Session**：
    调用方可能是收割线程（没有请求作用域的 session）。写失败只记日志。"""
    try:
        with Session(get_engine()) as s:
            cfg = s.get(PluginConfig, plugin_id)
            if cfg is None:
                return
            cfg.last_outcome = outcome
            cfg.last_summary = text[:512]
            cfg.last_finished_at = None if outcome == "running" else utcnow()
            # 只有成功那次才推进 `last_ok_at`。失败也推的话，这一列就退化成
            # `last_finished_at` 的副本，而它存在的全部意义正是把两者分开：
            # 登录过期之后每次定时都照跑照失败，「上次跑完」一直很新，
            # 「上次抓到东西」才会停在两周前——后者才是那条会变红的线。
            if outcome == "ok":
                cfg.last_ok_at = cfg.last_finished_at
            s.add(cfg)
            s.commit()
    except Exception as e:                              # noqa: BLE001
        log.warning("写回插件 %s 的运行结果失败：%s", plugin_id, e)


def _collect_core_facts(batch: Optional[str], run: Optional[str]) -> Optional[dict]:
    """把这一枚 run 的核心事实取过来。有批次的话累进批次，返回**批次累计**。

    **必须每个账号收尾时立刻取自己那一枚**，不能等到批次封口时再取——
    「一个子进程一枚令牌」（见 run_command 的注释）意味着 runlog 是按**账号**分开记的，
    而封口那一次只知道自己那枚 jti。先跑完的账号那条从此没人取，
    它的拒收**永久消失**（只剩后端日志）。表现是「3 个号里 2 个被全拒，卡片只提 1 个，
    而且是随机的哪一个」。
    """
    rec = runlog.take(run) if run else None
    if not batch:
        return rec
    with _BATCH_LOCK:
        # **用 setdefault 而不是 get**，与 `_batch_add` 同一口径：`done()` 里取在登记之前，
        # 所以第一个回调到来时批次条目**还不存在**。用 get 的话它拿到 None 就把
        # 自己那一枚原样返回、不存进批次——而此刻 outcome 还是 running，返回值被丢掉，
        # 于是**第一个账号的拒收永久消失**（实测：甲 3 + 乙 5，卡片只报 5）。
        b = _BATCHES.setdefault(batch, {"done": [], "total": None, "core": None})
        if rec and rec.get("rejected"):
            cur = b.get("core") or {"rejected": 0, "first": ""}
            cur["rejected"] += rec["rejected"]
            if not cur["first"]:
                cur["first"] = rec.get("first") or ""
            b["core"] = cur
        return dict(b["core"]) if b.get("core") else None


def _apply_core_facts(core: Optional[dict], plugin_id: str, outcome: str, text: str):
    """把**核心侧看到的事实**并进卡片文案，覆盖插件的自报。

    今天只有一件：核心逐项拒收了多少条（见 plugins/runlog）。
    插件推 30 条、核心全拒、插件不看回执照样 print「已导入 30 单」并 exit 0 ——
    没有这一步，卡片就是绿色的成功，而库里零写入，用户只能靠翻后端日志发现。

    **只在终态并进文案**：running 时并进去会被下一次回调盖掉。
    但取（`_collect_core_facts`）必须在每个账号收尾时就做——取与用是两件事，
    分开之后「先跑完的账号被漏掉」那个洞才补得上。
    """
    if outcome == "running":
        return outcome, text
    if not core or not core.get("rejected"):
        return outcome, text
    n, first = core["rejected"], core.get("first") or ""
    # 成败仍以插件的退出码为准（那是跨进程契约），但**绝不允许显示成绿色的成功**：
    # 用户看到绿字就不会再点开摘要，而那句话里写着出了什么事。
    hard = outcome if outcome == "failed" else "warn"
    log.warning("插件 %s 自报 %s，但核心本次拒收了 %d 条——卡片按 %s 显示",
                plugin_id, outcome, n, hard)
    return hard, f"{text}｜核心拒收 {n} 条（{first}）"


def _result_writer(plugin_id: str, label: str, who: str = "",
                   batch: Optional[str] = None, run: Optional[str] = None):
    """子进程收割完之后把结果写回 PluginConfig，供插件卡片显示。

    **开自己的 Session**：收割跑在 daemon 线程里，没有请求作用域的 session 可用。
    写失败只记日志——结果展示失败不该影响别的东西。

    **多账号扇出要合并**：N 个子进程共写 PluginConfig 这**一行**的
    last_outcome/last_summary，谁后完成谁覆盖。于是「3 个号里 1 个登录过期」这件事，
    只要那个号先跑完，卡片上就只剩最后一个成功号的绿字——失败的账号在界面上
    完全消失，而它恰恰是唯一需要人去处理的那个。
    这里按 batch 聚合：全部完成前显示「已完成 k/N」，全部完成后逐个列出，
    只要有一个失败整批就记 failed。
    """
    def done(ok: bool, summary: str, warn: bool = False) -> None:
        if not batch:
            outcome = "failed" if not ok else ("warn" if warn else "ok")
            core = _collect_core_facts(None, run)
            _write_outcome(plugin_id, *_apply_core_facts(
                core, plugin_id, outcome, f"{label}：{summary}"))
            return
        # **顺序要紧，而且是这个顺序**：先取核心事实、再登记本次结果。
        # 反过来的话，最后一个账号的 `_batch_add` 会因为凑齐而**把整条批次 pop 掉**，
        # 此后 `_collect_core_facts` 拿不到那条批次，只能回落成「只用自己这一枚」——
        # 前面几个账号累进去的拒收随那条一起消失。（第一版就是反的，测试当场抓到。）
        core = _collect_core_facts(batch, run)
        _, parts, total = _batch_add(batch, who, ok, summary, warn)
        outcome, text = _batch_text(label, parts, total)
        _write_outcome(plugin_id, *_apply_core_facts(core, plugin_id, outcome, text))
    return done


def seal_and_report(batch: str, launched: int, plugin_id: str, label: str) -> None:
    """扇出循环结束后必调：告知批次最终有几个子进程起来了。

    **必须在 finally 里调**——中途抛异常（缺 venv、令牌签发失败）时，
    已经起来的那些兄弟仍然会回调，而批次还在等一个永远到不了的计划数。
    """
    if launched <= 0:
        # 一个进程都没起来（第一个账号就抛了：缺 venv、令牌签发失败…）。
        # 此刻库里已经是「执行中」（那一行现在写在起进程**之前**），
        # 而不会有任何回调到来 —— 不在这里交代一句，卡片就永久停在「执行中…」。
        _write_outcome(plugin_id, "failed", f"{label}：一个任务都没能启动")
        _batch_seal(batch, 0)
        return
    finished, parts, core = _batch_seal(batch, launched)
    if finished and parts:
        # **这条路径也要过 `_apply_core_facts`。** 它是「所有回调都比 seal 先到」时
        # 唯一那次终态写入——而那正是 `run_command` 注释里讲的那条竞态
        # （子进程可能毫秒级就结束）。漏了这里，各账号累进批次的核心拒收会随
        # `_batch_seal` 把批次 pop 掉一起消失，卡片照样是绿色的「✓ ok」——
        # 也就是 runlog 这整个模块存在的理由原样复发。
        _write_outcome(plugin_id, *_apply_core_facts(
            core, plugin_id, *_batch_text(label, parts, launched)))


@router.delete("/{plugin_id}/account")
def delete_account(
    plugin_id: str,
    account: str = Query(..., description="要注销的账号：删磁盘登录会话并移出配置"),
    session: Session = Depends(get_session),
):
    """注销某账号：删掉磁盘登录会话，并从插件配置的账号列表里移除。"""
    m = _find_manifest(plugin_id)
    cfg = session.get(PluginConfig, plugin_id)
    if account not in _known_names(cfg, m):
        raise HTTPException(status_code=404, detail=f"该插件下没有账号：{account}")
    removed = _remove_account_state(m, account)                 # 删磁盘会话（含 .tmp/.lock）
    accs = _account_list(cfg)
    if cfg and any(a["name"] == account for a in accs):         # 再从配置账号列表里摘掉它
        _save_accounts(session, cfg, [a for a in accs if a["name"] != account])
        session.commit()
    return {"ok": True, "removed_session": removed}


@router.post("/{plugin_id}/account/rename")
def rename_account(
    plugin_id: str,
    old: str = Query(..., description="原账号名"),
    new: str = Query(..., max_length=_ACCOUNT_MAX, description="新账号名（须全新、不含逗号）"),
    session: Session = Depends(get_session),
):
    """账号改名：一次性迁移它名下的暂存/账本订单（保留标签颜色）、重命名磁盘登录会话、更新插件配置。
    只做纯改名——new 若已被占用（已有账号/数据/授权）则拒绝，不与「合并」语义混淆。"""
    m = _find_manifest(plugin_id)
    if not m["_m"].ledger_field:
        # 该操作要把账号名迁到账本的某一列上，清单没声明 accounts_ledger_field
        # 就说明这个插件的账号与账本无关（如汇率），不支持这类操作。
        raise HTTPException(status_code=400, detail="该插件不支持账号改名")
    field = _ledger_field(m)            # 账号名落在账本的哪一列，由清单说了算
    new = new.strip()
    if not new or "," in new:
        raise HTTPException(status_code=400, detail="新账号名不能为空、且不能含逗号（逗号是账号分隔符）")
    _state_file(m, new)                                     # 校验 new 是合法文件名（目录穿越/非法名 → 400）
    cfg = session.get(PluginConfig, plugin_id)
    # old 有效 = 配置/磁盘里的账号，或历史数据/标签里出现过（列头改名可能改一个只存在于旧订单的账号）
    if old not in _known_names(cfg, m) and not tag_value_in_use(session, field, old):
        raise HTTPException(status_code=404, detail=f"没有这个账号：{old}")
    if new == old:
        return {"ok": True, "unchanged": True}
    # 账号名按 casefold 比（理由同 add_account：它是会话文件名）；
    # 账本数据那一半仍然逐字节比，那一列的大小写敏感是刻意的。
    if any(n.casefold() == new.casefold() for n in _known_names(cfg, m)) \
            or tag_value_in_use(session, field, new):
        raise HTTPException(status_code=409, detail=f"新名字已被占用（已有账号/数据/授权）：{new}")
    # 1) 一个事务：数据 + 标签 + 配置一起改（只改昵称，平台/启用保留）
    raw = rename_tag_value(session, field, old, new)
    counts = {"staging": raw.get("OrderStaging", 0), "orders": raw.get("Order", 0)}
    accs = _account_list(cfg)
    if cfg and any(a["name"] == old for a in accs):
        for a in accs:
            if a["name"] == old:
                a["name"] = new
        _save_accounts(session, cfg, accs)
    session.commit()
    # 2) 提交后再搬会话文件（DB 已一致；万一搬失败只影响授权显示，可在新名下重登恢复）。
    #    old 可能是只存在于历史数据、含非法字符的账号名（此时 _state_file 会抛 HTTPException），
    #    这类名字本就没有合法会话文件，搬移失败不该让已提交的改名反报 4xx → 一并降级为警告。
    try:
        moved = _rename_state(m, old, new)
        return {"ok": True, "moved_session": moved, **counts}
    except (OSError, HTTPException) as e:
        log.warning("改名 %s→%s 后会话文件重命名失败：%s", old, new, e)
        return {"ok": True, "moved_session": False, **counts,
                "warning": "订单数据已改名，但本地登录会话重命名失败，请在新名字下重新扫码登录。"}


def _require_platform_account(m: dict, session: Session, account: str) -> None:
    """校验：淘宝插件 + account 确为已知账号（配置/磁盘/历史数据里出现过）。否则 400/404。"""
    if not m["_m"].ledger_field:
        # 同上：清单没声明 accounts_ledger_field 就说明这个插件的账号与账本无关，
        # 「按账号删单」无从谈起。核心不该知道任何具体插件的 id。
        raise HTTPException(status_code=400, detail="该插件不支持按账号删除订单")
    field = _ledger_field(m)
    cfg = session.get(PluginConfig, m["id"])
    if account not in _known_names(cfg, m) and not tag_value_in_use(session, field, account):
        raise HTTPException(status_code=404, detail=f"没有这个账号：{account}")


@router.delete("/{plugin_id}/account/staging")
def delete_account_staging_ep(
    plugin_id: str,
    account: str = Query(..., description="要清空暂存的账号"),
    session: Session = Depends(get_session),
):
    """删除该账号在「暂存订单」表里的所有行（含物品明细）。不动账本正式订单。

    已导入且账本单仍在的行会被**跳过**（删了会留下无法再导入的孤儿账本单，见
    tags.delete_account_staging），跳过数在 skipped 里回报，供前端提示用户先去删账本单。"""
    m = _find_manifest(plugin_id)
    _require_platform_account(m, session, account)
    deleted, skipped = delete_account_staging(session, _ledger_field(m), account)
    session.commit()
    return {"ok": True, "deleted": deleted, "skipped": skipped}


@router.delete("/{plugin_id}/account/orders")
def delete_account_orders_ep(
    plugin_id: str,
    account: str = Query(..., description="要软删账本订单的账号"),
    session: Session = Depends(get_session),
):
    """软删该账号名下的所有账本正式淘宝订单（从账本移除、可在数据库层恢复）。不动暂存。"""
    m = _find_manifest(plugin_id)
    _require_platform_account(m, session, account)
    n = soft_delete_account_orders(session, _ledger_field(m), account)
    session.commit()
    return {"ok": True, "deleted": n}


# --- 定时调度：按每个启用插件的 schedule_minutes 周期触发 fetch --------------

def _due(last: Optional[dt.datetime], minutes: int, now: dt.datetime) -> bool:
    if last is None:
        return True
    if last.tzinfo is None:                             # SQLite 取回可能是 naive，统一按 UTC 处理
        last = last.replace(tzinfo=dt.timezone.utc)
    return (now - last).total_seconds() >= minutes * 60


def _fan_targets(m: dict, cfg, cmd, account: Optional[str] = None) -> list[tuple[list[str], str]]:
    """一次触发要起几个子进程、各带什么参数。返回 [(附加参数, 账号名)]。

    **手动与定时共用这一份**，因为扇出规则必须只有一条。原先定时走的是 `_fanout`
    （判据是**插件**声明了 accounts），手动走的是 `cmd.per`（判据是**命令**声明了 per）
    ——同一条命令于是有两种行为：手动跑一次、不带 --account；定时按账号跑 N 次、
    每次多带 `--account X`。插件收到一个它在手动路径下从未见过的参数组合，
    而这条差异只在无人值守的那条路径上出现，是最难被发现的一类。

    判据取 `cmd.per`：per 是**命令**的属性（「这条命令要不要按账号各跑一遍」），
    插件级的 `accounts` 只说明「这个插件有账号这个维度」。
    """
    if cmd.per != "account":
        return [([], "")]
    known = _account_list(cfg)
    if account:
        # **点名某个账号时不看「启用」**：账号级的启用只决定「批量跑的时候带不带它」，
        # 手动点某个号（补抓、重新登录）是用户当下的明确意图，不该被它挡住。
        # 磁盘上有会话、但配置里没登记的孤儿账号也允许点（换机/重置库之后就剩它了）。
        named = next((a for a in known if a["name"] == account), None)
        _check_account_name(m, account)
        extra = ["--account", account]
        # **孤儿账号（磁盘上有会话、配置里没登记）不许被凭空安上「淘宝」。**
        # 这一支原先写死 `.get("platform", "淘宝")`，而它对**任何**账号型插件生效：
        # 装一个京东插件、点一个只在磁盘上存在的号，核心会给它的子进程下发
        # `--platform 淘宝` ⇒ 抓回来的单 platform="淘宝" 进账本 ⇒
        # `platform_provider`（OCR 用它决定说哪句话）会报出**京东插件的名字**在管淘宝截图。
        # ⚠️ **这里只堵住了孤儿账号那一支。** 配置里登记过的账号仍按登记值走，
        # 而那个值本身也可能是核心替它填的「淘宝」：`_account_list` 对**新的结构化格式**
        # 就写着 `... or "淘宝"`，加账号端点的默认值同样是 `Query("淘宝")`——
        # 也就是说上面那个「京东插件」的例子对登记过的账号依然成立。
        # 要真正修干净，得给 `plugin.toml` 加一个 `account_platforms` 声明
        # （顺带能让前端渲染下拉而不是自由文本），那是一次接口变更，见审计报告 §125。
        # 不下发时插件用它自己的默认值——那才是「核心不认识任何具体插件」的口径。
        if named and named.get("platform"):
            extra += ["--platform", named["platform"]]
        return [(extra, account)]
    return [(["--account", a["name"], "--platform", a["platform"]], a["name"])
            for a in known if a["enabled"]]


def _scheduled_command(m: dict):
    """定时该跑哪条命令：清单里的 `fetch`，没有就取标了 `primary` 的那条。

    定时是**无人值守**的，所以判据必须来自清单而不是核心里的一个字符串常量：
    写死 "fetch" 的话，没声明它的插件会被一遍遍拉起来跑一个它不认识的动词。
    （`per="account"` 的命令由 `_fan_targets` 负责展开，这里只挑动词。）
    """
    cmds = m["_m"].commands
    # **优先看清单自己说的**（`schedulable = true`）。
    # 原口径是「先按字面量找叫 fetch 的，没有再取 primary」，两种误判都真实存在：
    #   · 把 `sync` 标成 primary、同时留一条诊断用 `fetch` 的插件 ⇒ 定时跑错动词；
    #   · 单纯翻转成「先 primary」 ⇒ 把 `login` 标成 primary 的清单会在无人值守时
    #     反复弹出有头浏览器——而定时正是没人看着的那条路。
    # 声明了就没有猜的余地；一条都没声明时回落到老口径，老清单行为不变。
    declared = [c for c in cmds if c.schedulable]
    if declared:
        return declared[0]
    return (next((c for c in cmds if c.name == "fetch"), None)
            or next((c for c in cmds if c.primary), None))


def _run_due(session: Session) -> None:
    user = session.exec(select(User).where(User.is_active == True)).first()  # noqa: E712
    if not user:
        return
    manifests = {m["id"]: m for m in discover()}
    now, token = utcnow(), None
    for cfg in session.exec(select(PluginConfig).where(PluginConfig.enabled == True)).all():  # noqa: E712
        if cfg.schedule_minutes <= 0 or not _due(cfg.last_run_at, cfg.schedule_minutes, now):
            continue
        m = manifests.get(cfg.plugin_id)
        if not m or not _python(m).exists():
            continue
        # 动词从**清单**里取，不写死。写死 "fetch" 的话：清单里没声明它的插件会被
        # 一遍遍拉起来跑一个它不认识的动词，而 `launched` 只数「进程起没起来」，
        # 于是 last_run_at 照常推进——每个周期白跑一次，卡片上还显示「已触发」。
        cmd = _scheduled_command(m)
        if cmd is None:
            log.warning("插件 %s 没有可定时执行的命令，跳过（清单里得声明一条 fetch 或 primary 命令）",
                        cfg.plugin_id)
            continue
        granted = scopes.token_scopes(m, cfg)
        missing = set(cmd.needs) - granted
        if missing:
            # 与手动路径同一条闸：缺权限就别起进程。不拦的话子进程跑起来收一串 403，
            # 用户在卡片上只看到「失败」，看不出是**没勾授权**。
            log.warning("插件 %s 的定时任务缺权限 %s，跳过（去插件卡片上勾选授权）",
                        cfg.plugin_id, sorted(missing))
            continue
        launched = 0
        # 扇出与令牌口径都与手动路径**同源**，见 _fan_targets / run_command 的注释。
        targets = _fan_targets(m, cfg, cmd)
        cmd_scopes = granted & set(cmd.needs)
        batch = uuid.uuid4().hex
        busy = 0
        for extra, who in targets:
            # **jti 必须先置空**：它在 try 里赋值，而 `scopes.issue` 自己也可能抛。
            # 不置空的话 except 里的 `revoke(jti)` 要么撞未绑定，要么**吊销上一个账号的令牌**
            # ——那个账号的进程还在飞，它后续回灌会全部 401，而且是静默的。
            jti = None
            try:
                tok, jti = scopes.issue(user, cfg.plugin_id, cmd_scopes,
                                        timeout_s=_REAP_TIMEOUT)
                _launch(m, cmd.name, [*extra, "--soroban-url", _SELF_URL],
                        token=tok, config=_launch_conf(session, m, cfg), jti=jti,
                        # 定时也要写回结果。不写的话卡片上的「上次结果」永远停在上一次**手动**
                        # 那一次，而「上次触发」时间一直在走——定时失败在界面上完全不可见。
                        # 汇率取不到恰恰是账本会悄悄用兜底值的那类故障，最需要痕迹。
                        on_done=_result_writer(cfg.plugin_id, f"定时·{cmd.label}",
                                               who=who, batch=batch, run=jti))
                launched += 1
            except PluginBusy:
                # **这一支原先不存在，而它是常态**：`last_run_at` 全仓唯一的写入点就在下面，
                # 手动抓取**不推进它**——所以「手动那次还在飞（最长 30 分钟）时定时到点」
                # 没有任何东西挡住，而 `_run_due` 也没有手动路径那道 `_INFLIGHT` 预筛。
                # 漏掉它的后果不是少跑一次：`PluginBusy` 是 RuntimeError，会**逃出两层循环**
                # ⇒ 末尾的 `session.commit()` 不执行 ⇒ 本轮**已经成功起了进程的插件**，
                # 它们的 last_run_at 一起丢。而 `select(PluginConfig)` 没有 order_by，
                # 只要 fx 排在忙着的 taobao 前面，fx（6 小时一次、秒级跑完）就会
                # **每 60 秒被重起一次**，每次往汇率表追加一行——约 30 倍。
                scopes.revoke(jti)                      # 进程没起来，这枚令牌没人会 revoke
                busy += 1
                log.info("定时任务 %s/%s 跳过：同目标已有一个在跑", cfg.plugin_id, who or "-")
            except HTTPException as e:
                scopes.revoke(jti)                      # 同上：起不来就别把令牌漏在外面
                log.warning("定时任务 %s/%s 启动失败：%s", cfg.plugin_id, who or "-", e.detail)
        if not launched and busy:
            # **全部目标都被互斥挡下 ⇒ 什么都不做，直接看下一个插件。**
            # 不能走下面的 seal_and_report：它在 launched==0 时会写
            # 「定时·抓取：一个任务都没能启动」+ failed，把用户**正在跑的那张手动卡片**
            # 每 60 秒刷成红色——比悬空更误导。这一轮本来就没有任何事发生。
            log.info("定时任务 %s：%d 个目标都在跑，本轮跳过", cfg.plugin_id, busy)
            continue
        # 按**实际起来的**数量封口。这条路径最容易命中：它自己吞掉单个账号的启动失败，
        # 于是「计划 2 个、只起来 1 个」是常态，而按计划数等就永远等不齐。
        seal_and_report(batch, launched, cfg.plugin_id, f"定时·{cmd.label}")
        if launched:                                    # 只有真的起了进程才推进 last_run_at
            cfg.last_run_at = now                       # 空账号/全部启动失败 → 不推进，下轮重试
            session.add(cfg)
    session.commit()


def _run_due_in_session() -> None:
    """同步执行一轮定时抓取。单独抽出来是为了能整体丢进线程池（见 scheduler_loop）。"""
    with Session(get_engine()) as session:
        _run_due(session)


async def scheduler_loop(interval: int = 60) -> None:
    """后台循环：每 interval 秒检查一次到点的插件并触发抓取（放进 lifespan）。

    只读屏障期间跳过。不是怕它写坏（回灌走 HTTP，会被中间件拦下），而是**白开一次浏览器
    冲淘宝**——并发多开浏览器是风控红线（见插件 docs/风控与对策.md）。
    跳过也不会漏抓：last_run_at 没被推进，下一轮到点自然重来。"""
    from ..maintenance import barrier

    while True:
        try:
            reason = barrier.blocked_reason()
            if reason:
                log.info("定时抓取跳过：%s", reason)
            else:
                # **必须丢进线程池**：这是个 async 协程，而 Session/pymysql 是同步 I/O。
                # MySQL 运行中掉线时，建连会阻塞住整个事件循环——实测单次卡了 384 秒
                # （pymysql 的 read_timeout 默认 None，connect_timeout 只管 TCP 建连
                # 不管握手读），期间连 /api/health、静态资源都一起卡死。
                await run_in_threadpool(_run_due_in_session)
        except Exception as e:                          # 单轮异常不结束循环
            log.warning("插件定时循环异常：%s", e)
        await asyncio.sleep(interval)


def platform_provider(session: Session, platform: str) -> str:
    """这个来源平台（「淘宝」「京东」…）有没有插件在管。返回插件名，没有则空串。

    判据是**插件自己配的账号上写的 platform**，不是插件 id——核心不认识任何具体插件
    （`test_core_does_not_hardcode_any_plugin_id` 钉着这条）。
    账号的平台是用户在插件页添加账号时选的，本来就是「这个号抓的是哪个平台」。

    用途：OCR 认出截图不是闲鱼时，据此把提示写准。
    「淘宝的截图，而你**已经装了**淘宝插件（更准、字段更全），确定还要 OCR 建单吗」
    和「淘宝的截图，没有对应插件，OCR 是你唯一的路」——这两句话该说哪一句，
    取决于这台机器上的实际情况，不能写死。
    """
    if not platform:
        return ""
    try:
        for m in discover():
            cfg = session.get(PluginConfig, m["_m"].id)
            if not (cfg and cfg.enabled):
                continue
            for a in _account_list(cfg):
                if a.get("enabled") and str(a.get("platform", "")).strip() == platform:
                    return m["_m"].name
    except Exception:                                   # noqa: BLE001  探测失败不该让 OCR 挂掉
        pass
    return ""


def _bundled_plugin_root() -> Optional[Path]:
    """打进 exe 里的那份插件源码（只有冻结态才有）。"""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    d = Path(base) / "plugins"
    return d if d.is_dir() else None


def _manifest_version(f: Path) -> str:
    """从 plugin.toml 里取版本号。取不到就返回空串——**空串一律当作「不升级」**，
    否则一个读坏的清单会让每次启动都覆盖一遍用户目录。"""
    try:
        return str(tomllib.loads(f.read_text(encoding="utf-8")).get("version", "") or "")
    except Exception:                                   # noqa: BLE001
        return ""


def seed_bundled_plugins() -> dict[str, str]:
    """把打进 exe 的插件释放到运行目录的 plugins/。返回 {目录名: "new"|"updated"}。

    **为什么必须落到磁盘**：插件目录是可写的——各自的 `.venv`、登录会话 `.state`、
    参数都写在里面，而 onefile 的 `_MEIPASS` 每次进程退出就被清掉。
    就地跑的话，淘宝插件每次启动都要重新装一遍依赖、重新扫码登录。

    两条规则：
      · 目录**不存在** → 整份复制过去（首次运行、或用户手滑删了）。
      · 目录已在，但 exe 里那份 `plugin.toml` 的 version 与磁盘上的**不同** →
        逐文件覆盖。换 exe 就该换到新插件，否则「升级了却不生效」会变成下一个 bug。
        比的是「不同」而不是「更新」：版本号格式由插件作者定，
        解析不出大小的时候，回滚（装个旧 exe）同样应该生效。

    **只覆盖、绝不删除**：`.venv`、`.state`、用户自己加的文件都不在 exe 那份里，
    删除逻辑会把它们一起带走——那是登录会话和几百 MB 的依赖。
    """
    src_root = _bundled_plugin_root()
    if src_root is None:
        return {}
    dst_root = plugin_dir()
    done: dict[str, str] = {}
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
        for src in sorted(src_root.iterdir()):
            if not src.is_dir():
                continue
            dst = dst_root / src.name
            bundled_ver = _manifest_version(src / "plugin.toml")
            if not dst.exists():
                action = "new"
            elif bundled_ver and bundled_ver != _manifest_version(dst / "plugin.toml"):
                action = "updated"
            else:
                continue
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                target = dst / f.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
            done[src.name] = action
        if done:
            log.info("已从 exe 内释放插件：%s",
                     "、".join(f"{k}({v})" for k, v in done.items()))
    except OSError as e:
        # 释放失败不该挡启动：插件页会照常显示「没发现插件」，比整个应用起不来好。
        log.warning("释放内置插件失败：%s", e)
    return done


def reclaim_stale_runs() -> int:
    """启动时把上一条命还留在库里的「执行中」收掉。返回改了几行。

    **为什么必须有**：`last_outcome` 是**跨进程持久化**的，而唯一会把它从 `running`
    改走的是收割线程（`_reap` 的 finally）——它是 daemon 线程，主进程一没，它就没了。
    于是任何一次「插件还在跑的时候关掉 soroban」（关窗口、Ctrl-C、崩溃、断电）
    都会在库里留下一个永久的 `running`：下次启动后卡片顶着黄色「执行中」，
    而**没有任何进程会再来改它**——刷新没用、重启没用，只能去改数据库。

    `_BATCHES` 是纯内存的，重启即空，所以那条批次也永远凑不齐，属于同一件事的两半。

    判据只有一条：**进程刚起来，`_ALIVE_PROCS` 必然是空的**，所以此刻库里任何
    `running` 都不可能有活着的进程与之对应。不需要时间窗、不需要 PID 表。
    改成 `failed` 而不是清空：那一轮到底抓没抓到东西无人知道，说成「没结果」
    比说成「成功」诚实，用户看到就知道该重跑一次。
    """
    try:
        with Session(get_engine()) as s:
            rows = s.exec(select(PluginConfig)
                          .where(PluginConfig.last_outcome == "running")).all()
            for cfg in rows:
                cfg.last_outcome = "failed"
                cfg.last_summary = (f"{cfg.last_summary or '上次执行'}"
                                    "——soroban 在它跑完之前退出了，结果已丢失，请重跑")[:512]
                cfg.last_finished_at = utcnow()
                s.add(cfg)
            if rows:
                s.commit()
                log.info("启动清扫：%d 个插件的「执行中」是上一条命遗留的，已标为失败",
                         len(rows))
            return len(rows)
    except Exception as e:                              # noqa: BLE001  清扫失败不该挡启动
        log.warning("启动清扫遗留的插件运行状态失败：%s", e)
        return 0


