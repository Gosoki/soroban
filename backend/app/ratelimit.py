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
        """调用方必须持锁。清掉过期项；仍然超量则丢最旧的（攻击者刷用户名时的兜底）。"""
        expired = [k for k, (_, ts) in self._fails.items() if now - ts > FORGET_AFTER]
        for k in expired:
            del self._fails[k]
        if len(self._fails) > MAX_ENTRIES:
            for k, _ in sorted(self._fails.items(), key=lambda kv: kv[1][1])[
                    : len(self._fails) - MAX_ENTRIES]:
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
