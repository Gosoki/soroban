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


# --- `--run-plugin` 子进程绝不能停下来等人敲回车 -------------------------------
#
# 这三条各盯一个**独立**的机制，缺一条都能让洞回来：
#   ① 后端起插件时把子进程的 stdin 断掉（`plugins.py` 的 `Popen`）——承重那道；
#   ② `run.py` 的 `__main__` 兜底在子进程里不走 `_fatal`，且最后一行是真异常；
#   ③ `_fatal` 自己在子进程里不 pause——①失守时的第二道。

def _run_plugin_child(tmp_path, *, body: str, stdin) -> subprocess.CompletedProcess:
    """以「打包态的 `--run-plugin` 子进程」的身份跑一个会抛异常的插件模块。

    `sys.frozen` 只能在进程里现设（它不是环境变量），所以走一个薄启动器。
    模块名带 tmp_path 唯一后缀：同名的**包**会遮蔽同名模块，
    2026-09-02 就因为 scratchpad 里躺着一个上一轮的 `boom/` 包而验错过一次。
    """
    plug = tmp_path / "plug"
    plug.mkdir()
    mod = f"pmod_{abs(hash(str(tmp_path))) % 10**8}"
    (plug / f"{mod}.py").write_text(body, encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import runpy, sys\n"
        "sys.frozen = True\n"
        f"sys.argv = ['run.py', '--run-plugin', {str(plug)!r}, '-m', {mod!r}]\n"
        f"runpy.run_path({str(_BACKEND / 'run.py')!r}, run_name='__main__')\n",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SECRET_KEY")}
    env["PYTHONPATH"] = str(_BACKEND)
    return subprocess.run([sys.executable, str(launcher)], cwd=str(_BACKEND), env=env,
                          stdin=stdin, capture_output=True, text=True, timeout=25)


def test_a_crashing_plugin_child_never_waits_for_a_keypress(tmp_path):
    """插件抛未捕获异常时，`--run-plugin` 子进程必须**立刻**退出，不许挂住等回车。

    `_fatal` 的「按回车关闭这个窗口」是给**用户双击出来的主窗口**用的。
    而 `--run-plugin` 是后端起的子进程，没有人在看它的窗口——
    打包态下它的 stdin 还继承自主窗口，是**真的可读**的，于是 `input()` 永久阻塞：

      插件崩 → `__main__` 兜底 → `_fatal` → `input()` 挂死
      → `_reap` 一直等到 `_REAP_TIMEOUT`（30 分钟）
      → 这 30 分钟里 `_INFLIGHT` 攥着互斥键，用户每点一次都是 409「已经在跑了」、
        卡片顶着「执行中」；30 分钟后报的还是**「超时」**，而它其实 2 秒就崩了。

    2026-09-02 实测（修之前）：stdin 可读 → 退出码 124（永久挂起）。

    **stdin 用 `/dev/zero` 而不是管道**：管道的写端一关就是 EOF，
    `input()` 立刻抛 EOFError ⇒ 就算洞还在这条测试也会绿（判据被另一个原因满足）。
    要模拟的是「有一个活着的控制台在那儿」。
    """
    with open(os.devnull if not hasattr(os, "O_RDONLY") else "/dev/zero", "rb") as zero:
        r = _run_plugin_child(tmp_path, body="raise RuntimeError('插件炸了')", stdin=zero)
    assert r.returncode != 0, "插件抛了异常，子进程却报成功"
    assert "按回车" not in r.stderr, (
        f"子进程停下来等人敲回车了——没人会去敲，它会挂到 30 分钟超时：\n{r.stderr[-800:]}")


def test_a_crashing_plugin_child_reports_the_real_exception_not_a_database_scare(tmp_path):
    """子进程 stderr 的**最后一行**必须是插件自己的异常。

    后端 `_summarize` 取的正是最后一行非空内容放到插件卡片上。走主进程那支 `_fatal`
    的话，用户在卡片上看到的会是
    「常见原因：数据库文件损坏或被占用、.env 里的 SECRET_KEY 被改坏、磁盘满」
    ——给汇率插件的一个网络错误安上数据库故障的名字，而他看不到别的解释。
    """
    with open("/dev/zero", "rb") as zero:
        r = _run_plugin_child(tmp_path, body="raise RuntimeError('取汇率失败：连不上')", stdin=zero)
    last = next((ln.strip() for ln in reversed(r.stderr.splitlines()) if ln.strip()), "")
    assert "取汇率失败：连不上" in last, f"卡片上会显示的那一行不是真正的原因：{last!r}"
    assert "数据库" not in last and "SECRET_KEY" not in last, (
        f"把插件的失败说成了主进程的故障：{last!r}")


def test_the_backend_hands_plugin_children_no_stdin_at_all():
    """承重那道：后端起插件时必须显式把 stdin 断掉。

    `Popen` 不写 `stdin` 时的默认是**继承父进程的 stdin**，而打包版的父进程
    就是用户双击出来的控制台。这道闸比 `run.py` 那两条宽——它挡的是**任何**
    读 stdin 的插件（包括将来第三方写的），不只是 `_fatal`。

    判据**先剥注释**：上面那段解释里必然出现 `stdin`、`DEVNULL` 这些词，
    不剥就会被自己的解释满足（记忆里踩过 7 次的形态）。
    """
    import re

    src = (_BACKEND / "app/routers/plugins.py").read_text(encoding="utf-8")
    body = re.sub(r"(?<![:\w])#[^\n]*", "", src)      # 剥 Python 行注释（避开 URL 的 //）
    m = re.search(r"proc = subprocess\.Popen\((.*?)\n\s*\)", body, flags=re.S)
    assert m, "找不到起插件子进程的那个 Popen——这条守卫失去了锚点，先修守卫"
    call = m.group(1)
    assert "stdin=subprocess.DEVNULL" in call, (
        f"起插件的 Popen 没有显式断掉 stdin，默认会继承父进程的控制台：{call!r}")


def test_fatal_itself_refuses_to_pause_in_a_spawned_child():
    """第二道：即使有别的路径走到 `_fatal`，它在子进程身份下也不许 pause。

    今天没有生产路径能同时满足「`_SPAWNED_CHILD` 为真」和「走到 `_fatal`」
    （`__main__` 那支先分流了），所以这里**直接调**它来验——
    不能因为「现在够不着」就把这道闸留成没验过的死代码
    （记忆里那条「AST 守卫不管可达性」说的就是这个）。
    """
    code = (
        "import runpy, sys\n"
        f"g = runpy.run_path({str(_BACKEND / 'run.py')!r}, run_name='not_main')\n"
        "f = g['_fatal']\n"
        # `run_path` 返回的是命名空间的**副本**，改它影响不到函数自己的 globals
        # （2026-09-02 第一版就是这么写的，于是 _fatal 照样 pause、守卫红了——
        #  红的地方不是错的地方）。要改的是 `f.__globals__`。
        "f.__globals__['_SPAWNED_CHILD'] = True\n"
        "f.__globals__['sys'].frozen = True\n"
        "try:\n"
        "    f('测试用', hint='测试用')\n"
        "except SystemExit as e:\n"
        "    print('EXITED', e.code)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SECRET_KEY")}
    env["PYTHONPATH"] = str(_BACKEND)
    with open("/dev/zero", "rb") as zero:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(_BACKEND), env=env,
                           stdin=zero, capture_output=True, text=True, timeout=25)
    assert "EXITED 1" in r.stdout, f"_fatal 没有干脆退出：\n{r.stdout}\n{r.stderr[-600:]}"
    assert "按回车" not in r.stderr, "_fatal 在子进程身份下仍然挂住了窗口"


def test_a_mysql_database_url_is_ignored_loudly_not_silently(tmp_path):
    """`DATABASE_URL` 填非 SQLite 串时，**必须出声**说它不生效、以及实际连的是谁。

    这个行为本身是既定设计（控制库恒 SQLite，走不走 MySQL 由应用内「数据库」页决定），
    问题在于它一直是**静默**的：填了 MySQL 串的人以为自己连的是 MySQL，
    实际拿到的是运行时目录下那个**真实的 SQLite 账本**，两边都不报错。

    这条分支不是假想的，`_control_url` 的 docstring 自己写着它够得到：
    `scripts/migrate_sqlite_to_mysql.py` 明确让人把 `.env` 指向 MySQL 去建 schema
    （「建完记得改回去」），忘了改回去就落在这一支上。

    2026-09-02 我自己撞了一次：一个基准脚本设了 `DATABASE_URL=mysql://…`
    又显式 `run_migrations(那个 mysql url)`——**迁移跑在 MySQL 上、引擎连的却是本地账本**，
    六行测试数据写进了用户的账本（已按精确单号删回）。半边在 MySQL、半边在 SQLite，
    而这个进程从头到尾一个字都没说。

    只要求 warning、不要求硬失败：那条迁移脚本正当地需要在 MySQL 串下把 app 导进来。
    """
    # **运行时目录必须先挪到 tmp**，否则非 SQLite 那一支的兜底字面量算出来的
    # 正是仓库里那个**真实账本**。`create_engine` 虽然是惰性的、不会当场建文件，
    # 但「大概不会碰到」这种假设正是 2026-09-02 那次污染的起点——守卫要构造上就碰不到。
    #
    # 补丁必须打在 `import app.config` **之前**：`config` 与 `database` 都是
    # `from .paths import runtime_dir`（直接绑名字），导入之后再补 `app.paths` 就晚了。
    code = (
        "import logging, sys\n"
        "logging.basicConfig(level=logging.WARNING, stream=sys.stdout)\n"
        "import app.paths as paths\n"
        f"paths.runtime_dir = lambda: __import__('pathlib').Path({str(tmp_path)!r})\n"
        "import app.database as d\n"
        "print('URL=' + d._control_url())\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
    env["PYTHONPATH"] = str(_BACKEND)
    env["DATABASE_URL"] = "mysql+pymysql://u:p@127.0.0.1:3306/whatever"
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    assert "URL=sqlite" in out, f"非 SQLite 串居然生效了？\n{out[-800:]}"
    assert "DATABASE_URL" in out and "不会" in out, (
        f"静默回退到了本地 SQLite，一个字都没说——"
        f"填 MySQL 串的人会以为自己连的是 MySQL：\n{out[-800:]}")
    assert "soroban.db" in out, f"没说清实际连的是哪个库：\n{out[-800:]}"
    assert str(tmp_path) in out, (
        f"兜底库没落在临时目录里——这条守卫指着真账本跑了，先修守卫：\n{out[-800:]}")
