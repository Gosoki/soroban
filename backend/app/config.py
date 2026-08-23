"""App configuration. Reads from environment / .env (see .env.example)."""

from decimal import Decimal
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .paths import runtime_dir

# 汇率合理区间 + 量化精度：**唯一真相**，手填校验(schemas)与抓取入库(services/fx)共用，
# 避免两处各写一份而悄悄漂移（1 CNY = X JPY，X≈20）。
FX_MIN = Decimal("5")
FX_MAX = Decimal("50")
FX_QUANTUM = Decimal("0.0001")

# 金额上限（唯一真相；schemas 手填校验与 models.compute_money 派生校验共用）：
# - CNY_MAX = Numeric(12,2) 上限，防人民币列溢出 + 防 quantize 越精度抛 InvalidOperation。
# - JPY_MAX = 有符号 INT 上限，防 jpy_auto/jpy_settled 溢出（MySQL 报 Out of range → 500，
#   SQLite 静默存超大数 → 双引擎发散）。派生日元 = 货款×汇率，故必须在 compute_money 也卡一次。
CNY_MAX = Decimal("9999999999.99")
JPY_MAX = 2_147_483_647


class Settings(BaseSettings):
    # `.env` 锚到**运行时目录**，不是当前工作目录。写成 ".env" 的话，
    # 在别的目录里跑 `python -m app.X` / `python -m tools.X` 会**找不到它**，
    # 于是 SECRET_KEY 静默退回下面那个默认值——而控制库里的 MySQL 连接串正是用它加密的，
    # 解不开就会静默降级成一个空的本地库。详见 app/paths.py。
    model_config = SettingsConfigDict(env_file=runtime_dir() / ".env", extra="ignore")

    SECRET_KEY: str = "dev-insecure-key-change-me"
    # 本地 SQLite 文件位置（控制/配置库，恒 SQLite）。是否走 MySQL 由应用内「数据库」页
    # 迁移决定，连接串加密存入 SQLite 的 app_db_config，运行期热切换（见 app/database.py、
    # app/db/control.py）。此处即使填 MySQL 串也会被忽略、回退到默认 SQLite 文件。
    DATABASE_URL: str = "sqlite:///./soroban.db"
    TOKEN_EXPIRE_DAYS: int = 90  # 登录有效期 3 个月
    ALGORITHM: str = "HS256"

    # --- 汇率的**币种口径** ---
    # 核心**不抓**汇率：抓取全部在 `plugins/soroban-plugin-fx` 里，走通用写入通道回灌。
    # 这两个键留在这里是因为它们描述的是「这本账按什么折算」（CNY→JPY），
    # 属于核心的记账口径，不是某个数据源的配置。
    FX_BASE: str = "CNY"
    FX_QUOTE: str = "JPY"
    # Frontend dev origin(s) for CORS（默认前端端口 8621；同源托管/vite 代理下其实用不到）。
    CORS_ORIGINS: list[str] = ["http://localhost:8621", "http://127.0.0.1:8621"]

    # --- 插件目录（soroban 扫这里的 soroban-plugin-* 子目录）---
    # 默认 = soroban 仓库下的 plugins/；可用环境变量覆盖到别处。
    PLUGIN_DIR: Optional[str] = None
    # 旧名，只为兼容已有 .env。新部署用 PLUGIN_DIR——插件已经不只是爬虫了
    # （汇率、国际快递查询都没有「爬」的语义）。两个都设时以 PLUGIN_DIR 为准。
    SCRAPER_DIR: Optional[str] = None

    @field_validator("DATABASE_URL")
    @classmethod
    def _anchor_sqlite_path(cls, v: str) -> str:
        """相对的 sqlite 路径锚到**运行时目录**，不跟着当前工作目录跑。

        原先 `sqlite:///./soroban.db` 是按 CWD 解析的：在别的目录里导入 `app.database`
        会当场新建一个空库并把它当成账本（SQLite 模式下控制库就是账本本体）。
        应用照常启动、一条报错都没有，只是账本是空的。

        只动「相对的 sqlite 路径」这一种：内存库（`sqlite://`、`:memory:`）、
        绝对路径（四道斜杠）、以及 MySQL 串全部原样放行。
        测试把 DATABASE_URL 设成绝对临时路径，因此不受影响。
        """
        if not v.startswith("sqlite"):
            return v
        from sqlalchemy.engine import make_url

        try:
            url = make_url(v)
        except Exception:                       # noqa: BLE001 - 交给 create_engine 去报真正的错
            return v
        db = url.database
        if not db or db == ":memory:" or Path(db).is_absolute():
            return v
        return str(url.set(database=str((runtime_dir() / db).resolve())))


settings = Settings()
