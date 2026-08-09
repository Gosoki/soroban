"""单进程闸：同一份数据目录只允许一个 soroban 进程。

**为什么这是硬约束，不是偏好**：插件令牌的撤销表 `plugins/scopes.py::_ALIVE`
是**进程内**的一个 dict。令牌能不能用，判据是「本进程的 _ALIVE 里还在不在」。
于是多开一个 worker，就多出一份空的 _ALIVE：

    worker A 起插件、签发令牌 jti=X（记进 A 的 _ALIVE）
    插件回灌 → 负载均衡把请求分给 worker B → B 的 _ALIVE 里没有 X → **401**

表现是「抓了一批单，一条都没回来」，而日志里只有一串 401——没有任何一处会说
「因为你开了多进程」。同类的进程内状态还有几处（`_ALIVE_PROCS` 在飞子进程表、
`_BATCHES` 批次聚合、`_install_state` 安装进度），全都是同一个道理。

这些状态搬去共享存储是可以做的，但那是另一个量级的改动，而这个项目
（单人记账、本机运行）根本不需要多 worker。所以选择**把假设变成断言**：
第二个进程直接拒绝启动，并说清楚为什么。

锁按**数据目录**分，不是全局：同一台机器上跑两份互不相干的 soroban
（不同目录、不同端口）是完全合法的用法，不该被这道闸误伤。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("soroban.single")

_LOCK_NAME = "soroban.lock"
_handle = None                      # 全局持有：文件对象被 GC 掉，锁就没了


class MultipleInstances(RuntimeError):
    """同一份数据目录已经有一个 soroban 在跑。"""


def _lock_path(control_url: str) -> Optional[Path]:
    """锁文件放在控制库旁边。控制库恒为 SQLite 文件，所以它一定有个所在目录。"""
    prefix = "sqlite:///"
    if not control_url.startswith(prefix):
        return None                 # 理论上到不了（_control_url 保证 sqlite），不猜
    db = Path(control_url[len(prefix):]).expanduser()
    return db.resolve().parent / _LOCK_NAME


def acquire(control_url: str) -> bool:
    """拿锁。拿到返回 True；已被别的进程占用则抛 MultipleInstances。

    **拿不到锁**和**用不了锁**是两回事，处理方式相反：
      · 拿不到 = 真的有第二个进程 → 抛异常，拒绝启动。
      · 用不了（目录只读、文件系统不支持 flock，网络盘上很常见）→ 记一条 warning
        继续跑。为了一道**辅助**闸门让整个应用起不来，是本末倒置。
    """
    global _handle
    if _handle is not None:
        return True                 # 同一进程重复调用（测试里会）——已经持有了
    path = _lock_path(control_url)
    if path is None:
        return False
    try:
        fh = open(path, "a+")       # noqa: SIM115  故意长期持有，见 _handle
    except OSError as e:
        log.warning("单进程闸：锁文件 %s 打不开（%s），跳过检查", path, e)
        return False
    try:
        _lock(fh)
    except MultipleInstances:
        fh.close()
        raise
    except OSError as e:
        log.warning("单进程闸：这个文件系统不支持文件锁（%s），跳过检查", e)
        fh.close()
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass                        # 写 pid 只是给人看的，失败不影响锁本身
    _handle = fh
    return True


def _lock(fh) -> None:
    """非阻塞独占锁。已被占用 → MultipleInstances；不支持 → OSError 由调用方降级。"""
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as e:
            # Windows 上「已被占用」与「不支持」共用 OSError，靠 errno 分：
            # EACCES/EDEADLOCK = 拿不到锁，其余当作不支持。
            import errno
            if e.errno in (errno.EACCES, errno.EDEADLOCK):
                raise MultipleInstances(_WHY) from e
            raise
        return
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise MultipleInstances(_WHY) from e


_WHY = (
    "同一个数据目录里已经有一个 soroban 在运行。\n"
    "  soroban 只能单进程跑：插件令牌的撤销表、在飞子进程表、批次聚合都在进程内存里，"
    "多开一个进程会让插件回灌**全线 401**（抓了一批单一条都回不来，日志里只有 401）。\n"
    "  · 如果你是用 `uvicorn --workers N` 起的：去掉 --workers（或设成 1）。\n"
    "  · 如果只是不小心开了两次：关掉另一个窗口。\n"
    "  · 确实要同时跑两份账本：换一个目录（各自带自己的 soroban.db 与 .env）+ 换端口。"
)


def release() -> None:
    """进程正常关停时松锁。异常退出不必管——进程一没，OS 自动释放。"""
    global _handle
    if _handle is None:
        return
    try:
        _handle.close()             # close 即释放（flock 与 msvcrt 都是）
    except OSError:
        pass
    _handle = None
