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
  backend/app/routers/plugins.py  _SOROBAN_ROOT = exe 同级          → plugins/ **不打包**，随包放 exe 旁边
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
# RapidOCR 自带 ONNX 模型 + 配置 yaml，全是运行时按路径读的
datas += collect_data_files("rapidocr_onnxruntime")

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
