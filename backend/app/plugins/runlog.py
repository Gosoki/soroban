"""一次插件运行里**核心侧**看到的事实——与插件自报的内容分开记。

**为什么必须分开**：卡片上显示的是插件自己 print 的那一句。一个不看回执的插件——
推 30 条、核心逐条拒收 30 条、照常 `print({"ok": true, "created": 30})` 并 `exit 0`——
会让卡片显示绿色的「已导入 30 单」，而库里**零写入**。
拒收信息其实一直都返回给插件了（`/api/ingest` 的 `summary` + 逐项 `results`），
核心也一直记着日志，但用户看不到日志，他只看卡片。

仓库里那个汇率插件被改成了会判回执（零写入即 `ok:False` + 非零退出码），
但那是**插件的自觉，不是核心的强制**：换一个第三方插件、或者哪天有人重写它时
忘了那段，同一个洞立刻复发。所以这里记的是核心自己看到的事实，
渲染卡片时它**优先于**插件的自报——插件说什么都盖不掉。

进程内、按 run（令牌 jti）聚合。一次运行的寿命远短于进程，故不落库、不需要迁移；
带上限，免得一个疯狂重试的插件把它撑大。
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("soroban.plugins.runlog")

# 同时最多记多少个 run。正常情况这里只有个位数（在飞的子进程数）。
# 上限兜的是两种「没人来取」：
#   · `"manual"` 那个桶——人类令牌手工调 ingest 时没有 jti，也就没有对应的卡片，
#     没有任何收尾流程会去取它；
#   · take 之后迟到的 `note_rejected`（子进程已收尾、回灌请求还在路上），
#     它会 setdefault 出一条新记录，同样没人再来取。
# （原先这里举的例子是「插件进程被 kill」——那个不成立：kill 路径下
#   `_reap` 的 finally 照样调 on_done，照样会 take。）
_MAX_RUNS = 256

_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}


def note_rejected(run: str, plugin_id: str, kind: str, count: int, first_message: str) -> None:
    """记一次「核心拒收了 N 条」。同一个 run 多次写入按累加。

    `run` 是令牌 jti（手动/定时都有）。拿不到 jti 的路径（人类令牌手工调 ingest）
    传的是 "manual"，那种情况下没有对应的卡片，累加在一起也无害。
    """
    if not run or count <= 0:
        return
    dropped = None
    with _LOCK:
        if run not in _RUNS and len(_RUNS) >= _MAX_RUNS:
            # 满了就丢**最早**的一条：宁可丢一条旧记录，也不让这个表无限长。
            # 丢弃本身要留痕——否则「卡片上为什么没提示」将无从查起。
            # ⚠️ **不能用 `popitem()`**：它在 Python 3.7+ 是 LIFO，丢的是刚刚插进来的那一条——
            #    也就是「正在跑、马上要在卡片上报出来」的那一次，恰恰是最要紧的一条。
            #    dict 保持插入序，所以最早的那个 key 就是 next(iter(...))。
            #    （第一版写的就是 popitem，而注释与日志都说「丢弃最早的」——方向和说法双双错。）
            dropped = next(iter(_RUNS))
            del _RUNS[dropped]
        e = _RUNS.setdefault(run, {"rejected": 0, "first": "", "plugin_id": plugin_id})
        e["rejected"] += count
        if not e["first"]:
            e["first"] = f"{kind}：{first_message}"
    if dropped:                             # 出锁再记，与下面那句一致（别在锁里做 I/O）
        log.warning("runlog 已满（%d 条），丢弃最早的 run=%s", _MAX_RUNS, dropped)
    log.warning("核心拒收 插件=%s run=%s 类型=%s 共 %d 条，首条：%s",
                plugin_id, run, kind, count, first_message)


def take(run: str) -> dict | None:
    """取出并**清掉**某个 run 的记录。收尾时调一次，天然不会泄漏。"""
    if not run:
        return None
    with _LOCK:
        return _RUNS.pop(run, None)


def peek(run: str) -> dict | None:
    """只看不取（测试与诊断用）。"""
    if not run:
        return None
    with _LOCK:
        e = _RUNS.get(run)
        return dict(e) if e else None


def reset() -> None:
    """清空。测试用——模块级状态必须在用例之间归零。"""
    with _LOCK:
        _RUNS.clear()
