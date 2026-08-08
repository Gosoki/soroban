"""插件私有存储。**这套设计里唯一的通用扩展位。**

它一次性解决三件今天做不到的事：
  · 物流轨迹推送要记「已经推过哪些事件」——插件没有属于自己的可写状态；
  · 快递查询要记「上次轮询时间」——省 API 计费；
  · 比价要落报价快照——账本里根本没有对应概念。

**明确不让账本金额依赖它**：schema-free、无外键、无约束、跨方言 JSON 查询能力弱。
需要参与记账的数据必须有自己的表和列（那时是加一个 handler + 一次迁移，
仍然不用开新接口）。
"""
from __future__ import annotations

import json

from pydantic import ConfigDict
from sqlmodel import Field, Session, SQLModel, select

from ....models import PluginRecord, utcnow
from .. import Ctx, Outcome, register

_MAX_BYTES = 64 * 1024      # 单条上限：核心不解释内容，但要防一条记录把库撑爆


class PluginRecordIn(SQLModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=64)      # 插件自定义（"tracking_event" / "price_snapshot"）
    key: str = Field(max_length=128)      # 同一 (plugin, kind) 下的唯一键
    data: dict                            # 内容核心不解释，只保证命名空间隔离与长度上限


@register(kind="plugin.record", label="插件数据", scope="data:own",
          schema=PluginRecordIn, key=lambda it: f"{it.kind}|{it.key}", max_batch=500)
def apply_plugin_record(session: Session, item: PluginRecordIn, ctx: Ctx) -> Outcome:
    blob = json.dumps(item.data, ensure_ascii=False)
    if len(blob.encode("utf-8")) > _MAX_BYTES:
        return Outcome("rejected", code="validation",
                       message=f"单条数据超过 {_MAX_BYTES // 1024}KB")
    row = session.exec(
        select(PluginRecord).where(PluginRecord.plugin_id == ctx.plugin_id,
                                   PluginRecord.kind == item.kind,
                                   PluginRecord.key == item.key)
    ).first()
    if row is None:
        row = PluginRecord(plugin_id=ctx.plugin_id, kind=item.kind, key=item.key, data=blob)
        session.add(row)
        session.flush()
        return Outcome("created", id=row.id)
    if row.data == blob:
        return Outcome("unchanged", id=row.id)
    row.data, row.updated_at = blob, utcnow()
    session.add(row)
    session.flush()
    return Outcome("updated", id=row.id)
