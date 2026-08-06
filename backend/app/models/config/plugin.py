"""爬虫插件配置（soroban 做管理层：存每个插件的启用/参数/定时；插件本体在 scraper/ 下）。"""

import datetime as dt
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from ...db.dialect import UtcDateTime
from ..base import utcnow


class PluginConfig(SQLModel, table=True):
    plugin_id: str = Field(primary_key=True, max_length=64)  # 对应 plugin.toml 的 id
    enabled: bool = Field(default=False)                    # 是否启用（定时抓取才生效）
    params_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))  # 用户在插件管理页填的参数（如 accounts）
    schedule_minutes: int = Field(default=0)               # 定时抓取间隔（分钟），0=不定时
    last_run_at: Optional[dt.datetime] = Field(default=None, sa_type=UtcDateTime())  # 上次自动抓取时间（定时循环判断用）
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
