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
    # 用户授予本插件的权限（JSON 数组）。空 = 没授权过 → 插件拿到的令牌什么门都进不去。
    # **默认拒绝**：插件升级后自己在清单里多写一项 scope 不会自动生效，
    # 卡片上会标「需要新授权」——`git pull` 不该悄悄扩大权限面。
    # 用 VARCHAR 而不是 Text：**MySQL 的 TEXT/BLOB 列不能有 DEFAULT**（错误 1101），
    # 而 SQLite 照单全收——不带长度的话就是「本地全绿、切到 MySQL 迁移直接失败」，
    # 本项目最常见的那类双引擎发散。512 够放满这张权限表里的全部 scope 名。
    granted_scopes: str = Field(default="[]", max_length=512, nullable=False)
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
