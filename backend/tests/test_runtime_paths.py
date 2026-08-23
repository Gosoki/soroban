"""`.env` 与 sqlite 路径锚在**运行时目录**，不跟着当前工作目录跑。

这个缺陷的现象是**应用完全正常地启动，账本却是空的**——没有报错、没有警告。
在别的目录里跑任何一条 `python -m app.X` / `python -m tools.X` / `alembic` 就会触发：

  · `.env` 找不到 ⇒ `SECRET_KEY` 退回默认值 ⇒ 控制库里加密的 MySQL 连接串再也解不开
    （`read_config` 静默降级成空的本地库）；
  · `sqlite:///./soroban.db` 指向那个目录 ⇒ 当场新建一个空账本，
    而 SQLite 模式下**控制库就是账本本体**。

仓库根目录那个只有控制表、没有任何业务表的 `soroban.db` 就是这么来的。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]


def _run_from(cwd: Path, code: str) -> str:
    """在 `cwd` 下用同一个解释器跑一段代码，返回 stdout。

    **必须开子进程**：`app.config` 在导入时就读 `.env`、`app.database` 在导入时就建引擎，
    在本进程里改 CWD 再重新导入是测不准的（模块已经在 sys.modules 里了）。
    也刻意不继承 `DATABASE_URL`/`SECRET_KEY`——conftest 把它们设成了临时库，
    继承过来就等于把这条测试要验的东西直接抹掉（判据会被另一个原因满足）。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "SECRET_KEY")}
    env["PYTHONPATH"] = str(_BACKEND)
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"子进程挂了：\n{r.stderr[-1500:]}"
    return r.stdout.strip()


def test_dotenv_is_found_from_any_working_directory(tmp_path):
    """在别的目录下也要读到真正的 `.env`。

    读不到的后果不是「少几个配置」，而是 `SECRET_KEY` 变成公开的默认值——
    已保存的 MySQL 连接串用它加密，换一把钥匙就再也解不开。
    """
    if not (_BACKEND / ".env").is_file():
        import pytest
        pytest.skip("这台机器上没有 backend/.env，无从对比")

    here = _run_from(_BACKEND, "from app.config import settings; print(settings.SECRET_KEY)")
    there = _run_from(tmp_path, "from app.config import settings; print(settings.SECRET_KEY)")
    assert there == here, "换个目录就读到了另一个 SECRET_KEY（多半是退回了默认值）"
    assert there != "dev-insecure-key-change-me", "读到的是那个不安全的默认值"


def test_a_relative_sqlite_url_is_anchored_to_the_runtime_dir(tmp_path):
    """相对的 sqlite 路径在任何目录下都指向同一个文件。"""
    code = "from app.config import settings; print(settings.DATABASE_URL)"
    here = _run_from(_BACKEND, code)
    there = _run_from(tmp_path, code)
    assert here == there, f"换个目录就换了一个库：\n  {here}\n  {there}"
    assert here.startswith("sqlite:////"), f"没锚成绝对路径：{here}"
    assert str(_BACKEND) in here, f"锚错了目录：{here}"


def test_importing_the_app_elsewhere_does_not_create_a_phantom_database(tmp_path):
    """在别的目录导入 `app.database`，**不许**在那里凭空建出一个库。

    这条是上面两条的合并现象，也是最容易被当成「账本没了」的那个：
    应用照常起来、一条报错都没有，只是它连的是一个刚被自己创建出来的空文件。
    """
    out = _run_from(tmp_path, "import app.database as d; print(d.control_url())")
    assert str(_BACKEND) in out, f"控制库指到了别处：{out}"
    strays = sorted(p.name for p in tmp_path.iterdir())
    assert not strays, f"在工作目录里留下了这些文件：{strays}"


def test_run_py_and_app_paths_agree_on_the_runtime_dir():
    """`run.py` 与 `app/paths.py` 必须给出同一个目录。

    两处各写一份的话，漂了也不会有任何报错——`run.py` chdir 到 A、
    而 `.env`/sqlite 锚在 B，于是「启动脚本能用、命令行工具连错库」。
    所以 `run.py` 不许再自己算一遍。
    """
    src = (_BACKEND / "run.py").read_text(encoding="utf-8")
    # 取 `_runtime_dir` **自己那一段**：切到下一个顶层 `def ` 为止。
    # （第一版写的是 `.split("def ")[1]`，那取到的是**下一个函数**的函数体——
    #  断言于是恒真，绿得毫无意义。）
    body = src.split("def _runtime_dir", 1)[1].split("\ndef ", 1)[0]
    assert "from app.paths import runtime_dir" in body, \
        "run.py 的 _runtime_dir 没有复用 app/paths.py，多半是又自己算了一份"
    assert "sys.executable" not in body, "run.py 的 _runtime_dir 里还留着自己那份实现"

    from app.paths import runtime_dir
    assert runtime_dir() == _BACKEND


def test_the_control_db_is_anchored_even_when_database_url_is_mysql(tmp_path):
    """`.env` 里写的是 MySQL 串时，控制库路径**同样**要是绝对的。

    `Settings._anchor_sqlite_path` 只管 sqlite 串；`DATABASE_URL` 是 MySQL 时它直接放行，
    于是 `_control_url()` 的兜底字面量 `"sqlite:///./soroban.db"` 从来没被锚过——
    又变回按当前工作目录解析。§140 修的就是这件事，这一条分支当时漏了。

    这个状态够得到：`scripts/migrate_sqlite_to_mysql.py` 明确让人把 `.env` 指向 MySQL
    去建 schema（「建完记得改回去」），没改回去就落在这一支上。
    后果实测过：在别的目录跑 `python -m tools.use_local_db` 会读到一个凭空新建的空控制库，
    回一句「已经是本地 SQLite，无需切换」并退出 0，而真正的控制库里写着 mysql。
    """
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SECRET_KEY")}
    env["PYTHONPATH"] = str(_BACKEND)
    env["DATABASE_URL"] = "mysql+pymysql://u:p@127.0.0.1:3306/nope"
    r = subprocess.run(
        [sys.executable, "-c", "import app.database as d; print(d.control_url())"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    out = r.stdout.strip()

    assert out.startswith("sqlite:////"), f"控制库不是绝对路径：{out}"
    assert str(_BACKEND) in out, f"控制库锚错了目录：{out}"
    strays = sorted(p.name for p in tmp_path.iterdir())
    assert not strays, f"在工作目录里凭空建了库：{strays}"
