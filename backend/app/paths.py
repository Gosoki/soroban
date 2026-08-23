"""运行时数据目录的**唯一真相**。刻意零副作用（只 import sys/pathlib）。

为什么要单独一个模块：`run.py` 必须在 `ensure_env()` **之前**就知道这个目录，
而那时还不能 import `app.config`（它在导入时就会去读 `.env` 定 SECRET_KEY，
读早了会读到一个还没被建出来的文件）。所以这里不能有任何 import 副作用。

**这个目录解决的问题**：`.env` 与 `sqlite:///./soroban.db` 原先都是**按当前工作目录**
解析的。于是在别的目录里跑任何一条 `python -m app.X` / `python -m tools.X`：

  · `.env` 找不到 ⇒ `SECRET_KEY` 退回默认值 ⇒ 控制库里加密的 MySQL 连接串**再也解不开**
    （`read_config` 会静默降级成空的本地库）；
  · `sqlite:///./soroban.db` 指向那个目录 ⇒ 当场新建一个**空账本**，
    而 SQLite 模式下控制库就是账本本体（见 `database._resolve_data_engine`）。

两件事叠起来的现象是：应用**完全正常地启动**，只是账本空空如也。
仓库根目录那个只有控制表、没有任何业务表的 `soroban.db` 就是这么来的。
"""
from __future__ import annotations

import sys
from pathlib import Path


def runtime_dir() -> Path:
    """运行时数据目录：打包后取 exe 同级，源码运行取 `backend/`。

    打包态用 `sys.executable` 而不是 `__file__`：PyInstaller 把源码解到一个临时目录，
    `__file__` 指的是那里——数据落进去等于每次退出就没了。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent      # app/paths.py → app/ → backend/
