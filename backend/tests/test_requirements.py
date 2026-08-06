"""依赖清单的一致性。

README 把 requirements.lock.txt 当作 requirements.txt 的等价选项介绍（「可复现安装」）。
它曾漏掉 PyMySQL / rapidocr_onnxruntime / pillow —— 而 db_migrate.py 在**模块顶层** import
pymysql、又被 dbadmin 路由导入，于是选了「可复现安装」这条路的人连服务都起不来。
这里把「lock 必须覆盖每一个直接依赖」变成断言。
"""
import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_REQ = _BACKEND / "requirements.txt"
_LOCK = _BACKEND / "requirements.lock.txt"


def _norm(name: str) -> str:
    """PEP 503 归一化：大小写、下划线/点/连字符都视为等价（PyMySQL == pymysql）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_deps(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        out.append(re.split(r"[<>=!\[;]", line)[0].strip())
    return out


def _locked(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==(.+)$", line)
        if m:
            out[_norm(m.group(1))] = m.group(2)
    return out


@pytest.fixture(scope="module")
def direct():
    return _direct_deps(_REQ.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def locked():
    return _locked(_LOCK.read_text(encoding="utf-8"))


def test_lock_covers_every_direct_dependency(direct, locked):
    missing = [d for d in direct if _norm(d) not in locked]
    assert not missing, (
        f"requirements.lock.txt 漏了直接依赖 {missing}——"
        "按 README「可复现安装」装出来的环境会缺包。"
        "刷新方式：在装齐 requirements.txt 的干净 venv 里 `pip freeze > requirements.lock.txt`"
    )


def test_lock_entries_are_all_pinned(locked):
    """lock 就该是精确 pin；出现范围约束说明它是手抄的而不是 freeze 出来的。"""
    text = _LOCK.read_text(encoding="utf-8")
    loose = [l.strip() for l in text.splitlines()
             if l.strip() and not l.strip().startswith("#") and "==" not in l]
    assert not loose, f"lock 里有非精确 pin 的行：{loose}"


def test_lock_respects_upper_bounds_in_requirements(direct, locked):
    """requirements.txt 里的上限（如 bcrypt<4.1，4.1+ 会破坏 passlib）必须在 lock 里被遵守。"""
    from packaging.requirements import Requirement
    from packaging.version import Version

    for line in _REQ.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        req = Requirement(line)
        pinned = locked.get(_norm(req.name))
        if pinned and req.specifier:
            assert Version(pinned) in req.specifier, (
                f"{req.name} 在 lock 里是 {pinned}，不满足 requirements.txt 的 `{req.specifier}`")


def test_pymysql_is_importable_at_module_scope():
    """db_migrate 在模块顶层 import pymysql，dbadmin 又导入它 → 缺了就整个 app 起不来。
    这条测试能跑起来本身就说明当前环境装了它；顺带把这个耦合写下来。"""
    src = (_BACKEND / "app" / "services" / "db_migrate.py").read_text(encoding="utf-8")
    assert re.search(r"^import pymysql$", src, re.M), \
        "db_migrate 不再顶层 import pymysql 的话，本测试与 lock 里的说明都要一起更新"
    import pymysql  # noqa: F401
