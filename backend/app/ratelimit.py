"""登录失败退避（进程内、无依赖）。

为什么需要：账号是长期不变的 `admin`、默认口令写在 README 里、令牌有效期 90 天，而登录端点
原本可以无限次尝试。即使只开在局域网，一台被入侵的设备就能离线字典跑穿。

为什么是进程内而不是 Redis：soroban 是单进程自用应用（uvicorn 单 worker，见 run.py / start.sh），
进程内计数就是全局计数。重启会清空——可接受：重启需要本机权限，那时攻击者已经赢了。

策略：前 FREE_TRIES 次失败不惩罚（手滑打错不该被锁），之后按 2 的幂退避并封顶；
成功登录立即清零。

**同时按两个键计数**，取两者中更严的那个：
  · `(用户名, 来源 IP)` —— 一个 IP 猜多个用户名时互不稀释、同一用户名从不同 IP 尝试时互不牵连；
  · `(用户名, "*")`     —— 与来源 IP 无关的兜底。

第二个键不是冗余，它挡的是一个**实测可行**的绕过：uvicorn 默认信任来自 loopback 的
`X-Forwarded-For`（`forwarded_allow_ips="127.0.0.1"`），而前端开发代理、以及任何同机反向代理
都会让后端把每个请求都看成来自 127.0.0.1。于是 `request.client.host` 变成攻击者可控的字符串，
每次换一个值就是一个全新的键。实测：固定 XFF 打 12 次在第 7 次开始 429；
每次换 XFF 打 12 次，**一次 429 都没有**。

另外，「先查退避、再记失败」是两次独立取锁，中间有窗口：并发一发，一个退避窗口能塞进
几十次口令尝试。所以对外只暴露一个 `begin()`——在**同一把锁里**判定并计数，
失败与否事后由 `record_success()` 决定要不要清零。宁可把一次成功登录也先记上一笔。
"""
from __future__ import annotations

import threading
import time

FREE_TRIES = 5              # 前几次失败不退避
BASE_DELAY = 2.0            # 第 FREE_TRIES+1 次失败后的等待秒数，之后翻倍
MAX_DELAY = 300.0           # 退避上限：5 分钟
FORGET_AFTER = 900.0        # 超过这么久没再失败就忘掉（手滑不该留一整天）
MAX_ENTRIES = 4096          # 内存上限：防止「每次换个用户名」把字典撑爆
PRUNE_DOWN_TO = 3584        # 越界时一次砍到这里（MAX 的 7/8），而不是每次只丢一条


class LoginThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[tuple[str, str], tuple[int, float]] = {}   # key -> (次数, 最后失败时刻)

    @staticmethod
    def key(username: str, client_ip: str | None) -> tuple[str, str]:
        return ((username or "").strip().lower(), client_ip or "?")

    @staticmethod
    def _keys(username: str, client_ip: str | None) -> tuple[tuple[str, str], ...]:
        """这次尝试要计到哪几个键上：按 IP 的那个，以及与 IP 无关的兜底。"""
        u = (username or "").strip().lower()
        return ((u, client_ip or "?"), (u, "*"))

    def _prune(self, now: float) -> None:
        """调用方必须持锁。**只管内存，不管判罚。**

        遗忘窗口的语义由 `_count_locked` 逐键负责（O(1)、读计数的那一刻判定），
        这里扫全表纯粹是为了回收内存。所以它可以跑得很稀疏——只在越过上限时才动手，
        而且一次砍到 `PRUNE_DOWN_TO`，让下一次全表操作在 512 条之后才发生。

        原先它每次失败都跑：全表扫过期 + 满了再全表排序。实测表满时
        每次 `record_failure` 要 1.585ms，表小时 0.065ms——**24 倍**。
        而表是攻击者自己灌满的（`auth.py` 对不存在的用户名短路、不跑 bcrypt），
        于是「几乎零成本」的那条路反而变成全应用最贵的单请求路径，
        单核每秒只扛得住 631 次。批量淘汰把它摊回常数级。

        ⚠️ 淘汰顺序不能只按「最旧」：**正在退避中的那条恰恰就是最旧的**——它一直被
        429 挡回去，时间戳不再刷新。于是攻击者只要用几千个不存在的用户名打一遍登录，
        就能把真正在退避的条目挤出表、计数归零，白拿一轮免罚尝试，可无限重复。
        实测把 900s 的惩罚压成 27s（约 20 倍提速）。
        所以排序键是 `(count, 最后失败时刻)`：**优先丢没有惩罚的条目**
        （count ≤ FREE_TRIES），它们被丢了也不损失任何保护。

        这里刻意 **fail-open**（表满也照样为新 key 建条目，只是换个淘汰顺序）。
        反过来「表满就拒绝建新条目」是 fail-closed：攻击者把表填满高 count 条目
        就能把你自己稳定锁在门外——用 20 倍提速换一个可靠的 DoS，对个人账本更糟。
        """
        if len(self._fails) <= MAX_ENTRIES:
            return
        for k in [k for k, (_, ts) in self._fails.items() if now - ts > FORGET_AFTER]:
            del self._fails[k]
        if len(self._fails) > PRUNE_DOWN_TO:
            victims = sorted(self._fails.items(), key=lambda kv: (kv[1][0], kv[1][1]))
            for k, _ in victims[: len(self._fails) - PRUNE_DOWN_TO]:
                del self._fails[k]

    def _count_locked(self, key: tuple[str, str], now: float) -> int:
        """这个键当前**有效**的失败次数。调用方必须持锁。

        过了遗忘窗口就当没发生过。这件事必须**逐键、在读计数的那一刻**判定，
        不能指望 `_prune` 先跑过：`_prune` 现在只负责内存、跑得稀疏，
        而且「先加一再 prune」的顺序会让刚写进去的新时间戳把过期项救活——
        真到那一步，一年前手滑五次的键今天再错一次就直接进退避。
        """
        entry = self._fails.get(key)
        if entry is None or now - entry[1] > FORGET_AFTER:
            return 0
        return entry[0]

    def _bump_locked(self, key: tuple[str, str], now: float) -> None:
        """记一次失败。调用方必须持锁。`begin()` 与 `record_failure()` 都走这里——
        两条路的计数语义必须逐字一致，否则测试驱动的是一套、生产跑的是另一套。"""
        self._fails[key] = (self._count_locked(key, now) + 1, now)

    def retry_after(self, key: tuple[str, str]) -> int:
        """还需等待的秒数；0 = 现在可以试。（只读，不计数——给测试与诊断用。）"""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return self._wait_locked(key, now)

    def begin(self, username: str, client_ip: str | None) -> int:
        """开始一次登录尝试。返回还需等待的秒数；0 = 放行（并且**已经记上一笔**）。

        判定与计数必须在同一把锁里：分成 `retry_after()` + `record_failure()` 两次取锁的话，
        并发请求会全部在任何一次计数落地之前通过检查——一个退避窗口能塞进几十次尝试。
        """
        now = time.monotonic()
        with self._lock:
            keys = self._keys(username, client_ip)
            wait = max(self._wait_locked(k, now) for k in keys)
            if wait:
                return wait
            for k in keys:                      # 先记后验：成功了再由 record_success 清零
                self._bump_locked(k, now)
            self._prune(now)                    # 写完再收内存，条目数才真的封在 MAX_ENTRIES 内
            return 0

    def _wait_locked(self, key: tuple[str, str], now: float) -> int:
        """调用方必须持锁。"""
        count = self._count_locked(key, now)
        if count <= FREE_TRIES:
            return 0
        delay = min(BASE_DELAY * (2 ** (count - FREE_TRIES - 1)), MAX_DELAY)
        remaining = delay - (now - self._fails[key][1])
        return max(0, int(remaining) + 1) if remaining > 0 else 0

    def record_failure(self, key: tuple[str, str]) -> None:
        now = time.monotonic()
        with self._lock:
            self._bump_locked(key, now)
            self._prune(now)

    def record_success(self, username: str, client_ip: str | None) -> None:
        """登录成功：把这次尝试涉及的两个键都清零。"""
        with self._lock:
            for k in self._keys(username, client_ip):
                self._fails.pop(k, None)

    def reset(self) -> None:
        """仅供测试：清空全部计数。"""
        with self._lock:
            self._fails.clear()


login_throttle = LoginThrottle()
