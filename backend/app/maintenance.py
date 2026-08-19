"""全局只读屏障：数据库迁移期间挡住一切写入。

**为什么必须有**：`services/db_migrate.replace_data` 是逐表读源库、逐表写目标库，而它在
**SQLite 上没有读快照**——pysqlite 默认只在 INSERT/UPDATE/DELETE 之前才发 BEGIN，SELECT 一律
跑在 autocommit 下。于是拷贝进行到一半时的写入会产生**撕裂的拷贝**：订单拷过去了、它的物品
还没拷；或者子表引用了一行尚未拷贝的父记录。
（源库是 MySQL 时反而没这问题：pymysql 的 SELECT 会开事务，InnoDB 给整轮拷贝一致的快照。
 又是一处双引擎发散——所以不能指望「反正 MySQL 没事」。）

**必须覆盖这几条写路径**，漏一条锁就是假的：
  1. HTTP 写端点        → main.py 的中间件（POST/PATCH/PUT/DELETE）
  2. routers/plugins.scheduler_loop → 屏障期间别去起爬虫子进程。不是怕它写坏（回灌会被
     中间件拦下），而是白开一次浏览器冲淘宝——并发多开浏览器是风控红线，见插件的
     docs/风控与对策.md。
  3. routers/tags._sync → **一个 GET 请求会写库**：GET /api/tags/{field} 会把数据里出现过
     的新值自动登记成标签并 commit。中间件按「GET 是安全方法」放行，拦不到它，
     所以它自己查屏障。教训：别用 HTTP 方法推断会不会写——只有代码知道。
     （tests/test_maintenance.py 里有一条传递闭包扫描守着，新出现同类路径会红。）
  4. routers/plugins._write_outcome → **收割线程自己开 Session 写 PluginConfig**，
     不经 HTTP、也不查屏障。这一条是**刻意放行**的，不是遗漏：
       · 它写的是卡片状态（last_outcome / last_summary），`PluginConfig` 没有子表，
         不会产生「父行拷了、子行没拷」那种撕裂；
       · 反过来挡住它的代价更大——那次写入没有重试，跳过就等于卡片永久停在「执行中…」，
         而屏障最长可以挂 30 分钟（子进程的收割超时）。
     ⚠️ 但**放行也不保证那次写入落得到新库**：迁移是进程内热切换（`dbadmin` 的
     `set_data_engine`，不重启），而 `_write_outcome` 每次现取 `get_engine()`。
     「pluginconfig 已经拷完 → 收割线程把结果写进**源库** → 切引擎」这个窗口里的写入照样丢，
     新库里的卡片一样停在「执行中」，而 `reclaim_stale_runs()` 只在 lifespan 里跑
     ⇒ 要等下次开机才清。所以这一条的准确说法是「放行的代价不比挡住更大」，
     不是「放行就没事」。
     ⚠️ 上面第 3 条那条传递闭包扫描**看不见它**：那条扫描的起点集只收带 `.get()` 装饰器
     的端点函数，结构上覆盖不到任何后台线程写入者。也就是说「第 4 条以外又冒出第 5 条」
     不会被自动发现——新增后台线程写入时要人工回到这里对一遍。

汇率**原本是第 2 条**：`fx_loop` 直接用 Session 写 FxRate。把取汇率整个搬进插件后，
汇率写入改走 `POST /api/plugins/ingest`，被第 1 条的 HTTP 中间件覆盖；核心里唯一还会
自己写 FxRate 的是 `ensure_manual_rate`，它只在请求作用域内被 rate_for_date
调到，而那些调用点全在 POST/PATCH 端点上——中间件已经挡在前面了。

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
        self._token: object = None              # 当前这次挂起的身份，见 hold 的 finally

    # --- 内部：调用方须持锁 ---
    def _expired(self) -> bool:
        return self._reason is not None and time.monotonic() > self._deadline

    def _reason_locked(self) -> Optional[str]:
        if self._expired():
            log.error("只读屏障超时自愈（原因：%s）——迁移可能异常中断，请检查日志", self._reason)
            self._reason = None
            self._token = None          # 连同身份一起清，否则原主收工时会误判成「还是我的」
        return self._reason

    # --- 查询 ---
    def blocked_reason(self) -> Optional[str]:
        """当前是否只读；None = 可写。非 HTTP 的写入方（scheduler、tags._sync）自己调它。"""
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
            # 这一次挂起的身份。出口靠它判「现在挂着的还是不是我这一次」——
            # 见下面 finally 的理由。用 object() 而不是计数器：不会回绕，也不会被人当序号读。
            self._token = tok = object()
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
                # **只撤自己那一次。** 入口 `_reason_locked()` 自带过期自愈：屏障有硬上限
                # （DEFAULT_TIMEOUT），到点后即便前一次还没收工，后来者也能拿到一个全新的屏障。
                # 原先这里无条件 `self._reason = None`，于是 A 超时之后 B 挂上了自己的屏障，
                # A 一收工就把**B 的**撤掉——B 的迁移在零保护下跑完，中间件放行一切写入，
                # 而 B 的响应仍然是 {"ok": true}，日志只说「只读屏障已撤销」，不会说那是别人的。
                # 现实窗口：前端给迁移配的 timeout 是 120 秒，屏障硬上限 900 秒，
                # 用户以为失败再点一次即可复现（`_ALLOWED_WHILE_READONLY` 明确放行 /api/db/）。
                if self._token is tok:
                    self._reason = None
                    self._deadline = 0.0
                    self._token = None
                    log.info("只读屏障已撤销：%s", reason)
                else:
                    log.warning("只读屏障已被别的维护操作接管，不撤销：%s", reason)

    # --- 测试用 ---
    def reset(self) -> None:
        with self._lock:
            self._reason = None
            self._deadline = 0.0
            self._inflight = 0
            self._token = None


barrier = ReadOnlyBarrier()
