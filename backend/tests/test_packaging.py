"""打包契约：soroban.spec 声明要打进 exe 的东西，必须与代码里按 `sys._MEIPASS` 找它们的路径对得上。

为什么需要这层守护：这些资源都是**运行时按路径读**的（Alembic 迁移脚本、前端 dist、OCR 模板图），
PyInstaller 静态分析看不到，只能靠 spec 显式声明。任何一边改了路径而另一边没跟，
打出来的 exe 会在**用户第一次启动时**才炸（建不了表 / 页面 404 / OCR 少一个判别信号）——
而 Windows 打包在 CI 与本机测试里都跑不到。这里用纯文本比对把两边钉在一起。

注意：本测试**不构建** exe（Linux 上也构不出 Windows 包），只校验清单与代码的一致性。
真正的构建仍需在 Windows 上跑一次 pyinstaller.bat。
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RUN_PY = _REPO / "backend" / "run.py"
_SPEC = _REPO / "soroban.spec"


@pytest.fixture(scope="module")
def spec_text() -> str:
    if not _SPEC.is_file():
        pytest.fail("soroban.spec 不存在——pyinstaller.bat 直接调它，缺了整条打包链是断的")
    return _SPEC.read_text(encoding="utf-8")


def test_spec_is_not_gitignored():
    """标准 Python .gitignore 有 `*.spec`，曾把这份手写清单整个忽略掉。"""
    ignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
    assert "!soroban.spec" in ignore, ".gitignore 缺少 `!soroban.spec` 反排除，spec 会在新 clone 里消失"


def test_spec_parses(spec_text):
    import ast
    ast.parse(spec_text)


def test_spec_bundles_alembic(spec_text):
    """database.run_migrations 冻结后用 `Path(sys._MEIPASS)` 找 alembic.ini 与 alembic/。"""
    db = (_REPO / "backend" / "app" / "database.py").read_text(encoding="utf-8")
    assert '_ROOT / "alembic.ini"' in db and '_ROOT / "alembic"' in db
    assert '"alembic.ini"' in spec_text and '"alembic"' in spec_text, \
        "spec 没打包 alembic.ini / alembic/，冻结后启动会建不出表"


def test_spec_bundles_frontend_dist(spec_text):
    """main.py 冻结后从 `_MEIPASS/frontend/dist` 托管前端——spec 的目标路径必须一模一样。"""
    main = (_REPO / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert '"frontend" / "dist"' in main
    assert '"frontend/dist"' in spec_text, "spec 里 dist 的目标路径必须是 frontend/dist"


def test_spec_bundles_ocr_template(spec_text):
    """ocr.py 用 `Path(__file__).with_name("xianyu_truck.png")` 读模板图，
    冻结后 __file__ 指向 _MEIPASS 内，所以图必须落在 app/services/ 下。"""
    ocr = (_REPO / "backend" / "app" / "services" / "ocr.py").read_text(encoding="utf-8")
    m = re.search(r'with_name\("([^"]+)"\)', ocr)
    assert m, "ocr.py 里找不到 with_name(...) 的模板图引用"
    asset = m.group(1)
    assert (_REPO / "backend" / "app" / "services" / asset).is_file(), f"模板图 {asset} 不在仓库里"
    assert asset in spec_text and '"app/services"' in spec_text, \
        f"spec 没把 {asset} 打到 app/services/，冻结后卡车判别信号会静默失效"


def test_spec_entry_point_is_run_py(spec_text):
    assert (_REPO / "backend" / "run.py").is_file()
    assert '"run.py"' in spec_text


def test_every_source_path_in_spec_exists(spec_text):
    """spec 里写死的每个源路径都要真实存在（frontend/dist 除外——它由构建产生）。"""
    for rel in ["backend/alembic.ini", "backend/alembic",
                "backend/app/services/xianyu_truck.png", "backend/run.py",
                "frontend/public/favicon.ico"]:
        assert (_REPO / rel).exists(), f"spec 引用了不存在的路径：{rel}"


def test_plugins_are_bundled_but_secrets_never_are(spec_text):
    """插件源码打进 exe，但**凭据与机器相关的东西一律剔除**。

    带上插件是为了消灭「打包者忘了把 plugins 文件夹复制到 exe 旁边」——
    那一步没有任何东西提醒他做，漏了的表现是整个插件页空空如也，而 exe 一切正常。

    剔除清单是这条改动的**前提条件**，不是优化：
    `.state/` 是打包者自己的淘宝登录 cookie。随包分发一个文件夹时它还能事后删，
    **打进 exe 就再也删不掉了**——所以这里必须是硬性剔除，不能靠人记得。
    """
    assert "_bundle_plugins(ROOT / \"plugins\")" in spec_text, \
        "spec 没把插件打进去——分发包会一个插件都没有"
    for must in (".state", ".env", ".venv", "__pycache__", ".pyc"):
        assert must in spec_text, f"打包插件时没有剔除 {must}"
    # 剔除是真的在代码里生效，而不是只写在注释里
    assert "_PLUG_SKIP_DIRS" in spec_text and "set(rel.parts[:-1]) & _PLUG_SKIP_DIRS" in spec_text


def test_bundled_plugins_are_released_to_a_writable_dir():
    """打进包的插件必须在启动时**释放到磁盘**，不能就地跑。

    插件目录是可写的（各自的 .venv、登录会话 .state、参数都写在里面），
    而 onefile 的 _MEIPASS 每次进程退出就被清掉——就地跑的话，
    淘宝插件每次启动都要重装依赖、重新扫码登录。
    """
    plug = (_REPO / "backend" / "app" / "routers" / "plugins.py").read_text(encoding="utf-8")
    assert "Path(sys.executable).resolve().parent" in plug, \
        "插件在磁盘上的家不再是 exe 同级——释放出去也找不到"
    assert "def seed_bundled_plugins" in plug
    main = (_REPO / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert "seed_bundled_plugins()" in main, "释放函数没人调用，等于没打包"
    i_seed = main.index("    seed_bundled_plugins()")
    i_loop = main.index("scheduler_loop()")
    assert i_seed < i_loop, "释放排在定时循环之后——第一轮定时会看不到任何插件"


def test_pyinstaller_bat_checks_for_spec():
    """pyinstaller.bat 缺 spec 时要给出可诊断的报错，而不是让 PyInstaller 吐一句路径找不到。"""
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert "soroban.spec" in bat
    assert "not exist" in bat and "soroban.spec not found" in bat


@pytest.mark.parametrize("bat", ["pyinstaller.bat", "start.bat"])
def test_batch_goto_targets_all_exist(bat):
    """`goto :nonexistent` 在 cmd 里是运行到那一行才报错——而这些脚本的错误分支恰恰是
    平时跑不到的，笔误能潜伏很久，直到真出错时才让用户看到一句莫名其妙的报错。"""
    text = (_REPO / bat).read_text(encoding="utf-8", errors="replace")
    gotos = {m.lower() for m in re.findall(r"goto\s+:?(\w+)", text, re.I)}
    gotos -= {"eof"}                     # :eof 是 cmd 内置标签，无需定义
    labels = {m.lower() for m in re.findall(r"^\s*:(\w+)", text, re.M)}
    assert gotos <= labels, f"{bat} 跳转到了不存在的标签：{sorted(gotos - labels)}"


def test_pyinstaller_bat_has_no_errorlevel_in_block():
    """批处理陷阱：括号块里的 %errorlevel% 在**解析**时就展开了，永远读到进块前的旧值。
    这类检查必须写成平铺的 `if errorlevel N`。"""
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    depth = 0
    for raw in bat.splitlines():
        line = raw.strip()
        if line.lower().startswith("rem"):
            continue
        if depth > 0 and "%errorlevel%" in line.lower():
            pytest.fail(f"括号块内用了 %errorlevel%（解析期展开，读到的是旧值）：{line}")
        depth += line.count("(") - line.count(")")
        depth = max(0, depth)


# --- 静态托管的缓存策略 ---------------------------------------------------------

def test_index_html_is_not_cached_but_assets_are():
    """前端每次 `npm run build`，资源文件名的哈希都会变。若 `index.html` 被浏览器缓存，
    老用户拿到的还是旧的那份，它引用的旧哈希文件已经不存在 → 一连串 404 → **整站白屏**，
    普通刷新还不一定能好（要硬刷新才绕过缓存）。本项目真白过一次。

    StaticFiles 默认只给 etag/last-modified、**不给 Cache-Control**，浏览器会启发式缓存——
    所以必须显式声明。反过来 assets 带内容哈希，内容变了名字就变，缓存一年也安全。
    """
    import inspect

    from app import main

    src = inspect.getsource(main._SpaStatic)
    assert 'resp.headers["Cache-Control"] = "no-cache"' in src, "index.html 没设 no-cache"
    assert "immutable" in src and "max-age=31536000" in src, "assets 没设长缓存"
    assert 'path.startswith("assets/")' in src, "两类资源没有分开设策略"


def test_static_mount_uses_the_cache_aware_class():
    """挂载时用回裸 StaticFiles 就等于把上面那条策略整个绕过去了。"""
    import inspect

    from app import main

    src = inspect.getsource(main)
    tail = src[src.index("if _DIST.is_dir():"):]
    assert "_SpaStatic(directory=" in tail, "静态挂载没用带缓存策略的子类"


def test_pyinstaller_bat_never_wipes_the_release_dir():
    """发布目录同时是打包版的**运行数据目录**，绝不能整体删除。

    exe 是便携式设计（run.py chdir 到自身所在目录），soroban.db、含 SECRET_KEY 的 .env、
    scraper\\ 的 venv 与扫码会话全落在 exe 旁边。而 VERSION 是手工维护的常量，
    「改个 bug 再打一次包」不会有人去改它——于是 `rmdir /s /q "%RELEASE%"` 就是
    一次无提示、不进回收站、backup.sh 也覆盖不到的全量数据销毁。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert 'rmdir /s /q "%RELEASE%"' not in bat, \
        "pyinstaller.bat 不许删除 %RELEASE%——那是用户账本所在目录"
    assert 'if not exist "%RELEASE%" mkdir "%RELEASE%"' in bat, \
        "发布目录应幂等创建（不存在才建），而不是先删后建"


def test_pyinstaller_bat_publishes_only_on_success():
    """产物先落 build\\dist，成功后才拷进发布目录。

    否则前端构建或 PyInstaller 失败时，用户既没拿到新 exe、发布目录里那份能跑的旧 exe
    也已经被覆盖/删掉了。这几条失败路径在脚本里是真实存在的（:npm_build_fail / :build_fail）。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert '--distpath "%ROOT%build\\dist"' in bat, "构建产物应先落到 build\\dist"
    assert 'copy /y "%ROOT%build\\dist\\soroban.exe" "%RELEASE%\\soroban.exe"' in bat, \
        "应在构建成功后才把 exe 拷进发布目录"
    assert ":copy_fail" in bat, "拷贝失败（exe 正在运行）要有可诊断的出口"


def test_offline_rescue_lives_in_the_app_package():
    """离线自救的实现必须在 `app/` 下，`tools/` 只能是薄壳。

    打包版拿不到 `tools/`：Analysis 入口是 run.py，run.py 与 app.* 都不 import tools，
    spec 的 datas 也没收它。而 `app.*` 天然在 PyInstaller 的导入图里。
    实现放错地方的后果是：MySQL 连不上时，唯一的自救指引在 exe 环境里根本执行不了，
    用户的账本被永久锁在一个起不来的进程后面。
    """
    from app import rescue

    assert hasattr(rescue, "use_local_db")
    shim = (_REPO / "backend" / "tools" / "use_local_db.py").read_text(encoding="utf-8")
    assert "from app.rescue import use_local_db" in shim, "tools 侧应复用 app.rescue，不许再抄一份"
    assert "switch_to_local()" not in shim, "自救逻辑不许留在 tools/（打包版取不到）"


def test_run_py_accepts_only_the_rescue_flag():
    """run.py 的参数白名单：只放行 `--use-local-db [--yes]`，其余一律 exit 2。

    放行必须排在 chdir + ensure_env 之后（SECRET_KEY 要先就位才能解密控制库里的连接串），
    且排在 `from app.main import app` 之前——后者会连库跑迁移，而这条路径的前提正是「库连不上」。
    """
    src = _RUN_PY.read_text(encoding="utf-8")
    assert '{"--use-local-db", "--yes"}' in src, "参数白名单不见了"
    # 注意匹配真语句而不是注释里的引用：run.py 的注释里也写着 `from app.main import app`，
    # 直接 index 会先命中那一处，把顺序断言变成一条恒假式。
    i_env = src.index("created_env = ensure_env(rt)")
    i_rescue = src.index("\n    if rescue:")
    i_app = src.index("\n    from app.main import app")
    assert i_env < i_rescue < i_app, "自救分支的位置不对（须在 ensure_env 之后、import app.main 之前）"


def test_mysql_down_hint_covers_the_packaged_build():
    """MySQL 连不上时的自救指引必须同时给出打包版能执行的那条。"""
    src = (_REPO / "backend" / "app" / "database.py").read_text(encoding="utf-8")
    assert "soroban.exe --use-local-db" in src, "指引只写了源码运行那条，打包版用户照做不了"


def test_launcher_settings_can_come_from_the_env_file(tmp_path, monkeypatch):
    """`BACKEND_PORT` / `HOST` 必须能从**运行目录的 .env** 读到，不能只认环境变量。

    双击 exe 的用户没有终端，他唯一能编辑的就是 exe 旁边那个 `.env`。
    原先这两项只读 `os.environ`，于是「端口被占用了怎么办」这个最常见的问题，
    照着提示改 .env 是**无效**的——而提示还理直气壮地写着「设环境变量 BACKEND_PORT」。
    这两项刻意不进 `app.config.Settings`（那是应用的配置，监听地址是启动器的事），
    所以必须由启动器自己读，也就必须由这条测试守住。
    """
    import sys

    sys.path.insert(0, str(_REPO / "backend"))
    import run

    monkeypatch.delenv("BACKEND_PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    (tmp_path / ".env").write_text(
        "# 注释行\nSECRET_KEY=x\n\nBACKEND_PORT=8931\nHOST='0.0.0.0'\n", encoding="utf-8")

    assert run._runtime_setting(tmp_path, "BACKEND_PORT", "8620") == "8931"
    assert run._runtime_setting(tmp_path, "HOST", "127.0.0.1") == "0.0.0.0"   # 去引号
    assert run._runtime_setting(tmp_path, "NOT_THERE", "fallback") == "fallback"

    monkeypatch.setenv("BACKEND_PORT", "9999")
    assert run._runtime_setting(tmp_path, "BACKEND_PORT", "8620") == "9999", "环境变量该优先"

    # 目录里没有 .env / 读不了 → 回落默认，绝不能因此起不来
    assert run._runtime_setting(tmp_path / "nope", "HOST", "127.0.0.1") == "127.0.0.1"


def test_env_template_documents_the_launcher_settings():
    """首启生成的 .env 模板里必须写着这两项——写不出来的配置项等于不存在。"""
    import sys

    sys.path.insert(0, str(_REPO / "backend"))
    import run

    tpl = run._ENV_TEMPLATE
    assert "BACKEND_PORT=" in tpl and "HOST=" in tpl
    assert "0.0.0.0" in tpl, "模板没告诉用户怎么开局域网访问"
    assert "密码" in tpl, "开局域网前必须提醒改默认密码"


def test_port_hint_tells_the_user_something_they_can_actually_do():
    """端口占用的提示要指向 `.env`，不能指向「设环境变量」——双击用户做不到。

    只看**代码行**：注释里出现这句话是在描述被修掉的旧行为，那是文档不是提示。
    （第一版这条测试 grep 了整个文件，于是被自己写的说明性注释判成违规。）
    """
    src = (_REPO / "backend" / "run.py").read_text(encoding="utf-8")
    code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    bad = [ln.strip() for ln in code if "设环境变量 BACKEND_PORT" in ln]
    assert not bad, f"提示还在让双击用户去设环境变量：{bad}"
    hints = [ln for ln in code if "BACKEND_PORT" in ln and ".env" in ln]
    assert hints, "端口相关的提示里一句都没提到 .env"


def test_build_script_warns_before_shipping_plugin_credentials():
    """打包者手边唯一的 plugins 就是仓库里那个开发用的——里面有 `.state/*.json`
    （他自己的淘宝登录会话）和 `.env`。

    插件现在**打进 exe**，所以这段话的分量比以前更重：随包分发一个文件夹时凭据
    还能事后删，**打进 exe 就再也删不掉了**。spec 里已经硬性剔除，
    但必须在打包日志里说出来——「我的 cookie 是不是在这个 exe 里」
    没人会主动去查。手工拷文件夹那条老路仍然存在，告诫也仍然要留着。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert "EXCLUDED from soroban.exe" in bat, "没说清凭据有没有被打进 exe"
    assert "DO NOT SHIP plugins" in bat, "手工拷贝那条路的告诫不该删掉"
    for token in (".state", ".env", ".log", ".venv"):
        assert token in bat, f"警告里没列出要删的 {token}"
    ship = [ln for ln in bat.splitlines() if "Ship a" in ln and "plugins" in ln]
    assert ship and any("delete" in ln.lower() for ln in ship), \
        "「拷 plugins 过去」这句话必须自带「先删掉凭据」"


def test_plugin_readme_documents_what_to_strip_before_sharing():
    readme = (_REPO / "plugins" / "README.md").read_text(encoding="utf-8")
    assert "发给别人之前" in readme
    for token in (".state/", ".env", "*.log", ".venv/"):
        assert token in readme, f"插件 README 没说要删 {token}"


# --- inherit 类插件：打包版必须真能跑起来 --------------------------------------

def test_spec_bundles_httpx_for_inherit_plugins(spec_text):
    """soroban 自己一行都不 import httpx，所以必须**显式**带进 exe。

    汇率插件写的是 `python = "inherit"`（跑在 exe 自己的解释器里）并 import httpx。
    只在 requirements.txt 里写着是不够的——PyInstaller 打的是**静态可达的模块图**，
    没人 import 它就不进包。表现是分发包上汇率永远取不到，
    而 exe 本身照常启动、打包日志里一个字都不提。
    """
    assert 'collect_submodules("httpx")' in spec_text, \
        "spec 没带 httpx —— 打包版的 inherit 类插件必然 ModuleNotFoundError"
    assert 'collect_data_files("certifi")' in spec_text, \
        "没带 certifi 的 CA 包，httpx 的 https 请求会全部证书校验失败"
    assert "import httpx" in spec_text and "打包中止" in spec_text, \
        "打包机缺 httpx 时没有让打包失败——会出一个「汇率是死的」的包"


def test_run_py_has_the_plugin_verb_and_it_precedes_the_whitelist():
    """`--run-plugin` 是打包版跑 inherit 插件的唯一出路，且必须**先于**参数白名单判。

    白名单那条规则是「整个 argv 必须落在集合里」，而 --run-plugin 后面跟的是插件
    自己的参数（fetch、--soroban-url…）。判晚了会被自己的保险打下来，
    表现是插件一启动就 exit 2，而错误信息说的是「soroban 不接受命令行参数」。
    """
    src = _RUN_PY.read_text(encoding="utf-8")
    assert "def _run_plugin(" in src, "打包版没有跑 inherit 插件的入口"
    i_verb = src.index('if argv[0] == "--run-plugin":')
    i_white = src.index('if set(argv) <= {"--use-local-db", "--yes"}')
    assert i_verb < i_white, "--run-plugin 判在白名单之后，会被白名单先拒掉"
    # 它必须支持 python 自己那两种入口写法，否则清单里换个 entry 就跑不了
    assert 'rest[0] == "-m"' in src and "run_path" in src, \
        "--run-plugin 没有按 python 的规矩解释 entry（-m 模块 / 脚本路径）"


def test_run_plugin_does_not_boot_the_server(tmp_path, monkeypatch):
    """真跑一遍：`--run-plugin` 必须执行插件入口，**不能**顺手把 soroban 启动起来。

    这条是整个动词的价值所在——它存在的理由正是「exe 被当解释器用时会把自己再启动一遍」。
    只断言源码里有那几个字符串是不够的：分支写错（比如落到 rescue 或继续往下走）
    在文本上完全看不出来。
    """
    import subprocess
    import sys

    d = tmp_path / "plug"
    (d / "demo_mod").mkdir(parents=True)
    (d / "demo_mod" / "__init__.py").write_text("", encoding="utf-8")
    (d / "demo_mod" / "__main__.py").write_text(
        "import sys; print('PLUGIN-RAN', sys.argv[1:])", encoding="utf-8")
    r = subprocess.run([sys.executable, str(_RUN_PY), "--run-plugin", str(d),
                        "-m", "demo_mod", "fetch", "--soroban-url", "http://x"],
                       capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert "PLUGIN-RAN" in r.stdout, f"插件没被跑起来：{r.stdout!r} {r.stderr[-400:]!r}"
    assert "'fetch', '--soroban-url', 'http://x'" in r.stdout, \
        "插件拿到的 argv 不对——命令与参数没原样传过去"
    assert "正在启动" not in r.stdout, "把 soroban 服务也启动了，这正是这个动词要消灭的事"


def test_spec_plugin_filter_really_excludes_credentials(spec_text, tmp_path):
    """把 spec 里那个过滤函数**真跑一遍**，别只比对字符串。

    这是整条改动里唯一「错了会把打包者的淘宝 cookie 焊进 exe」的地方，
    而字符串断言证明不了 `set(rel.parts[:-1]) & _PLUG_SKIP_DIRS` 这行逻辑是对的
    ——比如漏掉最后一段路径、或者把 startswith 写反，文本上完全看不出来。
    所以从 spec 源码里把那一段抠出来执行，测的就是真正会跑的代码。
    """
    import re as _re

    block = _re.search(r"^_PLUG_SKIP_DIRS = .*?^_plugin_files = ", spec_text,
                       _re.S | _re.M)
    assert block, "spec 里找不到插件过滤那一段——改了名字就把这条守卫架空了"
    ns = {"Path": Path}
    exec(compile(block.group(0).rsplit("\n_plugin_files", 1)[0], "<spec>", "exec"), ns)

    base = tmp_path / "plugins" / "soroban-plugin-demo"
    (base / ".state").mkdir(parents=True)
    (base / ".venv" / "bin").mkdir(parents=True)
    (base / "__pycache__").mkdir()
    (base / "pkg").mkdir()
    (base / ".state" / "session.json").write_text('{"cookie":"secret"}', encoding="utf-8")
    (base / ".venv" / "bin" / "python").write_text("x", encoding="utf-8")
    (base / "__pycache__" / "m.cpython-312.pyc").write_text("x", encoding="utf-8")
    (base / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (base / "scrape.log").write_text("x", encoding="utf-8")
    (base / "plugin.toml").write_text('id = "demo"', encoding="utf-8")
    (base / "README.md").write_text("hi", encoding="utf-8")
    (base / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    got = {Path(src).name for src, _dest in ns["_bundle_plugins"](tmp_path / "plugins")}
    assert got == {"plugin.toml", "README.md", "__init__.py"}, \
        f"过滤结果不对：{sorted(got)}——凭据或机器相关文件被打进了 exe"


def test_release_dir_does_not_carry_the_version():
    """发布目录**同时是运行数据目录**，所以它的名字里不能有版本号。

    带版本号时，改一次 VERSION 就产出一个全新的空目录：新 exe 首次启动会建一个
    空账本 + 新 SECRET_KEY，而用户读到的是「升级把我的数据吃了」。
    数据并没丢，它在上一个版本的目录里——但流程里没有任何一处会这么说。
    固定目录是把这个失败**消除**掉，而不是再加一句警告。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^set RELEASE=(.+)$", bat, re.M)
    assert m, "pyinstaller.bat 里找不到 RELEASE 的定义"
    assert "%VERSION%" not in m.group(1), \
        f"发布目录又带上版本号了：{m.group(1)}——升级会产出空账本"
    # 老用户的数据还在带版本号的旧目录里，那句一次性搬迁提示不能删
    assert "YOUR LEDGER IS NOT IN THIS FOLDER" in bat


def test_pyinstaller_bat_does_not_point_windows_users_at_a_venv_start_bat_never_makes():
    """打包脚本不许再叫 Windows 用户「跑 start.bat 建 venv」——那个 venv 它从来不建。

    `start.bat` 在 Windows 上创建/激活的是 **conda 环境 `soroban`**（见它自己的
    `CONDA_ENV_NAME` 与 `conda create -n ... python=3.12`）；`backend\\.venv` 是
    `start.sh` 在 Linux/macOS 上的产物。所以「run start.bat once to create the venv」
    这句指引在 Windows 上**永远不可能成立**——它指着一个 start.bat 不会写的路径。

    这三句自 `ceee9b7` 起就是错的，而且在审计报告里被记过三次没人动。
    改字符串成本为零，留着的成本是照文档走的人拿不到正确指引。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    for wrong in ("start.bat once to create the venv",
                  "run start.bat first for a clean env",
                  "start.bat creates it"):
        assert wrong not in bat, f"又出现了那句对 Windows 不成立的指引：{wrong}"
    assert "conda activate soroban" in bat, "没告诉 Windows 用户真正该做什么"


def test_pyinstaller_bat_gates_the_python_version_on_both_ends():
    """打包脚本要有**上下界**的 Python 版本闸。

    下界 3.11 与 start.bat 一致；上界是真正会咬人的那一头：`rapidocr_onnxruntime`
    不发 3.13 的 wheel（start.bat 只在注释里提了一句，两个脚本都没有真的挡）。
    在 3.13 上，依赖装到一半失败，pip 的报错完全不提这件事——而如果那步被跳过，
    打出来的 exe 里 OCR 是死的，只在用户机器上才发现。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert "(3,11) <= sys.version_info < (3,13)" in bat, "没有上下界都管的版本闸"
    assert ":bad_python_version" in bat, "闸挡下之后没有可诊断的出口"
    assert "3.13" in bat and "rapidocr" in bat, "没说清为什么 3.13 不行"


# --- 启动器把解析出来的 HOST/PORT 交给应用 ----------------------------------------

def test_runtime_host_and_port_reach_the_app_before_it_is_imported():
    """`.env` 这一档**只有启动器读得到**（pydantic-settings 读 .env 也不写回 os.environ），
    而应用侧有两个模块在**导入期**只认环境变量：

      · `main._LAN` —— 决定要不要关掉 /docs、/openapi.json。读不到用户在 .env 里设的
        HOST ⇒ 恒判「环回」⇒ 用户按模板把 HOST 改成 0.0.0.0 想用手机记账，
        局域网里任何设备都能免登录打开完整 API 地图，而「已关闭 /docs」那条日志也不会出现。
      · `plugins._SELF_URL` —— 插件回灌地址。用户照本程序自己的提示把 BACKEND_PORT
        改成 8621 之后，插件仍把数据连同 Bearer 令牌 POST 到 8620。

    所以两条赋值必须排在 `from app.main import app` **之前**。这是顺序守卫：
    位置错了（或被删掉）就红。
    """
    import ast
    import inspect
    import textwrap

    import run as run_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(run_mod.main)))
    set_at, import_at = {}, None
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            seg = ast.dump(node)
            for name in ("HOST", "BACKEND_PORT"):
                if f"'{name}'" in seg or f'"{name}"' in seg:
                    set_at.setdefault(name, node.lineno)
        if isinstance(node, ast.ImportFrom) and node.module == "app.main":
            import_at = node.lineno
    assert set(set_at) == {"HOST", "BACKEND_PORT"}, f"启动器没把 {set_at} 落进 os.environ"
    assert import_at, "找不到 `from app.main import app`"
    for name, line in set_at.items():
        assert line < import_at, f"os.environ[{name!r}] 排在 app.main 导入之后，来不及生效"


def test_uvicorn_startup_failure_is_not_a_silent_flash():
    """**uvicorn 不把启动失败抛给调用方**：它自己 `except OSError → sys.exit(3)`，
    lifespan 里抛异常同样被转成 `sys.exit(3)`。于是 `except OSError` 那一支是死代码，
    而 `__main__` 里的 `except SystemExit: raise` 会让打包版闪退——
    数据库连不上的那段中文指引、单实例闸的解释全都打印出来了，然后窗口一秒内消失。

    这里钉的是「`uvicorn.run` 外面必须有一支 `except SystemExit` 且它会调 `_fatal`」。
    """
    import ast
    import inspect
    import textwrap

    import run as run_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(run_mod.main)))
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        calls = {getattr(n.func, "attr", getattr(n.func, "id", ""))
                 for n in ast.walk(node) if isinstance(n, ast.Call)}
        if "run" not in calls:                      # 不是包着 uvicorn.run 的那个 try
            continue
        for h in node.handlers:
            names = ast.dump(h.type or ast.Pass())
            if "SystemExit" in names and "_fatal" in ast.dump(ast.Module(h.body, [])):
                ok = True
    assert ok, "uvicorn.run 外面没有会调 _fatal 的 `except SystemExit` —— 打包版会闪退"


def test_env_values_tolerate_a_trailing_comment(tmp_path, monkeypatch):
    """用户照着提示改端口时很自然会写 `BACKEND_PORT=8621  # 换个端口`。

    不剥行尾注释的话 `int()` 抛 ValueError，落到 `__main__` 的兜底里 ——
    而那句提示写的是「数据库文件损坏或被占用、SECRET_KEY 被改坏、磁盘满」，
    把用户往完全错误的方向指。（这条路径**会** pause，所以不是闪退，是指错方向。）
    """
    import run as run_mod

    monkeypatch.delenv("BACKEND_PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    (tmp_path / ".env").write_text(
        "BACKEND_PORT=8621  # 换个端口\n"
        "HOST=0.0.0.0   # 局域网\n"
        # **带引号时 `#` 是内容不是注释**：这一条防止把剥注释写成无脑 split
        "SOROBAN_NOTE='a#b'\n",
        encoding="utf-8")

    assert run_mod._runtime_setting(tmp_path, "BACKEND_PORT", "8620") == "8621"
    assert int(run_mod._runtime_setting(tmp_path, "BACKEND_PORT", "8620")) == 8621
    assert run_mod._runtime_setting(tmp_path, "HOST", "127.0.0.1") == "0.0.0.0"
    assert run_mod._runtime_setting(tmp_path, "SOROBAN_NOTE", "") == "a#b"

    # **反面**：没有注释的普通值不能被这段处理弄坏
    (tmp_path / ".env").write_text("BACKEND_PORT=8622\n", encoding="utf-8")
    assert run_mod._runtime_setting(tmp_path, "BACKEND_PORT", "8620") == "8622"


def test_database_init_happens_after_the_single_process_gate():
    """建表/迁移与建管理员都必须排在**单进程闸之后**。

    `main.lifespan` 特意把 `single_process.acquire()` 放在 `create_db_and_tables()`
    之前，理由原话是「多 worker 时后来的那些会在这里当场退出，而不是先各自跑一遍迁移
    ——幂等归幂等，但那是几个进程同时 ALTER 同一个库」。

    而启动器原先在 `uvicorn.run()` **之前**调 `seed.main()`，那个函数自己会先
    `create_db_and_tables()` ⇒ 完整 alembic upgrade 跑在闸之前。
    改端口开第二个实例时，新进程会对**正在被老进程使用的库**跑完迁移，
    然后才在 lifespan 里被闸拒绝。

    两头都钉：lifespan 里的顺序，以及启动器不许自己碰库。
    """
    import ast
    import inspect
    import textwrap

    import run as run_mod
    from app import main as main_mod

    # ① lifespan 里：acquire → create_db_and_tables → ensure_admin
    tree = ast.parse(textwrap.dedent(inspect.getsource(main_mod.lifespan)))
    order = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("acquire", "create_db_and_tables", "ensure_admin"):
                order.setdefault(name, node.lineno)
    assert set(order) == {"acquire", "create_db_and_tables", "ensure_admin"}, order
    assert order["acquire"] < order["create_db_and_tables"] < order["ensure_admin"], (
        f"lifespan 里的顺序不对：{order}（闸 → 建表 → 建号）")

    # ② 启动器不许在 uvicorn 之前碰库
    launcher = ast.parse(textwrap.dedent(inspect.getsource(run_mod.main)))
    touched = {getattr(n.func, "attr", getattr(n.func, "id", ""))
               for n in ast.walk(launcher) if isinstance(n, ast.Call)}
    bad = touched & {"create_db_and_tables", "seed_admin", "ensure_admin"}
    assert not bad, (f"启动器在拿到单进程闸之前就碰库了：{sorted(bad)}。"
                     "这些事都该在 app.main.lifespan 里做")
