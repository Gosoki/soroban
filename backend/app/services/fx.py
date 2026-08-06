"""Exchange rate (CNY→JPY). Ported from hiyori/backend/fx.py — self-contained,
never calls hiyori's endpoint. Persists to FxRate table; falls back to the most
recent stored rate when the API is unreachable (P7)."""

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Optional

import httpx
from sqlmodel import Session, col, select

from ..config import FX_MAX, FX_MIN, FX_QUANTUM, settings
from ..database import get_engine
from ..models import FxRate, utcnow

log = logging.getLogger("soroban.fx")
JST = dt.timezone(dt.timedelta(hours=9))
ER_API = "https://open.er-api.com/v6/latest/{base}"
_Q = FX_QUANTUM                 # 与手填校验共用同一量化精度（唯一真相见 config）
_MIN, _MAX = FX_MIN, FX_MAX     # 合理区间，越界视为脏数据不入库（与 schemas 校验同源）


async def fetch_rate(base: Optional[str] = None, quote: Optional[str] = None) -> Decimal:
    """Live fetch. 1 base = <return> quote. Raises on network/parse/range failure."""
    base = base or settings.FX_BASE
    quote = quote or settings.FX_QUOTE
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "soroban/1.0"}) as client:
        r = await client.get(ER_API.format(base=base))
        r.raise_for_status()
        data = r.json()
    rate = (data.get("rates") or {}).get(quote)
    if not rate:
        raise ValueError(f"no rate for {base}->{quote}")
    rate = Decimal(str(rate)).quantize(_Q)
    if not (_MIN <= rate <= _MAX):
        raise ValueError(f"fx rate {rate} out of sane range [{_MIN}, {_MAX}]")
    return rate


def latest_stored(session: Session) -> Optional[FxRate]:
    return session.exec(select(FxRate).order_by(col(FxRate.date).desc())).first()


async def refresh_and_store(session: Session) -> Optional[FxRate]:
    """Fetch today's rate and upsert it. On failure, return last known (P7)."""
    try:
        rate = await fetch_rate()
    except Exception as e:
        log.warning("汇率抓取失败，沿用最近一次: %s", e)
        return latest_stored(session)
    today = dt.datetime.now(JST).date()
    row = session.exec(select(FxRate).where(FxRate.date == today)).first()
    if row:
        row.rate = rate
        row.fetched_at = utcnow()
    else:
        row = FxRate(date=today, rate=rate)
    session.add(row)
    session.commit()
    session.refresh(row)
    log.info("汇率已更新: 1 %s = %s %s", settings.FX_BASE, rate, settings.FX_QUOTE)
    return row


def current_rate(session: Session) -> Optional[Decimal]:
    row = latest_stored(session)
    return row.rate if row else None


def rate_for_date(session: Session, d: Optional[dt.date]) -> Optional[Decimal]:
    """按下单日期取汇率：优先用该日已记录的 FxRate；库里没有就退回最近一次记录的汇率
    （即入库当天的当前汇率）——**只读库、不重新调 API**。"""
    if d is not None:
        row = session.exec(select(FxRate).where(FxRate.date == d)).first()
        if row:
            return row.rate
    return current_rate(session)


async def fx_loop() -> None:
    """Background refresh; keeps last-good on any error (hiyori pattern)。

    只读屏障期间跳过：本循环**直接用 Session 写** FxRate，绕过 HTTP 中间件，
    不自己检查就会在数据库迁移的拷贝过程中往源库插行（见 app/maintenance.py）。
    跳过没有代价——汇率下一轮再抓即可，抓不到还有历史兜底。"""
    from ..maintenance import barrier

    while True:
        try:
            reason = barrier.blocked_reason()
            if reason:
                log.info("汇率刷新跳过：%s", reason)
            else:
                with Session(get_engine()) as session:
                    await refresh_and_store(session)
        except Exception as e:
            log.warning("汇率刷新循环异常: %s", e)
        await asyncio.sleep(settings.FX_REFRESH)
