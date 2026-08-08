"""插件私有存储（与账本隔离）。

**唯一的通用扩展位**：插件需要「属于自己的可写状态」时用它，不必为每种新数据加一张表。
典型用途：物流轨迹去重、上次轮询时间、比价快照。

刻意 schema-free（`data` 存 JSON 文本）：核心不解释内容，只保证
① 命名空间隔离（按 plugin_id 分区，插件之间读不到彼此的）
② 长度上限（见 ingest/kinds/plugin_record.py）

**账本金额绝不能依赖它**——没有外键、没有约束、跨方言 JSON 查询能力弱。
需要参与记账的数据必须有自己的列和迁移。
"""
import datetime as dt
from typing import Optional

from sqlalchemy import Index, Text
from sqlmodel import Field, SQLModel

from ...db.dialect import BinStr, UtcDateTime
from ..base import utcnow


class PluginRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # 三元组唯一：同一插件、同一自定义类型下，key 不重复。
    # 用 BinStr 与全库键列同口径——否则 SQLite 区分大小写、MySQL 默认不区分，
    # 同一份数据两种后端行为不同（本项目栽过的双引擎发散）。
    plugin_id: str = Field(max_length=64, sa_type=BinStr(64))
    kind: str = Field(max_length=64, sa_type=BinStr(64))
    key: str = Field(max_length=128, sa_type=BinStr(128))
    data: str = Field(sa_type=Text)                       # JSON 文本，核心不解释
    created_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
    updated_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())

    __table_args__ = (
        Index("ix_pluginrecord_owner", "plugin_id", "kind", "key", unique=True),
    )
