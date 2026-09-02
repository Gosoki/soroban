"""插件子进程这一层：**起进程 → 收割 → 读它说了什么**。

从 `routers/plugins.py` 整体搬出来的（那个文件到 2300 行、七类职责，而这一组是其中
最内聚、也最与 HTTP 无关的一块）。**搬移时一行行为都没改**，注释里记着的每一次事故同样原样带过来。

这一层负责的三件事，边界很清楚：
  · **互斥**：同一个账号同时只许有一个子进程（`_INFLIGHT` / `_run_key`）；
  · **进程组**：起进程、收割、连孙进程一起收（`_ALIVE_PROCS` / `_OWN_GROUP` / `_kill_tree`）；
  · **读输出**：把插件吐的那行 JSON 变成卡片上的一句人话（`_summarize`）。

它**不碰**：HTTP、数据库、批次聚合、卡片写回。那些留在 `routers/plugins.py`——
`_reap` 通过一个 `on_done` 回调把结果交回去，是这两层之间唯一的接口。

⚠️ `routers/plugins.py` 仍然 `from .plugins_proc import *`（显式列名）再导出这些名字：
测试与 `main.py` 大量按 `plugins._INFLIGHT` / `plugins.shutdown_plugins` 这样引用，
而且 `monkeypatch.setattr(mod, "_launch", …)` 这类替身依赖「名字在 plugins.py 上」。
再导出的是**同一个对象**，所以 `_INFLIGHT.clear()` 之类跨模块照常生效。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Optional

from ..plugins import scopes

log = logging.getLogger("soroban.plugins")

# 子进程最长跑多久。**令牌 TTL 必须由它推导**（见每个 scopes.issue 的 timeout_s）：
# 原先 issue 全部走默认 600s → TTL 恒为 12 分钟，而这里等 30 分钟。
# 任何超过 12 分钟的抓取，最后那次回灌必然 401，整批订单静默丢失——单账号也中。
# 两个数字分别写在两个文件里，就是这么长出来的。
_REAP_TIMEOUT = 1800


# 卡片上认识的计数键 → 中文标签。顺序即显示顺序。
_COUNTERS = (("created", "新建"), ("updated", "更新"), ("unchanged", "无变化"),
             ("skipped", "跳过"), ("blocked", "挡下"), ("failed", "失败"),
             ("rejected", "拒收"))
# 核心认识、但本身不构成「有话要说」的键。
# `account` 必须在里面：淘宝插件**每一行**都吐 `{"ok":…, "account": args.account, **res}`，
# 而账号名已经由 `_batch_text` 拼在整句最前面了——不排除它的话，
# 卡片会变成「甲 ✓ 新建 3、更新 1｜account=甲」，同一个名字出现两遍。
_KNOWN_KEYS = frozenset({"ok", "error", "logged_in", "rate", "source", "account"}) | {
    k for k, _ in _COUNTERS}


def _extra_notes(d: dict) -> str:
    """插件说了、而核心不认识的那些话。

    **只取值是非空字符串的键**：那是插件写给人看的一句话（如汇率插件 `probe` 的
    `note: "没有 SOROBAN_TOKEN，只取不交"`）。布尔与列表一律跳过——
    原先无差别地 `f"{k}={d[k]}"`，于是中文卡片上会出现 `pushed=True`、`tried=['boc']`
    这种 Python 字面量，而它们对用户没有任何意义。
    """
    out = [str(v).strip() for k, v in d.items()
           if k not in _KNOWN_KEYS and isinstance(v, str) and v.strip()]
    return "；".join(out)[:120]


def _summarize(line: str, returncode: int, errtail: str = "") -> str:
    """把插件吐的那行 JSON 变成一句人话，放插件卡片上。

    插件之间字段不统一（爬虫回 created/updated/failed，汇率回 source/rate），
    所以取「认识的键优先，认不出就原样截断」——核心不该规定插件必须回什么，
    但也不该让用户在卡片上看一坨 JSON。

    `errtail` 是子进程 stderr 的尾巴，**只在没别的可说时**才用：插件崩在
    import 阶段（缺依赖、解释器不对）时 stdout 是空的、returncode 非 0，
    原先卡片上只有一句「退出码 1」——真正的原因（ModuleNotFoundError 之类）
    全在 stderr 里，而用户看不到日志文件。这正是打包版汇率插件的失败形态。
    """
    try:
        d, parsed = json.loads(line or "{}"), True
    except (TypeError, ValueError):
        # **这一支要和「line 是空的」分开记。** 两者都得到 d={}，而它们该走不同的结局：
        # 解析不了的原样显示（那是插件唯一能说的话），空的才叫「已完成」。
        d, parsed = {}, False
    if not isinstance(d, dict):
        return (line or "")[:200]
    if d.get("error"):
        return str(d["error"])[:200]
    bits = []
    # skipped/logged_in 是淘宝插件最常见的两种结果（本轮无变化 / 登录成功），
    # 原先不在表里 → 落到最后一支，卡片上显示的是原始 JSON。
    for k, label in _COUNTERS:
        if d.get(k):
            bits.append(f"{label} {d[k]}")
    if d.get("logged_in"):
        bits.append("登录成功")
    if d.get("rate"):
        bits.append(f"1元 = {str(d['rate'])[:8]}円" + (f"（{d['source']}）" if d.get("source") else ""))
    notes = _extra_notes(d)
    if bits:
        # **认识的键说完之后，别把不认识的悄悄吞掉。**
        # 汇率插件的 `probe`（「只测试、不写入」）回的是
        #   {"ok":true,"source":"boc","rate":"21.03","pushed":false,"note":"没有 SOROBAN_TOKEN，只取不交"}
        # `rate` 命中就提前返回 ⇒ `note` 消失 ⇒ 卡片上「只测不写」和一次真正成功的写入
        # **显示同一句话**，而 probe 存在的全部理由就是排查「取不到汇率」。
        return "、".join(bits) + (f"｜{notes}" if notes else "")
    if returncode != 0:
        # stderr 的**最后一行非空内容**通常就是异常那一行，比整段栈更适合放卡片。
        last = next((ln.strip() for ln in reversed((errtail or "").splitlines()) if ln.strip()), "")
        return f"退出码 {returncode}" + (f"：{last[:160]}" if last else "")
    # 到这里说明：退出码 0，而认识的计数键**一个非零的都没有**。
    # 上面那个循环用的是真值判断，所以 `{"created": 0, "updated": 0, "skipped": 0}`
    # ——定时抓取最常见的结局，跑完了确实没新东西——一条 bits 都不产生，
    # 原先直接落到最后一行，把整坨 JSON 显示在卡片上（正是本函数要避免的那件事）。
    #
    # ⚠️ 判据**不能**是「这行 JSON 里没有核心不认识的键」。第一版就是那么写的，
    # 而淘宝插件每一行都带着 `account` ⇒ 差集恒非空 ⇒ 这一支对**真正的生产者**
    # 是死代码，而配套用例手工去掉了 `account`（那个形状没有任何插件会产生）——
    # 一条把自己测绿了的假绿。判据改成「计数键出现过」，那才是「这一轮跑完了」的信号；
    # 不认识的话由 `_extra_notes` 单独接住，不会丢。
    if parsed and any(k in d for k, _ in _COUNTERS):
        return "本轮无变化" + (f"｜{notes}" if notes else "")
    if parsed and not set(d) - _KNOWN_KEYS:
        return "已完成"
    return (line or "已完成")[:200]


def _self_reported_error(line: str) -> bool:
    """插件在它那行 JSON 里自己说「出事了」吗（`{"error": ...}`）。

    与退出码是**两个独立的信号**，都要看：
      · 退出码是跨进程契约（非零=失败），插件作者会刻意用 0 表达「本次没什么可做」；
      · JSON 里的 error 是插件自己的交代，可能伴随 0 退出码（部分成功、软跳过）。
    只信前者会把「跑完了但有一半没抓到」显示成绿色的「成功」——而用户看到绿字
    就不会再去点开摘要，那句话里恰恰写着出了什么事。
    """
    try:
        d = json.loads(line or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(d, dict) and bool(d.get("error"))


_MAX_CAPTURE = 256 * 1024        # 每路输出最多留末尾 256KB

# 在飞的插件子进程：pid → (Popen, 标签)。进程关停时要连它们一起收掉。
#
# 为什么必须有：`_launch` 用 `start_new_session=True` 起进程（新会话，见 _kill_tree），
# 而收割线程是 daemon —— 主进程一退出，收割线程立刻消失，子进程却还活着，
# 变成 PPID=1 的孤儿，且**再没有任何人执行那个 30 分钟超时**。
# 对浏览器类插件这意味着一个 chromium 永久留在后台：用户「关掉了 soroban」，
# 内存里却还躺着几百 MB，而任务管理器里那个进程与 soroban 已经毫无关联，没人猜得到。
_ALIVE_PROCS: dict[int, tuple] = {}
# 起进程时**验证过**「它自己就是进程组组长」的那些 pid（pgid == pid）。
#
# 为什么必须在子进程还活着的时候验、并且记下来：一旦它被回收，`os.getpgid(pid)` 就
# 查不到了（实测 `[Errno 3] No such process`）——而那恰恰是最需要这个信息的时刻。
# 典型场景：插件跑完自己退了、或者崩了，而它拉起的 chromium 还开着。此时
#   · `_reap` 已经 wait 到了子进程 ⇒ 它被移出 `_ALIVE_PROCS`；
#   · `shutdown_plugins` 的 `if proc.poll() is None` 判假 ⇒ 整个 `_kill_tree` 跳过；
#   · 就算调到 `_kill_tree`，它第一步 `os.getpgid(proc.pid)` 也已经失败，
#     走的是「只杀单个进程」的降级分支，而那个进程早没了。
# 三层叠加的结果是：孙进程从子进程退出的那一刻起彻底失联，**不只是关停时**。
# 用户机器上留下一个与 soroban 已无任何关联的 chromium，没人猜得到该去杀谁。
#
# 进程组在还有成员时就一直存在，组长死了不影响——所以拿这个记下来的 pgid 照样杀得掉（已实测）。
_OWN_GROUP: set[int] = set()
_PROCS_LOCK = threading.Lock()
# 在飞的「插件/命令 [账号]」键。同一个键同时只许有一个子进程——
# 项目自己把「同账号并发多开浏览器」写成风控红线（见 scheduler_loop 的说明、
# 以及淘宝插件仓的 docs/风控与对策.md），而此前**核心一侧一道闸都没有**：
# 「授权登录」按钮连点三下就是三个有头 chromium 同时打开同一个淘宝账号。
# 互斥原先被推给每个插件各自实现（淘宝插件的 _account_lock 只包 fetch，login 没包），
# 那等于把一条安全边界交给第三方代码去记得。
_INFLIGHT: dict[str, str] = {}      # 互斥键 → 正占着它的那个标签（谁在跑）


class PluginBusy(RuntimeError):
    """同一插件/命令/账号已有一个进程在飞。由 run_command 决定是跳过还是 409。"""


def _run_account(extra: list[str]) -> str:
    """这次调用针对哪个账号（`--account 甲`）。不是按账号跑的命令返回空串。"""
    return extra[1] if len(extra) >= 2 and extra[0] == "--account" else ""


def _run_key(manifest: dict, command: str, extra: list[str]) -> str:
    """互斥键。**按账号跑的命令，键里不带命令名——一个账号同时只能有一个进程。**

    原先键是「插件/命令 [账号]」，于是 `fetch [甲]` 与 `login [甲]` 是两把不同的锁，
    同一个账号可以同时起两个进程。它们抢的是同一份东西：`state/甲.json`（登录会话）。
    实际后果是登录白做——login 写完新会话，正跑着的 fetch 拿的仍是启动时读到的旧的，
    退出时又按自己那份覆盖回去；坏一点的时候两边同时写，会话文件直接损坏，
    表现为「刚登录成功，抓取却说没登录」，而日志里两条命令各自都是成功的。
    插件侧的 `_account_lock` 只包 fetch，挡不住这一对。

    不按账号跑的命令（per 不是 account）沿用「插件/命令」，它们之间本来就无关。
    """
    acct = _run_account(extra)
    return f"{manifest.get('id', '?')} [{acct}]" if acct else f"{manifest.get('id', '?')}/{command}"


def _run_label(manifest: dict, command: str, extra: list[str]) -> str:
    """给人看的标签（日志、卡片、409 文案）：带命令名，才说得清「谁占着」。

    与 `_run_key` 分开是因为两者的粒度必须不同：键要粗到能互斥，标签要细到能读懂。
    合成一个的话，要么互斥漏掉（原样），要么日志里所有账号级命令长得一模一样。
    """
    acct = _run_account(extra)
    return f"{manifest.get('id', '?')}/{command}" + (f" [{acct}]" if acct else "")


def _drain(stream, sink: list) -> None:
    """把一路输出读到 EOF，内存里**只留末尾 _MAX_CAPTURE 字节**。

    话多的插件（playwright 的 debug 日志能到上百 MB）不该把后端的内存吃掉，
    而按插件约定有用的只有尾巴：stdout 的最后一行是结果 JSON，stderr 的末尾是报错栈。

    这个函数**可能永远返回不了**——孙进程继承着写端时 EOF 不会来。这没关系：
    调用方用 join(timeout) 等它，等不到就当「拿不到输出」，daemon 线程随进程退出。
    """
    try:
        for chunk in iter(lambda: stream.read(8192), ""):
            if not chunk:
                break
            sink.append(chunk)
            while len(sink) > 1 and sum(map(len, sink)) > _MAX_CAPTURE:
                sink.pop(0)
    except Exception:                                            # noqa: BLE001
        pass                                                     # 管道被 kill 掐断是正常路径
    finally:
        try:
            stream.close()
        except Exception:                                        # noqa: BLE001
            pass


def _remember_group(pid: int, label: str) -> None:
    """记下「这个 pid 自己就是进程组组长」。调用方须持 `_PROCS_LOCK`，且**子进程必须还活着**。

    `_launch` 用 `start_new_session=True` 起进程，所以这一条**本该**恒成立。
    仍然要验一次：万一它在某个平台上没生效（老平台、被 patch、被 mock），
    pgid 会是继承来的父进程组——那时对着这个 pgid 发 killpg 会把**后端自己**一起带走。
    验不过就不记，后面所有按组回收的动作自动降级成只动单个进程。
    """
    if os.name == "nt":
        return                          # Windows 没有进程组语义，见 _kill_tree
    try:
        pgid = os.getpgid(pid)
    except OSError as e:                # 极短命的进程可能已经退了
        log.warning("插件 %s 起来后取不到进程组（pid=%s）：%s"
                    "——它拉起的孙进程将无法按组回收", label, pid, e)
        return
    if pgid != pid:
        log.error("插件 %s 的进程组是 %s、不是它自己（pid=%s）：start_new_session 没生效。"
                  "本次不按组回收——按组杀会波及后端自己所在的进程组。", label, pgid, pid)
        return
    _OWN_GROUP.add(pid)


def _group_has_members(pgid: int) -> bool:
    """这个进程组里还有活着的成员吗（信号 0 = 只探测不发信号）。"""
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False


def _sweep_group(pid: int, label: str, why: str) -> int:
    """子进程已经退出之后，把它进程组里的**残余成员**（孙进程）收掉。返回收了几轮。

    这是 `_kill_tree` 够不着的那一半：`_kill_tree` 要求直接子进程还活着
    （它靠 `os.getpgid(proc.pid)` 定位组），而这里处理的正是「子进程先退、孙进程还在」。

    **无论收没收到都留日志**：收到了要说清收了谁（否则用户永远不知道自己机器上
    曾经躺过一个浏览器）；没收到也值得记一行 debug，好让「这条路径到底跑没跑过」可查。

    ⚠️ pid 会被系统回收复用，所以这个 pgid **只在有界窗口内可用**：
    收割线程 wait 到子进程就用一次、收尾时再兜一次，以及关停时用一次。绝不长期挂着。
    """
    if pid not in _OWN_GROUP:
        # 正常路径上这一支**每次都会走到**：上面那次（"子进程刚退出"）已经扫过并摘牌，
        # finally 里那次兜底就落在这里。所以措辞不能读着像出错。
        log.debug("插件 %s 无需按组回收：已扫过或未验证过进程组（%s）", label, why)
        return 0
    _OWN_GROUP.discard(pid)
    if pid == os.getpgid(0):            # 双保险：永远不动后端自己所在的组
        log.error("插件 %s 的 pgid 与后端自身相同（%s），拒绝按组回收", label, pid)
        return 0
    if not _group_has_members(pid):
        log.debug("插件 %s 的进程组已空，无需回收（%s）", label, why)
        return 0
    log.warning("插件 %s 退出后其进程组 %s 里仍有存活进程（%s）——正在回收，"
                "否则它们会变成与 soroban 无关联的孤儿", label, pid, why)
    rounds = 0
    for sig, wait in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 0.5)):
        try:
            os.killpg(pid, sig)
            rounds += 1
        except OSError as e:            # 组刚好空了
            log.info("插件 %s 的进程组 %s 已无成员（%s）", label, pid, e)
            return rounds
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _group_has_members(pid):
                log.info("插件 %s 的残余进程已被 %s 回收干净", label, sig.name)
                return rounds
            time.sleep(0.05)
    if _group_has_members(pid):
        log.error("插件 %s 的进程组 %s 在 SIGKILL 之后仍有成员——放弃", label, pid)
    return rounds


def _kill_tree(proc: subprocess.Popen, label: str) -> None:
    """终止插件子进程**连同它 fork 出来的孙进程**。

    `_launch` 用 `start_new_session=True` 起进程，所以子进程是一个**新会话/新进程组的
    组长**，pgid == 它自己的 pid。杀这个组正好覆盖它拉起的浏览器等孙进程，
    且**不可能**波及本进程所在的组（uvicorn / 终端 / 其它无关进程）。

    三重保险，缺一不可：
      · pgid 必须等于 proc.pid —— 组长身份是我们自己创建时保证的。万一
        start_new_session 没生效（老平台/被 patch），pgid 会是继承来的父进程组，
        那时 killpg 会把**后端自己**一起带走。此时只杀单个进程。
      · pgid 必须不等于本进程的组。前一条已经蕴含这一条，但显式写出来，
        免得将来有人改了启动参数却没改这里。
      · 任何异常都吞掉：进程可能刚好自己退了（ProcessLookupError）。
    """
    if os.name == "nt":
        # Windows 没有进程组语义可用（start_new_session 在 nt 上是 no-op）。
        # taskkill /T 按 PID 精确地连子树一起结束，不涉及任何按名匹配。
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, check=False)
        except Exception as e:                                   # noqa: BLE001
            log.warning("插件 %s taskkill 失败：%s", label, e)
        proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
        if pgid == proc.pid and pgid != os.getpgid(0):
            os.killpg(pgid, signal.SIGKILL)
            return
        log.error("插件 %s 的进程组是 %s（期望 %s）——只杀单个进程，不动整组",
                  label, pgid, proc.pid)
    except Exception as e:                                       # noqa: BLE001
        log.warning("插件 %s 取进程组失败：%s", label, e)
    try:
        proc.kill()
    except Exception:                                            # noqa: BLE001
        pass


def _reap(proc: subprocess.Popen, label: str, key: str,
          jti: Optional[str] = None, on_done=None) -> None:
    """后台收割子进程：读取其 stdout 单行 JSON 结果并写日志（成功计数 / 失败原因都落 soroban 日志）。
    插件约定 stdout 只吐一行 JSON（见各插件的 run.py），量小不会撑爆管道；30min 上限防挂死。

    收割完必须**作废令牌**：任务结束后那枚令牌不该还能用二十几分钟，
    而它此刻已经落在插件的日志与环境变量里了。放 finally 里——超时被 kill 的路径同样要作废。

    `on_done(ok, summary)` 把结果写回 PluginConfig 供界面显示。同样在 finally 里：
    超时被 kill 时也要有个交代，否则卡片会永远停在「执行中…」。
    """
    result, ok, warn = "", False, False
    outs: list[str] = []
    errs: list[str] = []
    try:
        # **不用 communicate()**，两个原因，都不是理论问题：
        #
        # 1. communicate() 等的是「管道到达 EOF」，不是「子进程退出」。浏览器类插件会
        #    fork 出 chromium 之类的孙进程，它们**继承**着同一对管道；直接子进程被 kill
        #    之后孙进程还攥着写端，EOF 永远不来 → 超时分支里那句无参 communicate()
        #    自己挂死在收割线程里，令牌不作废、卡片永远停在「执行中…」。
        #    改成 wait()（只等子进程）+ 独立的排空线程（可以放弃等待）。
        # 2. communicate() 把 stdout/stderr 全量存进内存。插件日志走的正是 stderr，
        #    一个话多的插件就能把后端 RSS 顶上去。_drain 只留末尾 _MAX_CAPTURE 字节——
        #    而按插件约定，有用的东西（结果 JSON、报错栈）恰好都在尾巴上。
        t_out = threading.Thread(target=_drain, args=(proc.stdout, outs), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, errs), daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=_REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, label)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:                    # 僵而不死（D 状态等）
                log.error("插件 %s 已发 KILL 但仍未退出，放弃收割", label)
            log.warning("插件 %s 超时(%d 秒)已终止", label, _REAP_TIMEOUT)
            # **把它挂住之前说的最后一句话带上。**
            # 原先这里只报「超时（30 分钟）已终止」——一句没有信息量的结论：
            # 用户等了半小时，仍然不知道它卡在哪。而 `errs` 里攒着的恰恰是唯一的线索
            # （「正在等待浏览器登录…」「已加载第 3 页」之类），此前整个被丢掉。
            # join **必须有上限**：孙进程攥着写端时 EOF 永远不来（见 `_drain`），
            # 而这里已经是收尾路径，多等一秒都是卡片多顶一秒「执行中」。
            # 也**必须先 join 再读**：`_drain` 会 `pop(0)` 裁剪，边写边读会读到撕裂的内容。
            t_err.join(2)
            stuck = next((ln.strip() for ln in reversed("".join(errs).splitlines())
                          if ln.strip()), "")
            result = (f"超时（{_REAP_TIMEOUT // 60} 分钟）已终止"
                      + (f"｜挂住前最后一条输出：{stuck[-160:]}" if stuck else ""))
            return
        except Exception as e:                                   # noqa: BLE001
            log.warning("插件 %s 结果回收异常：%s", label, e)
            result = f"结果回收异常：{e}"
            return
        # **孙进程在这里就收掉，不要等到下面的 finally。**
        # 下面两个 join(5) 恰恰是因为「孙进程还攥着管道、EOF 不来」才存在的——
        # 于是 finally 里那次回收实测要晚 10.05 秒。那 10 秒里：`_INFLIGHT` 还攥着
        # （用户点「再跑一次」吃 409）、卡片多顶 10 秒「执行中」、令牌多活 10 秒，
        # 而那个孙进程（chromium）也多开 10 秒。
        # 顺带把 join 变快：孙进程一走 EOF 就来了。
        # `finally` 里那一句**必须保留**——超时支与异常支都从上面 return 出去，
        # 那是它们唯一的按组回收点，也是 `_OWN_GROUP` 唯一的释放点。两次调用天然幂等。
        _sweep_group(proc.pid, label, "子进程刚退出")
        # 给排空线程一点时间收尾，但**绝不无限等**——拿不到输出不等于不能收割。
        t_out.join(5)
        t_err.join(5)
        out, err = "".join(outs), "".join(errs)
        tail = [ln for ln in (out or "").strip().splitlines() if ln.strip()]
        line = tail[-1] if tail else ""
        ok = proc.returncode == 0
        errtail = (err or "").strip()
        result = _summarize(line, proc.returncode, errtail)
        # **退出码是跨进程契约，不动它**：淘宝插件的 `already_running` 刻意 return 0，
        # 那是「这次没什么可做的」而不是失败，改成信 JSON 会把它刷成红色。
        # 但「退出码 0 且自报了 error」也不该显示成绿色的「成功」——
        # 用户看到绿字就不会再去点开摘要，而那句话里写着出了什么事。
        # 所以加第三档：成败仍由退出码定，颜色多一档黄。
        warn = ok and _self_reported_error(line)
        if ok:
            log.info("插件 %s 完成：%s", label, line or "(无 stdout)")
        else:
            log.warning("插件 %s 失败(exit=%s)：%s%s", label, proc.returncode,
                        line or "(无 stdout)",
                        ("｜stderr: " + errtail[-300:]) if errtail else "")
    finally:
        with _PROCS_LOCK:
            _ALIVE_PROCS.pop(proc.pid, None)
            _INFLIGHT.pop(key, None)            # 释放互斥键，这个账号可以再跑了
        # **子进程退出的这一刻，是回收它孙进程的最后时机。**
        # 过了这里它就被移出注册表，关停时也不会再看它一眼——而它拉起的 chromium
        # 可能还开着。放在锁外：扫一次最多等 2 秒，不该把别的启动/关停挡在门外。
        _sweep_group(proc.pid, label, "收尾兜底")
        scopes.revoke(jti)          # 任务结束 → 令牌立即失效
        if on_done:
            on_done(ok, result or "已完成", warn)


def shutdown_plugins(grace: float = 3.0) -> int:
    """进程关停时收掉所有在飞的插件子进程。返回终止了几个。

    放在 lifespan 的 finally 里。不做这件事的话，子进程会变成 PPID=1 的孤儿：
    收割线程是 daemon（随主进程消失），于是那个 30 分钟超时**再没有人执行**，
    浏览器类插件的 chromium 会一直留在后台——用户以为关掉了 soroban，
    而任务管理器里那个进程与 soroban 已经毫无关联，没人猜得到该去杀谁。

    先 SIGTERM 给一点体面退出的时间（插件可能正在写 .state 会话文件），
    过了 grace 再走 `_kill_tree`（连孙进程一起，见其中的三重保险）。
    """
    with _PROCS_LOCK:
        alive = list(_ALIVE_PROCS.values())
        _ALIVE_PROCS.clear()
    if not alive:
        return 0
    log.info("关停：正在收掉 %d 个在飞的插件子进程", len(alive))
    for proc, label in alive:
        try:
            proc.terminate()                    # SIGTERM：给它自己收尾的机会
        except Exception:                       # noqa: BLE001  可能刚好自己退了
            pass
    deadline = time.monotonic() + grace
    for proc, label in alive:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:                       # noqa: BLE001  超时/已回收
            pass
    for proc, label in alive:
        if proc.poll() is None:
            log.warning("插件 %s 未在 %.1fs 内退出，强制终止其进程组", label, grace)
            _kill_tree(proc, label)
        else:
            # **原先这一支是空的**——直接子进程已经退了就当没事，
            # 而它拉起的孙进程（浏览器）此刻还在，且从此再没有任何人管它。
            log.info("插件 %s 的直接子进程已退出，检查其进程组有无残余", label)
        _sweep_group(proc.pid, label, "关停")
    return len(alive)
