"""汇率写入。汇率插件靠它把抓到的牌价交给核心。"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import ConfigDict, field_validator
from sqlmodel import Field, Session, SQLModel

from ... import fx
from .. import Ctx, Outcome, register


class FxRateIn(SQLModel):
    model_config = ConfigDict(extra="forbid")

    rate: Decimal
    # 源标识由插件自己定（"boc" / "google" / …）。限字符集是因为它会进日志、
    # 前端色表键与 FxRate.source 列（VARCHAR(16)）。
    # SQLModel 的 Field 不透传 pattern，所以用校验器；顺带能给出人能读懂的报错。
    source: str = Field(max_length=16)
    date: Optional[dt.date] = None          # 留空 = JST 今天

    @field_validator("source")
    @classmethod
    def _src(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z0-9._-]{1,16}", v or ""):
            raise ValueError(f"汇率源标识 {v!r} 只能是小写字母/数字/.-_，长度 1..16")
        if v == fx.SOURCE_MANUAL:
            # `manual` 在核心里是**语义关键字**而不是普通标识：当天取值优先它、
            # 界面显示成「手填」、写侧还靠它挡自动源。插件用了它，就等于既能压过
            # 当天所有真实源、又冒充成用户亲手填的，而用户在界面上分辨不出来。
            raise ValueError(f"{fx.SOURCE_MANUAL!r} 是核心保留给用户手填的源名，插件请换一个")
        return v

    @field_validator("rate")
    @classmethod
    def _q(cls, v):
        # 与人手填汇率**同一把尺子**（区间 + 量化）。插件独立发版，不能指望它自律。
        from ....schemas import _q_fx
        return _q_fx(v)


@register(kind="fx.rate", label="汇率", scope="fx:write", schema=FxRateIn,
          # 同轮去重键 = **日期 + 源**，两半都不能少：
          #   少了 source → 一批里同一天的第 2、3 个源被判「本轮已提交过」且永不落库；
          #   date 用 `or "today"` → 不填日期和显式填今天算成两个键，同一天同源反而不去重。
          # 两个方向都错，且都表现为静默丢数据。对照 plugin_record 用的是天然复合键。
          key=lambda it: f"{it.date or dt.datetime.now(fx.JST).date()}|{it.source}",
          max_batch=32)
def apply_fx_rate(session: Session, item: FxRateIn, ctx: Ctx) -> Outcome:
    today = dt.datetime.now(fx.JST).date()
    d = item.date or today
    if d > today:
        # 补一个既有缺口：`latest_stored` 按 date desc 取，一条未来日期的行会**永远胜出**，
        # 此后所有新单都按它折算。进程内写入时这只是理论风险，开放给插件之后它是现实风险。
        return Outcome("rejected", code="validation", message="不接受未来日期的汇率")
    if (today - d).days > 7:
        return Outcome("rejected", code="validation", message="只接受近 7 天内的汇率")
    try:
        rate = fx._sane(item.rate)
    except (ValueError, InvalidOperation) as e:
        return Outcome("rejected", code="validation", message=str(e))

    # 问的必须是「**那一天**有没有手填」，不是「全库最新那条是不是手填」。
    # 用 latest_stored 的话，补录到过去某天时 `existing.date == d` 不成立，保护静默失效——
    # 读侧 pick_from 兜住了值所以不出错账，但写侧与读侧用了两把钥匙开同一把锁。
    manual = next((r for r in fx.rows_on(session, d) if r.source == fx.SOURCE_MANUAL), None)
    if manual is not None and item.source != fx.SOURCE_MANUAL:
        existing = manual
        # 人 > 机器，与 `can_advance_purchase` 同一条原则：用户当天亲手填过的汇率，
        # 插件不该悄悄覆盖掉。（用户改主意时自己去设置页改。）
        return Outcome("unchanged", id=existing.id,
                       message="当天已有手填汇率，不被自动源覆盖")

    row, created = fx.store(session, rate, item.source, on=d)   # 只 add/flush，不 commit
    return Outcome("created" if created else "updated", id=row.id)
