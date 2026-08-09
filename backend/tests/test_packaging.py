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


def test_scraper_is_not_bundled(spec_text):
    """插件各自带 venv + Playwright，体积巨大且要能单独更新；plugins.py 冻结后从
    **exe 同级目录**找它们，绝不该被打进包里。"""
    plug = (_REPO / "backend" / "app" / "routers" / "plugins.py").read_text(encoding="utf-8")
    assert "Path(sys.executable).resolve().parent" in plug
    # 查的是「插件目录有没有被列进打包内容」，不是查某个词是否出现——
    # 目录从 scraper/ 改名成 plugins/ 之后，原来那句 `"scraper" not in ...` 变成了恒真的空话。
    head = spec_text.split("# --- 静态分析")[0]
    for name in ("plugins", "scraper"):
        assert f"{name}/" not in head.replace(f"{name}/ ", "").replace(f"`{name}/`", ""), \
            f"spec 不该把 {name}/ 打进 exe（插件各自带 venv+Playwright，体积巨大且要能单独更新）"


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
    """分发指引说「把 plugins 文件夹拷到 exe 旁边」，而打包者手边唯一的 plugins
    就是仓库里那个开发用的——里面有 `.state/*.json`（他自己的淘宝登录会话）和 `.env`。

    `.gitignore` 覆盖了它们，但**分发是文件拷贝，git 没有发言权**。
    照着说明发一次版，就等于把自己的登录态发出去了。
    """
    bat = (_REPO / "pyinstaller.bat").read_text(encoding="utf-8", errors="replace")
    assert "DO NOT SHIP plugins" in bat, "缺少「别原样分发 plugins」的警告"
    for token in (".state", ".env", ".log", ".venv"):
        assert token in bat, f"警告里没列出要删的 {token}"
    # 那句「ship a plugins folder」本身也得带上告诫，别让人只看见前半句
    ship = [ln for ln in bat.splitlines() if "Ship a" in ln and "plugins" in ln]
    assert ship and any("delete" in ln.lower() for ln in ship), \
        "「拷 plugins 过去」这句话必须自带「先删掉凭据」"


def test_plugin_readme_documents_what_to_strip_before_sharing():
    readme = (_REPO / "plugins" / "README.md").read_text(encoding="utf-8")
    assert "发给别人之前" in readme
    for token in (".state/", ".env", "*.log", ".venv/"):
        assert token in readme, f"插件 README 没说要删 {token}"
