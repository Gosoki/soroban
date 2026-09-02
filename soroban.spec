# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包清单（供 pyinstaller.bat 调用）。

⚠️ 本文件是**手写并必须提交**的。标准 Python .gitignore 里有 `*.spec`，曾把它整个忽略掉，
   导致全新 clone 打包时在最后一步报「spec 找不到」——.gitignore 里已加 `!soroban.spec` 反排除。

打包成**单文件** soroban.exe：一个进程同时提供 API 与前端页面（同源、单端口）。
运行时 backend/run.py 会先 `os.chdir(exe 同级目录)`，于是 `.env`、`soroban.db`、`plugins/`
都相对 exe 解析——整包可随目录一起分发。

下面每一项 datas / hiddenimports 都对应代码里一处**运行时按路径或按名字加载**的东西，
PyInstaller 静态分析看不到，必须显式声明。改代码时若动了这些路径，记得同步改这里：

  backend/app/database.py      _ROOT = Path(sys._MEIPASS)          → 需要 alembic.ini + alembic/
  backend/app/main.py          _DIST = _MEIPASS/frontend/dist      → 需要 frontend/dist
  backend/app/services/ocr.py  Path(__file__).with_name(...png)    → 需要 app/services/xianyu_truck.png
  backend/app/routers/plugins.py  _SOROBAN_ROOT = exe 同级          → 插件在**磁盘上**的家，见下
  backend/run.py               --run-plugin 内部动词               → 见下面 httpx 那一段

插件（plugins/soroban-plugin-*）现在**打进 exe**，首次运行时由 `seed_bundled_plugins()`
释放到 exe 同级的 plugins/。必须落到磁盘、不能就地跑：插件目录是**可写**的
（各自的 .venv、登录会话 .state、参数），而 onefile 的 _MEIPASS 每次退出就被清掉。
打包时刻意剔掉三类东西，见下面 _PLUG_SKIP_* 的说明。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)                    # noqa: F821  SPECPATH 由 PyInstaller 注入
BACKEND = ROOT / "backend"

# --- 运行时按路径读取的资源 ---------------------------------------------------
datas = [
    # Alembic：启动时 `upgrade head`（database.run_migrations 用 _MEIPASS 定位这两项）。
    # versions/*.py 是被 Alembic 按文件路径动态加载的，所以是**数据**而不是模块。
    (str(BACKEND / "alembic.ini"), "."),
    (str(BACKEND / "alembic"), "alembic"),
    # 前端构建产物：main.py 检测到就同源托管。打包前 pyinstaller.bat 已跑过 npm run build。
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    # 闲鱼卡通卡车模板图：ocr.py 用 Path(__file__).with_name() 读，必须与模块同目录。
    (str(BACKEND / "app" / "services" / "xianyu_truck.png"), "app/services"),
]
# RapidOCR 自带 ONNX 模型 + 配置 yaml，全是运行时按路径读的。
#
# **打包机缺这个包时必须让打包失败，不能出一个残废的包。**
# `collect_data_files` / `collect_submodules` 对装不上的包只发一句 WARNING 并返回**空列表**，
# PyInstaller 照常出包——发到用户手里的 exe，OCR 是死的（`_get_engine()` 抛 OcrUnavailable
# → 每次上传截图收 503），而这在打包日志里只是几百行输出中的一行黄字。
# 更糟的是那句报错让用户「在 backend 下 pip install」——分发包里根本没有 backend 目录。
# 同理检查 PIL：解码这一步没有它，OCR 一样是死的。
_ocr_models = collect_data_files("rapidocr_onnxruntime")
if not _ocr_models:
    raise SystemExit(
        "打包中止：找不到 rapidocr_onnxruntime 的模型数据。\n"
        "  打包机上没装它（或装了但没有模型文件）。继续打下去会得到一个 OCR 全废的 exe，\n"
        "  而这个故障只有到了用户手里才会暴露。\n"
        "  解决：在**用来打包的那个解释器**里跑 pip install -r backend/requirements.txt"
    )
datas += _ocr_models
try:
    import PIL  # noqa: F401  仅探测；ocr.py 在解码那一步 import 它
except ImportError:
    raise SystemExit(
        "打包中止：打包机上没装 pillow。OCR 的图片解码全靠它，缺了 exe 的 OCR 是死的。\n"
        "  解决：在**用来打包的那个解释器**里跑 pip install -r backend/requirements.txt"
    )

# --- 静态分析看不到的模块 -----------------------------------------------------
hiddenimports = [
    # passlib 按名字延迟加载后端；bcrypt 本体也要带上
    "passlib.handlers.bcrypt",
    "bcrypt",
    # python-jose 按名字挑加密后端
    "jose.backends.cryptography_backend",
    # MySQL 驱动：连接串里写的是 mysql+pymysql://，SQLAlchemy 按名字 import
    "pymysql",
    # Alembic 迁移脚本里 import 的东西（脚本本身是数据文件，其依赖要显式带）
    "alembic.autogenerate",
    "app.db.dialect",
]
# uvicorn/rapidocr 的子模块都是按名字动态挑选（协议实现、事件循环、推理后端）
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("rapidocr_onnxruntime")

# --- inherit 类插件的依赖：httpx ------------------------------------------------
# soroban 自己**一行都不 import httpx**（汇率抓取搬进了插件），所以 PyInstaller 的
# 静态分析根本看不见它——requirements.txt 里写着不算数，进不了 exe。
# 而 `python = "inherit"` 的插件（汇率）跑在 exe 自己的解释器里、`import httpx`：
# 不显式带上，run.py 的 `--run-plugin` 一路走通到最后一步才 ModuleNotFoundError，
# 表现就是**分发包上汇率永远取不到**，而打包日志里一个字都不会提。
#
# 同 OCR 那两段一个道理：打包机缺它就让打包失败，别出一个「汇率是死的」的包。
try:
    import httpx  # noqa: F401
except ImportError:
    raise SystemExit(
        "打包中止：打包机上没装 httpx。汇率插件与 soroban 共用解释器、直接 import 它，\n"
        "  缺了的话分发包上汇率永远取不到（而 exe 本身照常启动，故障只有到了用户手里才暴露）。\n"
        "  解决：在**用来打包的那个解释器**里跑 pip install -r backend/requirements.txt"
    )
hiddenimports += collect_submodules("httpx")
# httpx 校验 TLS 用 certifi 的 CA 包（一个**数据文件**，不是模块）。
# 不带上的话 https 请求全部 SSLCertVerificationError——同样只在用户机器上出现。
datas += collect_data_files("certifi")

# --- 插件源码：打包机上有什么就带什么 -------------------------------------------
# 原先插件完全不打包，靠打包者「记得把 plugins 文件夹复制到 exe 旁边」。
# 那一步没有任何东西会提醒他做，漏了的表现是**整个插件页空空如也**，
# 而 exe 本身一切正常——最难往「是不是少复制了个文件夹」上想的一种故障。
#
# 剔掉的三类，都不是为了省体积，是因为带上就是错的：
#   1. **凭据**：.state/（打包者自己的淘宝登录 cookie）、.env、*.log。
#      原先那段红色警告说的正是这个——但「随包分发一个文件夹」还能事后删，
#      **打进 exe 就再也删不掉了**，所以这里必须是硬性剔除，不能靠人记得。
#   2. **机器相关**：.venv/（写死了打包机的路径，到别人机器上根本跑不起来），
#      而且体积以百 MB 计。到了用户那边由插件页的「一键安装」重建。
#   3. **构建垃圾**：__pycache__、*.pyc、.git、node_modules。
# 除此之外一律照带（含 README、tests、LICENSE）——「打包机上有什么就有什么」
# 是一条不需要维护的规则，而白名单要求每加一种文件就回来改一次这里。
# 打包插件时剔除的三类东西。**这不是优化，是前提条件**：
# 随包分发一个文件夹时，多带的东西还能事后删；**打进 exe 就再也删不掉了**。
#
# 后一半（docs / tests / captures / examples）是 2026-09-02 补的，起因很具体：
# `plugins/soroban-plugin-taobao/docs/captures/fetch-c233-20260806-133510.json`
# 是一份 607 KB 的**真实淘宝抓包**——43 个订单号、167 个 orderId、94 个商品标题、
# 182 处金额，全是打包者本人的单。它是所有被打包插件文件里**最大的那一个**，
# 会随 exe 分发、并由 `seed_bundled_plugins()` 写到**每个用户的磁盘上**。
# 与 `.state`（打包者的登录 cookie）是同一类东西，只是当时的清单没想到它。
#
# 现在的口径是「只带跑得起来所必需的」：源码、plugin.toml、requirements.txt、
# README、LICENSE。开发期产物一律不带——运行时代码里没有任何一处读插件的
# docs/ 或 tests/（已 grep 确认）。
_PLUG_SKIP_DIRS = {".venv", ".state", "__pycache__", ".git", "node_modules", ".pytest_cache",
                   "docs", "tests", "captures", "examples", ".github", ".idea", ".vscode"}
_PLUG_SKIP_NAMES = {".env"}
_PLUG_SKIP_SUFFIXES = (".pyc", ".pyo", ".log")


def _bundle_plugins(base):
    out = []
    if not base.is_dir():
        return out
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(base)
        if set(rel.parts[:-1]) & _PLUG_SKIP_DIRS:
            continue
        if rel.name in _PLUG_SKIP_NAMES or rel.name.endswith(_PLUG_SKIP_SUFFIXES):
            continue
        out.append((str(f), str(Path("plugins") / rel.parent)))
    return out


_plugin_files = _bundle_plugins(ROOT / "plugins")
print(f"[soroban.spec] 打包 {len(_plugin_files)} 个插件文件"
      f"（已剔除 .state/.env/*.log/.venv/__pycache__）")
datas += _plugin_files

a = Analysis(                            # noqa: F821
    [str(BACKEND / "run.py")],
    pathex=[str(BACKEND)],               # 让 `from app.main import app` 能解析
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 打包体积：这些 GUI/绘图库没用到，onnxruntime 会顺手把它们拖进来
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "PySide6", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)                        # noqa: F821

exe = EXE(                               # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="soroban",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,                        # 保留控制台：启动日志/迁移进度/错误都要看得见
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "frontend" / "public" / "favicon.ico"),
)
