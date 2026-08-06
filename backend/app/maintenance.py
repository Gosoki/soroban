"""全局只读屏障：数据库迁移期间挡住一切写入。

**为什么必须有**：`services/db_migrate.replace_data` 是逐表读源库、逐表写目标库，而它在
**SQLite 上没有读快照**——pysqlite 默认只在 INSERT/UPDATE/DELETE 之前才发 BEGIN，SELECT 一律
跑在 autocommit 下。于是拷贝进行到一半时的写入会产生**撕裂的拷贝**：订单拷过去了、它的物品
还没拷；或者子表引用了一行尚未拷贝的父记录。
（源库是 MySQL 时反而没这问题：pymysql 的 SELECT 会开事务，InnoDB 给整轮拷贝一致的快照。
 又是一处双引擎发散——所以不能指望「反正 MySQL 没事」。）

**必须覆盖三条写路径**，漏一条锁就是假的：
  1. HTTP 写端点        → main.py 的中间件（POST/PATCH/PUT/DELETE）
  2. services/fx.fx_loop → 它**直接用 Session 写** FxRate，完全绕过 HTTP
  3. routers/plugins.scheduler_loop → 屏障期间别去起爬虫子进程。不是怕它写坏（回灌会被
     中间件拦下），而是白开一次浏览器冲淘宝——并发多开浏览器是风控红线，见插件的
     docs/风控与对策.md。

**绝不能泄漏**：屏障一旦忘了撤，整个应用就永久只读。所以除了 try/finally，还带一道**硬超时**
自愈：超过 deadline 一律视为已撤销。宁可迁移中途失去保护（那时最坏是拷贝撕裂、可重来），
也不能让用户对着一个再也写不进去的账本。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Optional

log = logging.getLogger("soroban.maintenance")

DEFAULT_TIMEOUT = 900.0        # 屏障硬上限（秒）：超过即自愈撤销，防永久只读
DEFAULT_DRAIN = 5.0            # 等在飞写请求排空的最长秒数


class ReadOnlyBarrier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: Optional[str] = None
        self._deadline = 0.0
        self._inflight = 0                      # 已放行、尚未完成的写请求数

    # --- 内部：调用方须持锁 ---
    def _expired(self) -> bool:
        return self._reason is not None and time.monotonic() > self._deadline

    def _reason_locked(self) -> Optional[str]:
        if self._expired():
            log.error("只读屏障超时自愈（原因：%s）——迁移可能异常中断，请检查日志", self._reason)
            self._reason = None
        return self._reason

    # --- 查询 ---
    def blocked_reason(self) -> Optional[str]:
        """当前是否只读；None = 可写。非 HTTP 的写入方（fx/scheduler）自己调它。"""
        with self._lock:
            return self._reason_locked()

    # --- HTTP 中间件用：登记/注销一次写请求 ---
    def begin_write(self) -> Optional[str]:
        """原子地「查屏障 + 计数」。返回 None 表示放行（已计数），否则返回拒绝原因。
        查与计数必须在同一把锁里，否则「查完通过 → 屏障挂起 → 才计数」会漏掉一个在飞写。"""
        with self._lock:
            reason = self._reason_locked()
            if reason is not None:
                return reason
            self._inflight += 1
            return None

    def end_write(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    # --- 挂起 ---
    @contextmanager
    def hold(self, reason: str, *, timeout: float = DEFAULT_TIMEOUT, drain: float = DEFAULT_DRAIN):
        """挂起只读屏障，并等在飞的写请求排空。

        drain 超时不算失败——只记 warning 继续。此时最坏是有一两个写请求与拷贝重叠，
        比「因为一个卡住的请求而拒绝迁移」要好。"""
        with self._lock:
            if self._reason_locked() is not None:
                raise RuntimeError(f"已有另一项维护操作在进行：{self._reason}")
            self._reason = reason
            self._deadline = time.monotonic() + timeout
        log.info("只读屏障已挂起：%s", reason)
        try:
            deadline = time.monotonic() + drain
            while time.monotonic() < deadline:
                with self._lock:
                    if self._inflight == 0:
                        break
                time.sleep(0.05)
            else:
                with self._lock:
                    n = self._inflight
                if n:
                    log.warning("仍有 %d 个写请求在飞，不再等待（拷贝可能与其重叠）", n)
            yield
        finally:
            with self._lock:
                self._reason = None
                self._deadline = 0.0
            log.info("只读屏障已撤销：%s", reason)

    # --- 测试用 ---
    def reset(self) -> None:
        with self._lock:
            self._reason = None
            self._deadline = 0.0
            self._inflight = 0


barrier = ReadOnlyBarrier()
