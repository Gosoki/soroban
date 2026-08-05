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
    assert "scraper" not in spec_text.split("# --- 静态分析")[0].replace("scraper/", "×"), \
        "spec 不该把 scraper/ 打进 exe"


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
