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
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, select

from ..auth import get_current_user
# 别名 pmanifest：本文件里 `manifest` 这个名字被多个函数的 dict 参数占着，
# 模块同名会在那些函数体内被静默遮蔽（今天没用到只是运气）。
from ..plugins import manifest as pmanifest
from ..plugins import params as plugin_params, scopes
from ..config import settings
from ..database import get_engine, get_session
from ..models import Order, PluginConfig, User, utcnow
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
                except Exception:
                    continue
                mf = pmanifest.parse(m, d)
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
    raise HTTPException(status_code=404, detail=f"未发现插件: {plugin_id}")


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


def _authorized(manifest: dict, account: str) -> bool:
    """该账号是否已授权：插件的 state 目录下有 <account>.json（登录会话）即算。"""
    state_dir = manifest.get("state_dir", ".state")
    return (manifest["_dir"] / state_dir / f"{account}.json").is_file()


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
        raise HTTPException(status_code=400, detail="账号昵称不能为空、且不能含逗号。")
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


def _python(manifest: dict) -> Path:
    """跑这个插件用哪个解释器。

    `python = "inherit"` = 用 soroban 自己的解释器，**不建 venv、不用安装**。
    只给「依赖已在 soroban 里」的轻插件用（汇率只要 httpx）。这条是刻意留的：
    汇率是记账的必要供给方，不该让「Windows 上装不了 venv」变成「记不了账」——
    打包版 exe 在系统没有 Python 时一个插件都装不上，那时唯一能跑的就是这类。
    重依赖插件（浏览器、OCR）照旧走独立 venv，隔离仍然成立。
    """
    want = manifest.get("python", ".venv/bin/python")
    if want == "inherit":
        return Path(_base_python() or sys.executable)
    p = manifest["_dir"] / want
    if not p.exists() and os.name == "nt":          # Windows 的 venv 是 Scripts/python.exe
        alt = manifest["_dir"] / ".venv" / "Scripts" / "python.exe"
        if alt.exists():
            return alt
    return p


_needs_cache: dict[str, tuple[float, list[dict]]] = {}
_NEEDS_TTL = 60.0                                    # 秒


def needs_cached(manifest: dict) -> list[dict]:
    """`probe_needs` 的带缓存版本（60s）。

    名字必须自解释「带不带缓存」：叫 needs/_needs 时，下划线在 Python 里读作「私有」，
    读不出真正的区别，而两处调用哪一处写反都是静默故障——装完依赖用缓存版会报「仍缺依赖」，
    轮询接口用非缓存版会每次 spawn 三四个探测子进程。

    必须缓存：探测依赖要在**插件自己的解释器**里 `import`（每个模块一次子进程）再问一次
    Playwright 浏览器路径。而 GET /api/plugins 是前端**轮询**的接口——不缓存的话，每次轮询
    都要 spawn 三四个进程，装依赖时前端还在秒级轮询，直接把机器拖垮。
    安装结束会显式失效（见 _install_worker），所以不会拿着过期结论不放。"""
    key = manifest["id"]
    hit = _needs_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _NEEDS_TTL:
        return hit[1]
    val = probe_needs(manifest)
    _needs_cache[key] = (now, val)
    return val


def probe_needs(manifest: dict) -> list[dict]:
    """插件还缺哪些依赖。返回 [{key, label, hint}]；空列表 = 可以用了。

    分三档而不是笼统一句「未安装」：三者的补法完全不同（建 venv / 装 pip 包 / 下浏览器），
    用户看到「缺什么」才知道点下去会发生什么，也才看得懂失败在哪一步。
    """
    d, out = manifest["_dir"], []
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
        missing = [m for m in _declared_modules(req) if not _importable(py, m)]
        if missing:
            out.append({"key": "deps", "label": "Python 依赖",
                        "hint": "缺少：" + "、".join(missing)})
    if _wants_browser(d) and not _browser_ready(py):
        out.append({"key": "browser", "label": "浏览器内核（Chromium）",
                    "hint": "Playwright 需要一份自带的 Chromium（约 150MB），系统装的浏览器不算"})
    return out


def _declared_modules(req: Path) -> list[str]:
    """requirements.txt 的行 → 可 import 的模块名。只做最直白的解析，够本项目用。"""
    mods = []
    for line in req.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            mods.append(name.replace("-", "_"))
    return mods


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


def _importable(py: Path, module: str) -> bool:
    """在**插件自己的解释器**里试 import。不能用 soroban 的解释器判断——两个环境是隔离的。"""
    try:
        return subprocess.run([str(py), "-c", f"import {module}"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def _summarize(line: str, returncode: int) -> str:
    """把插件吐的那行 JSON 变成一句人话，放插件卡片上。

    插件之间字段不统一（爬虫回 created/updated/failed，汇率回 source/rate），
    所以取「认识的键优先，认不出就原样截断」——核心不该规定插件必须回什么，
    但也不该让用户在卡片上看一坨 JSON。
    """
    try:
        d = json.loads(line or "{}")
    except (TypeError, ValueError):
        d = {}
    if not isinstance(d, dict):
        return (line or "")[:200]
    if d.get("error"):
        return str(d["error"])[:200]
    bits = []
    for k, label in (("created", "新建"), ("updated", "更新"), ("unchanged", "无变化"),
                     ("blocked", "挡下"), ("failed", "失败")):
        if d.get(k):
            bits.append(f"{label} {d[k]}")
    if d.get("rate"):
        bits.append(f"1元 = {str(d['rate'])[:8]}円" + (f"（{d['source']}）" if d.get("source") else ""))
    if bits:
        return "、".join(bits)
    if returncode != 0:
        return f"退出码 {returncode}"
    return (line or "已完成")[:200]


def _reap(proc: subprocess.Popen, label: str, jti: Optional[str] = None, on_done=None) -> None:
    """后台收割子进程：读取其 stdout 单行 JSON 结果并写日志（成功计数 / 失败原因都落 soroban 日志）。
    插件约定 stdout 只吐一行 JSON（见各插件的 run.py），量小不会撑爆管道；30min 上限防挂死。

    收割完必须**作废令牌**：任务结束后那枚令牌不该还能用二十几分钟，
    而它此刻已经落在插件的日志与环境变量里了。放 finally 里——超时被 kill 的路径同样要作废。

    `on_done(ok, summary)` 把结果写回 PluginConfig 供界面显示。同样在 finally 里：
    超时被 kill 时也要有个交代，否则卡片会永远停在「执行中…」。
    """
    result, ok = "", False
    try:
        try:
            out, err = proc.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            log.warning("插件 %s 超时(30min)已终止", label)
            result = "超时（30 分钟）已终止"
            return
        except Exception as e:                                   # noqa: BLE001
            log.warning("插件 %s 结果回收异常：%s", label, e)
            result = f"结果回收异常：{e}"
            return
        tail = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
        line = tail[-1] if tail else ""
        ok = proc.returncode == 0
        result = _summarize(line, proc.returncode)
        if ok:
            log.info("插件 %s 完成：%s", label, line or "(无 stdout)")
        else:
            errtail = (err or "").strip()
            log.warning("插件 %s 失败(exit=%s)：%s%s", label, proc.returncode,
                        line or "(无 stdout)",
                        ("｜stderr: " + errtail[-300:]) if errtail else "")
    finally:
        scopes.revoke(jti)          # 任务结束 → 令牌立即失效
        if on_done:
            on_done(ok, result or "已完成")


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
    cmd = [str(python)] + shlex.split(manifest.get("entry", "")) + [command] + extra
    env = None
    if token or config:
        env = {**os.environ}
        if token:
            env["SOROBAN_TOKEN"] = token
        if config:
            env["SOROBAN_CONFIG"] = json.dumps(config, ensure_ascii=False)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(manifest["_dir"]), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"启动插件失败：{e}")
    acct = extra[1] if len(extra) >= 2 and extra[0] == "--account" else ""
    label = f"{manifest.get('id', '?')}/{command}" + (f" [{acct}]" if acct else "")
    # jti 传给收割线程：进程一结束就把令牌作废。否则插件跑完之后那枚令牌还能用二十几分钟，
    # 而它此刻已经落在插件的日志/环境里了。
    threading.Thread(target=_reap, args=(proc, label, jti, on_done), daemon=True).start()
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


@router.get("")
def list_plugins(session: Session = Depends(get_session)):
    out, seen_ids = [], set()
    for m in discover():
        seen_ids.add(m["_m"].id)
        cfg = session.get(PluginConfig, m["_m"].id)
        need = needs_cached(m)
        with _install_lock:
            st = dict(_install_state.get(m["_m"].id) or {})
        out.append({
            "id": m["_m"].id, "name": m["_m"].name, "version": m["_m"].version,
            "installed": not need,                  # 「装好了」= 一样不缺，而非只看 venv 在不在
            "needs": need,                          # 缺什么，逐项给前端说清
            "install": st,                           # 安装进度（running/step/error）
            "python": str(_python(m)),
            "config": {
                "enabled": bool(cfg.enabled) if cfg else False,
                "schedule_minutes": cfg.schedule_minutes if cfg else 0,
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
                 # 缺权限的命令直接禁用并说明，而不是让用户点了收 403
                 "blocked": sorted(set(c.needs) - set(json.loads(cfg.granted_scopes or "[]") if cfg else []))}
                for c in m["_m"].commands
            ],
            "manifest_error": m["_m"].error,
            # 前端据此决定要不要渲染账号区。不给这个字段的话，无账号插件的卡片上
            # 会出现「添加账号」「账号（0）」——纯噪音，还会让人以为自己漏配了什么。
            "accounts_enabled": m["_m"].accounts,
            "last_run": {
                "outcome": cfg.last_outcome if cfg else "",
                "summary": cfg.last_summary if cfg else "",
                "at": cfg.last_finished_at if cfg else None,
            },
            "scopes": {
                "declared": sorted(m.get("scopes") or []),
                "granted": sorted(json.loads(cfg.granted_scopes or "[]")) if cfg else [],
                "effective": sorted(scopes.token_scopes(m, cfg)),
                "catalog": scopes.describe(),
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
            "manifest_error": "插件目录已不在，这是库里残留的配置",
            "config": {"enabled": bool(cfg.enabled),
                       "schedule_minutes": cfg.schedule_minutes,
                       "last_run_at": cfg.last_run_at},
            "last_run": {"outcome": cfg.last_outcome, "summary": cfg.last_summary,
                         "at": cfg.last_finished_at},
            "scopes": {"declared": [], "granted": sorted(json.loads(cfg.granted_scopes or "[]")),
                       "effective": [], "catalog": scopes.describe()},
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
    if cfg is None:
        raise HTTPException(status_code=404, detail="没有这个插件的配置")
    session.delete(cfg)
    session.commit()
    log.info("已清理插件 %s 的残留配置（含授权 %s）", plugin_id, cfg.granted_scopes)
    return {"plugin_id": plugin_id, "removed": True}


@router.put("/{plugin_id}/grants")
def save_grants(plugin_id: str, payload: dict, session: Session = Depends(get_session)):
    """授予/收回插件权限。只接受清单声明过、且核心认识的项。

    传进来的多余项**静默丢弃而不是报错**：插件降级或核心删了某个 scope 之后，
    界面上那份旧勾选不该让保存整个失败——用户会以为是别的地方坏了。
    """
    m = _find_manifest(plugin_id)
    want = set(payload.get("granted") or [])
    keep = sorted(want & set(m.get("scopes") or []) & set(scopes.SCOPES))
    cfg = session.get(PluginConfig, plugin_id) or PluginConfig(plugin_id=plugin_id)
    cfg.granted_scopes = json.dumps(keep, ensure_ascii=False)
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    log.info("插件 %s 的授权改为：%s", plugin_id, keep or "（无）")
    return {"plugin_id": plugin_id, "granted": keep,
            "dropped": sorted(want - set(keep))}


@router.put("/{plugin_id}/config")
def save_config(plugin_id: str, payload: PluginConfigIn, session: Session = Depends(get_session)):
    """只存插件级设置：启用定时 + 定时间隔。账号（昵称/平台/启用）走专用增删改端点，这里不碰。"""
    _find_manifest(plugin_id)                                    # 确认插件存在
    cfg = session.get(PluginConfig, plugin_id) or PluginConfig(plugin_id=plugin_id)
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
    platform: str = Query("淘宝", description="导入平台（加时定，之后不可改）"),
    session: Session = Depends(get_session),
):
    """添加一个账号：绑定昵称 + 平台（写一次即锁定），默认启用。之后在下面列表登录授权。"""
    m = _find_manifest(plugin_id)
    name = name.strip()
    _check_account_name(m, name)                        # 非空+无逗号+合法文件名（会话文件按此名存）
    cfg = session.get(PluginConfig, plugin_id) or PluginConfig(plugin_id=plugin_id)
    accs = _account_list(cfg)
    if any(a["name"] == name for a in accs):
        raise HTTPException(status_code=409, detail=f"账号已存在：{name}")
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


@router.post("/{plugin_id}/login")
def login(plugin_id: str, account: str = Query(..., description="要授权登录的账号")):
    m = _find_manifest(plugin_id)
    _check_account_name(m, account)   # 非空+无逗号+目录穿越校验——与 add 同一把尺子，别让 login 收下非法名产生孤儿会话
    return {"started": True, "pid": _launch(m, "login", ["--account", account])}


@router.put("/{plugin_id}/params")
def save_params(plugin_id: str, payload: dict, session: Session = Depends(get_session)):
    """保存插件私有参数。清单里没声明的键丢弃（插件降级时不该让保存整体失败）。"""
    m = _find_manifest(plugin_id)
    cfg = session.get(PluginConfig, plugin_id) or PluginConfig(plugin_id=plugin_id)
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

    cfg = session.get(PluginConfig, plugin_id) or PluginConfig(plugin_id=plugin_id)
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

    if cmd.per == "account":
        pool = [a for a in _account_list(cfg) if a["enabled"]]
        if account:
            pool = [a for a in pool if a["name"] == account]
            if not pool:
                raise HTTPException(status_code=404, detail=f"没有启用的账号 {account}")
        if not pool:
            raise HTTPException(status_code=400, detail="没有可用账号：先添加账号并启用。")
        fan = [(["--account", a["name"], "--platform", a["platform"]], a["name"]) for a in pool]
    else:
        fan = [([], "")]

    token, jti = scopes.issue(current, plugin_id, granted)
    conf = {**plugin_settings(session, m), "params": plugin_params.load(mf, cfg)}
    pids = []
    for extra, who in fan:
        pids.append(_launch(m, cmd.name, [*extra, "--soroban-url", _SELF_URL],
                            token=token, config=conf, jti=jti,
                            on_done=_result_writer(plugin_id, cmd.label)))
    cfg.last_outcome, cfg.last_summary = "running", f"{cmd.label} 执行中…"
    cfg.last_finished_at = None
    cfg.updated_at = utcnow()
    session.add(cfg)
    session.commit()
    return {"launched": True, "command": cmd.name, "pids": pids,
            "targets": [w for _, w in fan if w], "scopes": sorted(granted)}


def _result_writer(plugin_id: str, label: str):
    """子进程收割完之后把结果写回 PluginConfig，供插件卡片显示。

    **开自己的 Session**：收割跑在 daemon 线程里，没有请求作用域的 session 可用。
    写失败只记日志——结果展示失败不该影响别的东西。
    """
    def done(ok: bool, summary: str) -> None:
        try:
            with Session(get_engine()) as s:
                cfg = s.get(PluginConfig, plugin_id)
                if cfg is None:
                    return
                cfg.last_outcome = "ok" if ok else "failed"
                cfg.last_summary = f"{label}：{summary}"[:512]
                cfg.last_finished_at = utcnow()
                s.add(cfg)
                s.commit()
        except Exception as e:                              # noqa: BLE001
            log.warning("写回插件 %s 的运行结果失败：%s", plugin_id, e)
    return done


@router.post("/{plugin_id}/fetch")
def fetch(
    plugin_id: str,
    account: Optional[str] = Query(None, description="仅抓该账号；不填=配置里的全部账号"),
    current: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    m = _find_manifest(plugin_id)
    cfg = session.get(PluginConfig, plugin_id)
    by_name = {a["name"]: a for a in _account_list(cfg)}
    if account:                                          # 单账号：手动抓，忽略「启用」；孤儿(磁盘授权未配置)按缺省淘宝
        _check_account_name(m, account)                  # 非空+无逗号+目录穿越校验——与 add/login 一致
        targets = [by_name.get(account) or {"name": account, "platform": "淘宝", "enabled": True}]
    else:                                                # 全部：只抓「已启用」的配置账号
        targets = [a for a in _account_list(cfg) if a["enabled"]]
    # 无账号插件（汇率、快递查询）整体跑一次；账号型插件才要求先加账号。
    # 与定时调度共用同一个判据，避免「定时能跑、手动点不动」这种两处不一致。
    if m.get("accounts") and not targets:
        raise HTTPException(status_code=400, detail="没有可抓的账号：先添加账号并启用。")
    # **限权令牌**：只带「清单声明 ∩ 用户授权 ∩ 核心已知」那几项权限，任务结束即作废。
    # 原先发的是完整用户 JWT——插件能调任何接口，包括删订单、清账本、改数据库连接。
    granted = scopes.token_scopes(m, cfg)
    token, jti = scopes.issue(current, plugin_id, granted)
    fan = ([(["--account", a["name"], "--platform", a["platform"]], a["name"]) for a in targets]
           if m.get("accounts") else [([], "")])
    pids = [_launch(m, "fetch", [*extra, "--soroban-url", _SELF_URL],
                    token=token, config=plugin_settings(session, m), jti=jti)
            for extra, _ in fan]                         # 平台按每个账号各自绑定的来源下发
    return {"launched": True, "accounts": [w for _, w in fan if w], "pids": pids,
            "scopes": sorted(granted)}


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
        raise HTTPException(status_code=400, detail="该插件不支持账号改名。")
    new = new.strip()
    if not new or "," in new:
        raise HTTPException(status_code=400, detail="新账号名不能为空、且不能含逗号（逗号是账号分隔符）。")
    _state_file(m, new)                                     # 校验 new 是合法文件名（目录穿越/非法名 → 400）
    cfg = session.get(PluginConfig, plugin_id)
    # old 有效 = 配置/磁盘里的账号，或历史数据/标签里出现过（列头改名可能改一个只存在于旧订单的账号）
    if old not in _known_names(cfg, m) and not tag_value_in_use(session, "platform_account", old):
        raise HTTPException(status_code=404, detail=f"没有这个账号：{old}")
    if new == old:
        return {"ok": True, "unchanged": True}
    if new in _known_names(cfg, m) or tag_value_in_use(session, "platform_account", new):
        raise HTTPException(status_code=409, detail=f"新名字已被占用（已有账号/数据/授权）：{new}")
    # 1) 一个事务：数据 + 标签 + 配置一起改（只改昵称，平台/启用保留）
    raw = rename_tag_value(session, "platform_account", old, new)
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
        raise HTTPException(status_code=400, detail="该插件不支持按账号删除订单。")
    cfg = session.get(PluginConfig, m["id"])
    if account not in _known_names(cfg, m) and not tag_value_in_use(session, "platform_account", account):
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
    deleted, skipped = delete_account_staging(session, account)
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
    n = soft_delete_account_orders(session, account)
    session.commit()
    return {"ok": True, "deleted": n}


# --- 定时调度：按每个启用插件的 schedule_minutes 周期触发 fetch --------------

def _due(last: Optional[dt.datetime], minutes: int, now: dt.datetime) -> bool:
    if last is None:
        return True
    if last.tzinfo is None:                             # SQLite 取回可能是 naive，统一按 UTC 处理
        last = last.replace(tzinfo=dt.timezone.utc)
    return (now - last).total_seconds() >= minutes * 60


def _fanout(manifest: dict, cfg) -> list[tuple[list[str], str]]:
    """一次定时触发要起几个子进程，各带什么参数。

    返回 [(附加参数, 日志里的标识)]。

    **两类插件**：
      · `accounts = true`（如淘宝）——一个账号一个进程，各带自己的 cookie 与平台。
      · 不声明（如汇率、快递查询）——**整体跑一次**，不带 --account。

    以前这里只有前一种：账号列表为空 → 一个都不起 → `launched` 恒为 0 →
    `last_run_at` 永不推进 → 这类插件**永远不会被定时触发**，而且界面上看不出异常
    （它「已启用」、有定时周期，只是从不运行）。无账号插件是本次要支持的主要形态，
    所以这条分支必须存在。
    """
    if not manifest.get("accounts"):
        return [([], "")]
    out = []
    for a in _account_list(cfg):
        if not a["enabled"]:                            # 停用的账号：定时跳过
            continue
        out.append((["--account", a["name"], "--platform", a["platform"]], f"/{a['name']}"))
    return out


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
        launched = 0
        for extra, who in _fanout(m, cfg):
            try:
                tok, jti = scopes.issue(user, cfg.plugin_id, scopes.token_scopes(m, cfg))
                _launch(m, "fetch", [*extra, "--soroban-url", _SELF_URL],
                        token=tok, config=plugin_settings(session, m), jti=jti)
                launched += 1
            except HTTPException as e:
                log.warning("定时任务 %s%s 启动失败：%s", cfg.plugin_id, who, e.detail)
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
