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
        return v

    @field_validator("rate")
    @classmethod
    def _q(cls, v):
        # 与人手填汇率**同一把尺子**（区间 + 量化）。插件独立发版，不能指望它自律。
        from ....schemas import _q_fx
        return _q_fx(v)


@register(kind="fx.rate", label="汇率", scope="fx:write", schema=FxRateIn,
          key=lambda it: str(it.date or "today"), max_batch=32)
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

    existing = fx.latest_stored(session)
    if existing is not None and existing.date == d and existing.source == fx.SOURCE_MANUAL \
            and item.source != fx.SOURCE_MANUAL:
        # 人 > 机器，与 `can_advance_purchase` 同一条原则：用户当天亲手填过的汇率，
        # 插件不该悄悄覆盖掉。（用户改主意时自己去设置页改。）
        return Outcome("unchanged", id=existing.id,
                       message="当天已有手填汇率，不被自动源覆盖")

    row, created = fx.store(session, rate, item.source, on=d)   # 只 add/flush，不 commit
    return Outcome("created" if created else "updated", id=row.id)
