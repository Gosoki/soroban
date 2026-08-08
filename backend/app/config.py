"""App configuration. Reads from environment / .env (see .env.example)."""

from decimal import Decimal
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

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
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SECRET_KEY: str = "dev-insecure-key-change-me"
    # 本地 SQLite 文件位置（控制/配置库，恒 SQLite）。是否走 MySQL 由应用内「数据库」页
    # 迁移决定，连接串加密存入 SQLite 的 app_db_config，运行期热切换（见 app/database.py、
    # app/db/control.py）。此处即使填 MySQL 串也会被忽略、回退到默认 SQLite 文件。
    DATABASE_URL: str = "sqlite:///./soroban.db"
    TOKEN_EXPIRE_DAYS: int = 90  # 登录有效期 3 个月
    ALGORITHM: str = "HS256"

    # --- Exchange rate (open.er-api.com, free, no key) ---
    # 抓取逻辑从 hiyori 移植、本项目自带一份；不调用 hiyori 的接口（部署位置可能不同）。
    FX_BASE: str = "CNY"
    FX_QUOTE: str = "JPY"
    FX_REFRESH: int = 21600  # 6h；免费源约每日更新一次
    # Frontend dev origin(s) for CORS（默认前端端口 8621；同源托管/vite 代理下其实用不到）。
    CORS_ORIGINS: list[str] = ["http://localhost:8621", "http://127.0.0.1:8621"]

    # --- 插件目录（soroban 扫这里的 soroban-plugin-* 子目录）---
    # 默认 = soroban 仓库下的 plugins/；可用环境变量覆盖到别处。
    PLUGIN_DIR: Optional[str] = None
    # 旧名，只为兼容已有 .env。新部署用 PLUGIN_DIR——插件已经不只是爬虫了
    # （汇率、国际快递查询都没有「爬」的语义）。两个都设时以 PLUGIN_DIR 为准。
    SCRAPER_DIR: Optional[str] = None


settings = Settings()
