"""soroban 打包入口（PyInstaller 冻结后即 soroban.exe）。

单进程：uvicorn 起 FastAPI（app.main:app），后端**同源**托管打入包内的前端 frontend/dist，
API 与页面同端口。运行前把工作目录切到 exe 同级，使 .env、sqlite:///./soroban.db、plugins/
都相对 exe 目录解析——整包可随目录一起分发。

开发/源码运行不需要它，照旧用 start.bat 或 `uvicorn app.main:app`。

⚠️ 本文件承担 start.sh / start.bat 在源码模式下做的那份**安全初始化**：
生成含随机 SECRET_KEY 的 .env。冻结后没有别的地方会做这件事——曾经就是没做，
导致分发出去的 exe 用公开的默认 SECRET_KEY 签 JWT（任何人可伪造管理员令牌）。
"""

import os
import secrets
import sys
from pathlib import Path

# 首次启动生成的 .env 模板。刻意内联而不是读 .env.example：那要求把示例文件也打进包，
# 多一个会漂的依赖；这里只需要真正影响安全的那几项，其余走 config.py 的默认值。
_ENV_TEMPLATE = """\
# soroban 首次启动自动生成。SECRET_KEY 是随机的，请勿外传、勿提交。
# 它同时用于签发登录令牌与加密数据库连接串——换掉它会让所有人退出登录、
# 且已保存的 MySQL 连接串无法解密（会被视为无配置、回退本地 SQLite）。
SECRET_KEY={secret}

# 本地 SQLite 文件（同时是「控制库」，存当前后端与加密的 MySQL 连接串）。
# 是否使用 MySQL 由应用内「数据库」页决定，不由这里的串决定。
DATABASE_URL=sqlite:///./soroban.db

# 登录有效期（天）
TOKEN_EXPIRE_DAYS=90

# 首个管理员的账号与口令（**只在第一次建号时生效**，之后改这里没用，请在设置页改密码）。
# 留空则用 README 里写的那对默认值 —— 它是公开的，装好后请尽快改掉。
#SOROBAN_ADMIN_USER=
#SOROBAN_ADMIN_PASS=

# 监听地址与端口。改完保存、重新双击即可生效（环境变量优先级更高）。
# HOST 保持 127.0.0.1 = 只有本机能访问。改成 0.0.0.0 会把账本暴露给整个局域网，
# **改之前务必先改掉默认密码**。
BACKEND_PORT=8620
HOST=127.0.0.1
"""


def _runtime_dir() -> Path:
    """运行时数据目录：打包后取 exe 同级，源码运行取 backend/。

    实现在 `app/paths.py`——`.env` 与相对的 sqlite 路径也锚在同一个目录上，
    两处各写一份迟早会漂，而漂了的现象是「应用完全正常地启动、账本却是空的」。
    那个模块刻意零副作用（只 import sys/pathlib），所以这里可以在
    `ensure_env()` 之前安全导入它——app.config 那种一导入就读 .env 的就不行。
    """
    from app.paths import runtime_dir
    return runtime_dir()


def _fatal(msg: str, *, hint: str = "") -> "NoReturn":       # noqa: F821
    """打印人话错误并退出。**打包态下挂住窗口**。

    打包版是双击运行的：任何异常都会让控制台窗口在几毫秒内关掉，用户看到的是
    「点了没反应」。而这些失败恰恰都是用户自己能解决的（换个目录、关掉另一个实例），
    前提是他得看见错在哪。所以冻结态下必须停下来等一次回车。
    源码运行不 pause——那会卡住 CI 与脚本。
    """
    print(f"\n[soroban 启动失败] {msg}", file=sys.stderr)
    if hint:
        print(f"  → {hint}", file=sys.stderr)
    if getattr(sys, "frozen", False):
        try:
            input("\n按回车关闭这个窗口…")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(1)


def ensure_env(rt: Path) -> bool:
    """保证运行目录下有 .env（含随机 SECRET_KEY）。返回是否是本次新建的。

    必须在 `import app.*` **之前**调用——app.config 在导入时就实例化 Settings 并读 .env。
    已存在则原样不动（不覆盖用户改过的配置）。

    写不进去是**致命的**，不能吞掉继续跑：没有 .env 就没有随机 SECRET_KEY，
    而 config.py 的默认值写在公开仓库里——那等于任何人都能伪造管理员令牌。
    最常见的触发是把 exe 放进 `C:\\Program Files\\` 后双击（该目录对普通用户只读），
    原先这里抛的是一句裸 PermissionError traceback，窗口随即关闭。
    """
    f = rt / ".env"
    if f.exists():
        return False
    try:
        f.write_text(_ENV_TEMPLATE.format(secret=secrets.token_hex(32)), encoding="utf-8")
    except OSError as e:
        _fatal(
            f"无法在 {rt} 里创建 .env（{e.__class__.__name__}: {e}）。\n"
            "  soroban 需要把配置、账本数据库、插件登录会话都写在自己所在的目录里。",
            hint="把整个 soroban 文件夹移到有写权限的位置（如 桌面 或 D:\\soroban）再双击。"
                 "别放在 C:\\Program Files 下——那里对普通用户是只读的。",
        )
    try:                                   # 尽量收紧权限；Windows 上 chmod 基本无效，忽略即可
        f.chmod(0o600)
    except OSError:
        pass
    return True


def _runtime_setting(rt: Path, key: str, default: str) -> str:
    """启动器自己的配置项：环境变量 > 运行目录的 `.env` > 默认值。

    只做最朴素的 `KEY=VALUE` 解析（去引号、跳过注释与空行）：这几行必须跑在
    `import app.*` **之前**，不能依赖应用的配置体系；而 `.env` 是我们自己生成的，
    格式可控，不值得为它引入解析库。读不出来就当没写——启动器不该因为配置文件里
    有一行怪东西就起不来。
    """
    v = os.environ.get(key)
    if v:
        return v
    f = rt / ".env"
    try:
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == key:
                val = val.strip()
                # **行尾注释要剥掉。** 用户照着提示改端口时很自然会写
                # `BACKEND_PORT=8621  # 换个端口`，而带引号的值里 `#` 是内容不是注释，
                # 所以只在**没被引号包起来**时剥。不剥的话 `int()` 会抛 ValueError，
                # 落到 __main__ 的兜底里，而那句提示写的是「数据库文件损坏或被占用、
                # SECRET_KEY 被改坏、磁盘满」——把用户往完全错误的方向指。
                if val[:1] not in ("'", '"'):
                    val = val.split("#", 1)[0].strip()
                return val.strip("'\"") or default
    except OSError:
        pass
    return default


def _check_port_free(host: str, port: int) -> None:
    """端口占用 → **启动前**就说清楚，而不是让 uvicorn 抛异常后窗口一闪而过。

    这是打包版最常见的失败：用户双击了第二次（第一个还在跑）。原先的表现是
    先打印「soroban 已启动」、然后一串 traceback、然后窗口关闭——最后留在屏幕上的
    那句话还是「已启动」，与事实完全相反。
    """
    import socket

    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        if s.connect_ex((probe_host, port)) == 0:
            _fatal(
                f"端口 {port} 已被占用——多半是 soroban 已经在跑了。",
                hint=f"先去看看是不是已经开着（浏览器打开 http://{probe_host}:{port}）。"
                     f"确实要再开一个的话，用记事本打开旁边的 .env，"
                     f"把 BACKEND_PORT 改成别的（如 8621）再双击。",
            )


def _run_plugin(argv: list[str]) -> "NoReturn":                 # noqa: F821
    """内部动词 `--run-plugin <插件目录> <入口…>`：**把 exe 自己当解释器**跑一个插件。

    存在的理由是打包版的一个死结：`plugin.toml` 里写 `python = "inherit"` 的插件
    （汇率就是）意思是「用 soroban 自己的环境跑，不建 venv」。源码模式下
    `sys.executable` 就是那个环境，一切正常；**打包之后 `sys.executable` 是 soroban.exe**：
      · 拿它去 `soroban.exe -m soroban_fx fetch` —— bootloader 根本不解释 `-m`，
        结果是把 soroban 又启动一遍（见 `_base_python` 的说明）；
      · 退一步去找系统的 python，那个 python 里**没有 httpx**，ModuleNotFoundError。
    两条路都不通 ⇒ 分发包上汇率插件必然跑不起来，而它是账本唯一的汇率来源。

    出路是承认一件事：这类插件本来就是 soroban 的一部分，依赖也已经在 exe 里了。
    所以给 exe 开一个内部动词，让它以「python 解释器」的语义跑一次插件入口，
    `-m mod` 与 `脚本.py` 两种写法都照 python 自己的规矩解释。
    重依赖插件（浏览器、OCR）不走这里，照旧用自带 venv，隔离仍然成立。

    这不构成新的攻击面：能给 exe 传参数的人本来就能直接运行任意程序，
    而 soroban 平时就在起插件子进程。它只是把「误用」变成「明确支持的一种用法」。

    这里**不能**做 chdir / 建 .env / 起服务：工作目录由调用方（`_launch` 的 cwd）
    设成插件目录，令牌与配置走环境变量。做了就等于每跑一次插件顺手改一次运行目录。
    """
    if len(argv) < 2:
        print("--run-plugin 需要 <插件目录> <入口…>，例如 "
              "--run-plugin /path/to/plugin -m soroban_fx fetch", file=sys.stderr)
        raise SystemExit(2)
    import runpy

    plugin_dir, rest = argv[0], argv[1:]
    sys.path.insert(0, plugin_dir)          # 让插件包可 import（等价于 python 的 cwd 规则）
    if rest[0] == "-m":
        if len(rest) < 2:
            print("--run-plugin 的 -m 后面要跟模块名", file=sys.stderr)
            raise SystemExit(2)
        sys.argv = [rest[1], *rest[2:]]
        runpy.run_module(rest[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = list(rest)
        runpy.run_path(rest[0], run_name="__main__")
    raise SystemExit(0)


def main() -> None:
    # 本入口只接受一个**精确白名单**：`--use-local-db [--yes]`（MySQL 连不上时的离线自救）
    # 与内部动词 `--run-plugin`（见上）。其余一律拒绝。这道保险是因为：打包成 exe 后
    # sys.executable 就是 exe 自己，任何「拿它当 python 跑」的误用（如 `soroban.exe -m venv …`）
    # 都会静默地**把 soroban 再启动一遍**——建 .env、连库跑迁移，最后卡在端口占用。
    # 直接报错退出，能把这类误用从「莫名其妙的影子实例」变成一眼可见的错误。
    argv = sys.argv[1:]
    rescue = False
    if argv:
        # --run-plugin 必须**最先**判：它后面跟的是插件自己的参数，
        # 不该被下面那条「整个 argv 必须落在白名单里」的规则误伤。
        if argv[0] == "--run-plugin":
            _run_plugin(argv[1:])
        if set(argv) <= {"--use-local-db", "--yes"} and "--use-local-db" in argv:
            rescue = True
        else:
            print(f"soroban 不接受命令行参数（收到 {argv}）。"
                  "如果你想用它当 Python 解释器跑模块，那是用错了——请用系统的 python。",
                  file=sys.stderr)
            raise SystemExit(2)
    rt = _runtime_dir()
    os.chdir(rt)  # 让 .env / soroban.db（默认 sqlite:///./soroban.db）落在 exe 同级

    created_env = ensure_env(rt)           # ← 必须在任何 app.* 导入之前

    # 自救必须排在 chdir + ensure_env 之后（app.config 在导入时就读 .env 定 SECRET_KEY，
    # 而控制库的 MySQL 连接串是用它加密的），且排在 `from app.main import app` 之前——
    # 后者会连库跑迁移，而这条路径存在的前提正是「库连不上」。
    if rescue:
        from app.rescue import use_local_db
        raise SystemExit(use_local_db(assume_yes="--yes" in argv))

    # 默认只监听环回：要暴露到局域网得**显式**设 HOST=0.0.0.0。
    # 之前默认 0.0.0.0，配上「无 .env → 默认 SECRET_KEY」就是开箱即用的认证绕过。
    #
    # 优先级：环境变量 > .env > 默认值。
    # **`.env` 这一档是打包版唯一可用的入口**：双击 exe 的用户没有终端，
    # 他能编辑的只有 exe 旁边那个 .env。原先这两项只读环境变量，
    # 于是「端口被占用了怎么办」这个最常见的问题，用户照着提示改 .env 是**无效**的
    # ——而提示本身还理直气壮地写着「设环境变量 BACKEND_PORT」。
    # 这两项刻意不进 app.config 的 Settings：那是**应用**的配置，
    # 而监听地址是**启动器**的事，冻结版与源码版的启动器不是同一个。
    host = _runtime_setting(rt, "HOST", "127.0.0.1")
    raw_port = _runtime_setting(rt, "BACKEND_PORT", "8620")
    try:
        port = int(raw_port)
    except ValueError:
        # 说清楚是**哪一项**配错了。原先这里直接抛，被 __main__ 的兜底接住，
        # 而那句提示指向「数据库损坏」——用户会去查一个完全无关的方向。
        _fatal(f"`.env` 里的 BACKEND_PORT 不是一个数字：{raw_port!r}。",
               hint="用记事本打开 exe 旁边的 .env，把这一行改成形如 `BACKEND_PORT=8621`。")
    if not (1 <= port <= 65535):
        _fatal(f"`.env` 里的 BACKEND_PORT 超出范围：{port}。", hint="端口要在 1..65535 之间。")
    # **必须落回 os.environ，而且要在 `import app.main` 之前。**
    # `.env` 这一档只有启动器读得到（pydantic-settings 读 .env 也不写回 os.environ），
    # 而应用侧有两个模块在**导入期**只认环境变量：
    #   · `main._LAN` —— 决定要不要关掉 /docs、/openapi.json。冻结态下它恒读不到
    #     用户在 .env 里设的 HOST ⇒ 恒判「环回」⇒ 用户按模板把 HOST 改成 0.0.0.0
    #     想用手机记账，局域网里任何设备都能免登录打开完整 API 地图，
    #     而那句「已关闭 /docs」的日志也不会出现，连痕迹都没有。
    #   · `plugins._SELF_URL` —— 插件回灌地址。用户照本程序自己的提示把 BACKEND_PORT
    #     改成 8621 之后，浏览器一切正常，插件却把数据（连同 Bearer 令牌）POST 到 8620
    #     ——那上面往往正是**占用了端口的那个别的程序**。
    # 这两处的默认值都是对的，错在「拿不到真实值」。在这里补上，两处一起好。
    os.environ["HOST"] = host
    os.environ["BACKEND_PORT"] = str(port)
    # **首个管理员的账号口令也要从 `.env` 认过来。** `app.seed.ensure_admin()` 读的是
    # `os.getenv`，而 `.env` 只被 pydantic-settings 读进 `Settings`（且这两个键不是
    # Settings 的字段），**从不写回 `os.environ`**。于是冻结版用户在 `.env` 里写
    # `SOROBAN_ADMIN_PASS=强口令`，首启建出来的仍然是默认口令 ——
    # 而 `.env` 是双击用户**唯一**能编辑的入口（见上面那段）。
    for _k in ("SOROBAN_ADMIN_USER", "SOROBAN_ADMIN_PASS"):
        _v = _runtime_setting(rt, _k, "")
        if _v:
            os.environ[_k] = _v

    _check_port_free(host, port)           # 占用 → 现在就说清楚，别等 uvicorn 抛异常

    import uvicorn
    from app.main import app  # 触发建表/迁移在 lifespan 内进行

    # 建库/迁移 + 建管理员**都在 lifespan 里做**（`app.main.lifespan`），
    # 因为那里才拿得到单进程闸。这里原先先调一次 `seed.main()`，而它自己会
    # `migrate_to_latest()` ⇒ 完整 alembic upgrade 跑在闸之前，
    # 改端口开第二个实例时会对正在被老进程使用的库跑完迁移。

    if created_env:
        print(f"已生成 {rt / '.env'}（含随机 SECRET_KEY）。请勿外传或提交。")
    # 措辞是「正在启动」不是「已启动」：这一行**在 serve 之前**打印，
    # 真正的「已启动」由 uvicorn 自己的 `Uvicorn running on ...` 负责。
    # 原先这里写「soroban 已启动」，端口占用时它就成了屏幕上最后一句话，
    # 而事实恰恰相反。
    print(f"soroban 正在启动，将监听 {host}:{port}  (API 文档 /docs)")
    print(f"  本机访问 -> http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    if host == "0.0.0.0":
        print(f"  局域网访问 -> http://<本机IP>:{port}（需放行防火墙 TCP {port}）")
        print("  ⚠️ 已对外监听：请确认已改掉默认密码，并只在可信网络里这么开。")
    print("按 Ctrl+C 退出。")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:              # 正常退出路径，不该报「启动失败」
        pass
    except SystemExit as e:
        # **uvicorn 不把启动失败抛给调用方。** 它自己 `except OSError → logger.error →
        # sys.exit(STARTUP_FAILURE)`；lifespan 里抛异常同样被转成 `sys.exit(3)`。
        # 于是下面那支 `except OSError` 是**死代码**（从来没执行过），
        # 而 `__main__` 里的 `except SystemExit: raise` 会让打包版**闪退**：
        # 数据库连不上时 database.py 那段精心写的中文指引、单实例闸的解释、
        # SECRET_KEY 校验不过的说明——全都打印出来了，然后窗口在一秒内消失。
        # 用户看到的是「点了没反应」，而 __main__ 的注释还写着「这些原先都只会让窗口闪一下」。
        if e.code:
            _fatal(f"服务启动失败（uvicorn 退出码 {e.code}）。",
                   hint="真正的原因在上面几行：端口被占用或绑定被拒（改 .env 的 BACKEND_PORT）、"
                        "数据库连不上、已经有一个实例在跑、.env 里的 SECRET_KEY 被改坏。")
        raise                              # 正常退出（code=0/None）原样放行
    except OSError as e:                   # 兜底：将来 uvicorn 若改成向上抛
        _fatal(f"无法监听 {host}:{port}（{e}）。",
               hint="用记事本打开旁边的 .env，把 BACKEND_PORT 改成别的（如 8621）再双击。")


if __name__ == "__main__":
    # 兜住**所有**逃逸到这里的异常。打包版双击时，裸 traceback 等于「点了没反应」——
    # 迁移失败、库损坏、SECRET_KEY 校验不过（main.py 是 fail-closed 的），
    # 这些原先都只会让窗口闪一下。
    try:
        main()
    except SystemExit:
        raise                              # _fatal / argv 白名单已经给过交代
    except BaseException as e:             # noqa: BLE001  KeyboardInterrupt 也走这儿收尾
        if isinstance(e, KeyboardInterrupt):
            raise SystemExit(0)
        import traceback

        traceback.print_exc()
        _fatal(f"{e.__class__.__name__}: {e}",
               hint="上面是完整堆栈。常见原因：数据库文件损坏或被占用、.env 里的 "
                    "SECRET_KEY 被改坏、磁盘满。把这段完整截图下来最好排查。")
