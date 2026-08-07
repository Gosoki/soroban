"""爬虫插件（soroban 做管理层）。

soroban 扫 SCRAPER_DIR 下的 `soroban-scraper-*` 目录（各含 plugin.toml）作为插件，负责：
发现、存配置/定时、触发登录/抓取。插件本体是独立进程/venv，soroban 只按 manifest 调它的标准 CLI。
调用为子进程；抓取时把 soroban 短期 token 下发给插件回灌，插件无需存 soroban 密码。
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
from sqlmodel import Session, select

from ..auth import create_access_token, get_current_user
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


def scraper_dir() -> Path:
    return Path(settings.SCRAPER_DIR) if settings.SCRAPER_DIR else (_SOROBAN_ROOT / "scraper")


def discover() -> list[dict]:
    """扫描插件目录，读各 plugin.toml。返回 manifest 列表（附 _dir）。坏的跳过。"""
    base, out = scraper_dir(), []
    if not base.is_dir():
        return out
    for d in sorted(base.glob("soroban-scraper-*")):
        f = d / "plugin.toml"
        if not (d.is_dir() and f.is_file()):
            continue
        try:
            m = tomllib.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "id" not in m:
            continue
        m["_dir"] = d
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
    p = manifest["_dir"] / manifest.get("python", ".venv/bin/python")
    if not p.exists() and os.name == "nt":          # Windows 的 venv 是 Scripts/python.exe
        alt = manifest["_dir"] / ".venv" / "Scripts" / "python.exe"
        if alt.exists():
            return alt
    return p


_needs_cache: dict[str, tuple[float, list[dict]]] = {}
_NEEDS_TTL = 60.0                                    # 秒


def needs(manifest: dict) -> list[dict]:
    """`_needs` 的带缓存版本。

    必须缓存：探测依赖要在**插件自己的解释器**里 `import`（每个模块一次子进程）再问一次
    Playwright 浏览器路径。而 GET /api/plugins 是前端**轮询**的接口——不缓存的话，每次轮询
    都要 spawn 三四个进程，装依赖时前端还在秒级轮询，直接把机器拖垮。
    安装结束会显式失效（见 _install_worker），所以不会拿着过期结论不放。"""
    key = manifest["id"]
    hit = _needs_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _NEEDS_TTL:
        return hit[1]
    val = _needs(manifest)
    _needs_cache[key] = (now, val)
    return val


def _needs(manifest: dict) -> list[dict]:
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


def _reap(proc: subprocess.Popen, label: str) -> None:
    """后台收割子进程：读取其 stdout 单行 JSON 结果并写日志（成功计数 / 失败原因都落 soroban 日志）。
    插件约定 stdout 只吐一行 JSON（见 taobao_scraper/run.py），量小不会撑爆管道；30min 上限防挂死。"""
    try:
        out, err = proc.communicate(timeout=1800)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        log.warning("插件 %s 超时(30min)已终止", label)
        return
    except Exception as e:                                   # noqa: BLE001
        log.warning("插件 %s 结果回收异常：%s", label, e)
        return
    tail = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
    result = tail[-1] if tail else "(无 stdout)"
    if proc.returncode == 0:
        log.info("插件 %s 完成：%s", label, result)
    else:
        errtail = (err or "").strip()
        log.warning("插件 %s 失败(exit=%s)：%s%s", label, proc.returncode, result,
                    ("｜stderr: " + errtail[-300:]) if errtail else "")


def _launch(manifest: dict, command: str, extra: list[str], token: Optional[str] = None) -> int:
    """子进程调插件 CLI（fire-and-forget；返回 pid，后台线程收割其结果写日志）。

    token 走**环境变量** SOROBAN_TOKEN 下发，不进 argv——避免短期凭据出现在进程表(ps)/日志里。
    """
    python = _python(manifest)
    if not python.exists():
        raise HTTPException(status_code=400, detail=f"插件未安装：缺 venv（{python}）。见插件 README。")
    cmd = [str(python)] + shlex.split(manifest.get("entry", "")) + [command] + extra
    env = {**os.environ, "SOROBAN_TOKEN": token} if token else None
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
    threading.Thread(target=_reap, args=(proc, label), daemon=True).start()
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
    remaining = _needs(manifest)
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
    out = []
    for m in discover():
        cfg = session.get(PluginConfig, m["id"])
        need = needs(m)
        with _install_lock:
            st = dict(_install_state.get(m["id"]) or {})
        out.append({
            "id": m["id"], "name": m.get("name", m["id"]), "version": m.get("version"),
            "installed": not need,                  # 「装好了」= 一样不缺，而非只看 venv 在不在
            "needs": need,                          # 缺什么，逐项给前端说清
            "install": st,                           # 安装进度（running/step/error）
            "python": str(_python(m)),
            "config": {
                "enabled": bool(cfg.enabled) if cfg else False,
                "schedule_minutes": cfg.schedule_minutes if cfg else 0,
                "last_run_at": cfg.last_run_at if cfg else None,
            },
            "accounts": _display_accounts(cfg, m),
        })
    return out


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
    if not targets:
        raise HTTPException(status_code=400, detail="没有可抓的账号：先添加账号并启用。")
    token = create_access_token(current, dt.timedelta(minutes=30))   # 真·短期 token（30min，够抓一次），走环境变量不进 argv
    pids = [_launch(m, "fetch",
                    ["--account", a["name"], "--soroban-url", _SELF_URL, "--platform", a["platform"]],
                    token=token) for a in targets]       # 平台按每个账号各自绑定的来源下发
    return {"started": True, "accounts": [a["name"] for a in targets], "pids": pids}


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
    if m.get("platform") != "taobao":                       # 账号↔platform_account 的耦合是淘宝专属；别的插件先不支持
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
    if m.get("platform") != "taobao":
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
        token = token or create_access_token(user, dt.timedelta(minutes=30))
        launched = 0
        for a in _account_list(cfg):
            if not a["enabled"]:                        # 停用的账号：定时跳过
                continue
            try:
                _launch(m, "fetch",
                        ["--account", a["name"], "--soroban-url", _SELF_URL, "--platform", a["platform"]],
                        token=token)
                launched += 1
            except HTTPException as e:
                log.warning("定时抓取 %s/%s 启动失败：%s", cfg.plugin_id, a["name"], e.detail)
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
