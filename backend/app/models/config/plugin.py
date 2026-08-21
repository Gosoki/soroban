"""插件配置（soroban 做管理层：存每个插件的启用/授权/参数/定时/上次结果；
插件本体在 `plugins/soroban-plugin-*/` 下，各自成库、各自 venv）。

「爬虫」这个词已经不准确了：汇率插件不爬任何东西。插件 = 外部数据来源，爬只是其中一种。"""

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
    # 上一次执行的结果。原先只有 last_run_at（还只在定时路径推进），
    # 于是「跑完了吗、成没成、抓了几条」全埋在日志里——界面上一个字都看不到。
    # 插件是 fire-and-forget 的，没有这三列就等于没有反馈。
    # 刻意**不叫 last_status**：本项目的公理是「叫 status 的列必须是某个业务段的状态机」
    # （purchase_status / shipment_status / import_status）。这一列是「上次跑得怎么样」，
    # 不是状态机——沿用 status 会让它被 tests/test_status_taxonomy.py 的登记表要求表态，
    # 而它压根不属于任何业务段。那条守卫这次就当场抓到了我。
    last_outcome: str = Field(default="", max_length=16)     # ok | failed | running | ""
    last_summary: str = Field(default="", max_length=512)   # 给人看的一句话（插件回的 JSON 摘要）
    last_finished_at: Optional[dt.datetime] = Field(default=None, sa_type=UtcDateTime())
    # 上一次**成功**跑完的时间。与 `last_finished_at` 分开是必须的：那一列成功失败都推进，
    # 于是一个登录会话已经过期两周、每次定时都照跑照失败的插件，卡片上依然显示一个
    # 很新的时间戳。「最近跑过」和「最近抓到过东西」是两件事，而用户只会看前者。
    last_ok_at: Optional[dt.datetime] = Field(default=None, sa_type=UtcDateTime())
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
