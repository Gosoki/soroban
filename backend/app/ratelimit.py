"""登录失败退避（进程内、无依赖）。

为什么需要：账号是长期不变的 `admin`、默认口令写在 README 里、令牌有效期 90 天，而登录端点
原本可以无限次尝试。即使只开在局域网，一台被入侵的设备就能离线字典跑穿。

为什么是进程内而不是 Redis：soroban 是单进程自用应用（uvicorn 单 worker，见 run.py / start.sh），
进程内计数就是全局计数。重启会清空——可接受：重启需要本机权限，那时攻击者已经赢了。

策略：前 FREE_TRIES 次失败不惩罚（手滑打错不该被锁），之后按 2 的幂退避并封顶；
成功登录立即清零。按 (用户名, 来源 IP) 计数，避免一个 IP 猜多个用户名时互相稀释、
也避免同一用户名从不同 IP 尝试时互相牵连。
"""
from __future__ import annotations

import threading
import time

FREE_TRIES = 5              # 前几次失败不退避
BASE_DELAY = 2.0            # 第 FREE_TRIES+1 次失败后的等待秒数，之后翻倍
MAX_DELAY = 300.0           # 退避上限：5 分钟
FORGET_AFTER = 900.0        # 超过这么久没再失败就忘掉（手滑不该留一整天）
MAX_ENTRIES = 4096          # 内存上限：防止「每次换个用户名」把字典撑爆


class LoginThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[tuple[str, str], tuple[int, float]] = {}   # key -> (次数, 最后失败时刻)

    @staticmethod
    def key(username: str, client_ip: str | None) -> tuple[str, str]:
        return ((username or "").strip().lower(), client_ip or "?")

    def _prune(self, now: float) -> None:
        """调用方必须持锁。清掉过期项；仍然超量则按「谁最该被丢」淘汰。

        ⚠️ 淘汰顺序不能只按「最旧」：**正在退避中的那条恰恰就是最旧的**——它一直被
        429 挡回去，时间戳不再刷新。于是攻击者只要用几千个不存在的用户名打一遍登录
        （`auth.py` 对不存在的用户名短路、不跑 bcrypt，几乎零成本），就能把真正在
        退避的条目挤出表、计数归零，白拿一轮免罚尝试，可无限重复。实测把 900s 的
        惩罚压成 27s（约 20 倍提速）。

        所以排序键改成 `(count, 最后失败时刻)`：**优先丢没有惩罚的条目**
        （count ≤ FREE_TRIES），它们被丢了也不损失任何保护。

        这里刻意 **fail-open**（表满也照样为新 key 建条目，只是换个淘汰顺序）。
        反过来「表满就拒绝建新条目」是 fail-closed：攻击者把表填满高 count 条目
        就能把你自己稳定锁在门外——用 20 倍提速换一个可靠的 DoS，对个人账本更糟。
        """
        expired = [k for k, (_, ts) in self._fails.items() if now - ts > FORGET_AFTER]
        for k in expired:
            del self._fails[k]
        if len(self._fails) > MAX_ENTRIES:
            victims = sorted(self._fails.items(), key=lambda kv: (kv[1][0], kv[1][1]))
            for k, _ in victims[: len(self._fails) - MAX_ENTRIES]:
                del self._fails[k]

    def retry_after(self, key: tuple[str, str]) -> int:
        """还需等待的秒数；0 = 现在可以试。"""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._fails.get(key)
            if entry is None:
                return 0
            count, last = entry
            if count <= FREE_TRIES:
                return 0
            delay = min(BASE_DELAY * (2 ** (count - FREE_TRIES - 1)), MAX_DELAY)
            remaining = delay - (now - last)
            return max(0, int(remaining) + 1) if remaining > 0 else 0

    def record_failure(self, key: tuple[str, str]) -> None:
        now = time.monotonic()
        with self._lock:
            count = self._fails.get(key, (0, now))[0]
            self._fails[key] = (count + 1, now)
            self._prune(now)

    def record_success(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def reset(self) -> None:
        """仅供测试：清空全部计数。"""
        with self._lock:
            self._fails.clear()


login_throttle = LoginThrottle()
