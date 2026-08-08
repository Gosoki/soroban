"""汇率（CNY→JPY）。多源按**可配置的优先级链**依次尝试，链与降级条件都在「设置」页上改。

默认链：**中国银行牌价 → 谷歌财经 → 通用汇率 API**。

**为什么以中行为首**：这是一本代购账本，成本最终要按能真正换到的汇率算。中行牌价是国内
可执行的官方口径，比中间价更贴近实际。谷歌次之（更新快、覆盖广，但是中间价）。

**为什么中行用公开牌价页而不是查询接口**：
中行的「历史牌价检索」接口（`srh.bankofchina.com/tsearch/v1/...`）已经挂上 GeeTest v4
行为验证码，服务端确实校验（实测传占位值被拒：`errMsg: 调用动态验证码组件接口发生异常`）。
更早那条「取图形验证码 → OCR → POST search_cn.jsp」的路子整个 404 下线了。
绕验证码既是规避人家的反爬控制，也极其脆弱——对方一升级就断。
而 `www.boc.cn/sourcedb/whpj/` 这个**今日牌价页**把当天数据直接放在 HTML 里，无需任何验证。

**降级规则**（每一档都可配，见 services/prefs.py）：
  1. 每个源一轮里最多试 `fx.attempts` 次（默认 3），之间有退避，不是连打。
  2. 想用链上第 i 个源，前提是**它上面所有源都已连续失败满 `fx.fallback_hours`**（默认 72h）。
     没满就沿用上一次取到的值——短暂抖动不该让整本账换口径（不同源的口径本来就有差）。
  3. 某个源从未成功过（全新部署、或那个源一直连不上），视为「一直在失败」，不挡后面的源。
     否则新装的用户会因为首选源不通而**一个汇率都拿不到**。
  4. 首选源恢复后，下一轮自然写回它，前端的「备用」标记随之消失。

  「连续失败多久」不另存计时器，直接由数据推导：`max(fetched_at) where source=<该源>`。
  多存一份计时器就多一处要同步的真相，重启还会丢——而这个时间本来就在库里。
"""

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session, col, select

from ..config import FX_MAX, FX_MIN, FX_QUANTUM, settings
from ..database import get_engine
from ..models import FxRate, utcnow

log = logging.getLogger("soroban.fx")
JST = dt.timezone(dt.timedelta(hours=9))

SOURCE_BOC = "boc"                  # 中国银行外汇牌价
SOURCE_GOOGLE = "google"            # 谷歌财经
SOURCE_ERAPI = "erapi"              # 通用汇率 API
# 各源的展示名。前端在「备用」标签的提示里显示当前用的是哪个源。
SOURCE_LABELS = {SOURCE_BOC: "中国银行", SOURCE_GOOGLE: "谷歌财经", SOURCE_ERAPI: "通用汇率 API"}

BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
GOOGLE_URL = "https://www.google.com/finance/quote/{base}-{quote}"
ER_API = "https://open.er-api.com/v6/latest/{base}"

_Q = FX_QUANTUM                 # 与手填校验共用同一量化精度（唯一真相见 config）
_MIN, _MAX = FX_MIN, FX_MAX     # 合理区间，越界视为脏数据不入库（与 schemas 校验同源）

_BACKOFF = (2, 5)               # 重试之间的间隔（秒）。失败还连打会更像攻击，也解决不了问题

# 中行牌价表的列序（页面自述「本汇率表单位为100外币换算人民币」）：
#   0 货币名称 / 1 现汇买入价 / 2 现钞买入价 / 3 现汇卖出价 / 4 现钞卖出价 / 5 中行折算价 / 6 发布时间
# 用哪一列由 settings.FX_BOC_COLUMN 决定，默认「中行折算价」——中行自己的折算基准，
# 最接近原方案（通用 API 中间价）的口径。五个价来自**同一次请求**，换列不增加任何访问。
# 注：日元的现汇与现钞目前是同一个价（美元等币种才有差别）。
BOC_COLUMNS = {
    "hmrj": (1, "现汇买入价"), "cmrj": (2, "现钞买入价"),
    "mcj": (3, "现汇卖出价"), "cmcj": (4, "现钞卖出价"),
    "zhzjj": (5, "中行折算价"),
}
_BOC_UNIT = Decimal(100)        # 牌价是「100 日元 = ? 人民币」，要倒过来才是 1 人民币 = ? 日元


def boc_column(key: Optional[str] = None) -> tuple[int, str]:
    """选用的牌价列 (下标, 中文名)。配错了退回折算价并告警，不让汇率整个抓不到。"""
    k = (key or "").strip().lower()
    if k not in BOC_COLUMNS:
        if k:
            log.warning("中行取价口径 %r 不认识，退回「中行折算价」。可选：%s", key, "/".join(BOC_COLUMNS))
        return BOC_COLUMNS["zhzjj"]
    return BOC_COLUMNS[k]


def _sane(rate: Decimal) -> Decimal:
    """量化 + 区间校验。越界当脏数据拒收——宁可沿用旧值，也不让一个离谱汇率污染整本账。"""
    rate = rate.quantize(_Q)
    if not (_MIN <= rate <= _MAX):
        raise ValueError(f"汇率 {rate} 超出合理区间 [{_MIN}, {_MAX}]")
    return rate


def parse_boc_jpy(html: str, column: Optional[str] = None) -> Decimal:
    """从中行今日牌价页 HTML 里取日元的「中行折算价」，换算成 1 CNY = ? JPY。

    纯函数，离线可测（tests/test_fx_source.py 用真实页面片段跑）。

    行的形状是 `<tr data-currency='日元'>` —— **不能**按 `<tr>` 精确匹配去找，
    那样会漏掉带属性的开标签（本项目就这么踩过一次，以为页面里没有数据）。
    """
    row = re.search(r"<tr[^>]*data-currency=['\"]日元['\"][^>]*>(.*?)</tr>", html, re.S)
    if not row:
        raise ValueError("中行牌价页里没有日元行（页面结构可能变了）")
    cells = [re.sub(r"<[^>]+>", "", c).replace("\xa0", " ").strip()
             for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
    idx, label = boc_column(column)
    if len(cells) <= idx:
        raise ValueError(f"日元行只有 {len(cells)} 列，取不到{label}")
    raw = cells[idx]
    try:
        per_100 = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}不是数字：{raw!r}")
    if per_100 <= 0:
        raise ValueError(f"{label}异常：{per_100}")
    # 牌价：100 JPY = per_100 CNY  →  1 CNY = 100 / per_100 JPY
    return _sane(_BOC_UNIT / per_100)


async def fetch_rate_boc(column: Optional[str] = None) -> Decimal:
    """中国银行今日牌价。失败抛异常。"""
    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; soroban/1.0)"},
    ) as client:
        r = await client.get(BOC_URL)
        r.raise_for_status()
        # 中行这个页面是 GBK 家族编码，httpx 猜错会把中文变成乱码、连「日元」都匹配不到
        r.encoding = r.encoding or "utf-8"
        html = r.text
        if "日元" not in html:
            html = r.content.decode("gb18030", errors="replace")
    return parse_boc_jpy(html, column)


_GOOGLE_POINT = re.compile(
    r"\[\[(\d{4}),(\d{1,2}),(\d{1,2}),(\d{1,2}),(\d{1,2}),null,null,\[\]\],\[([\d.eE\-]+),")


def parse_google(html: str) -> Decimal:
    """从谷歌财经页里取最新报价。纯函数，离线可测。

    新版页面（`/finance/beta/quote/...`）**不再把价放在 DOM 文本里**，而是塞进内嵌 JS 的
    时间序列数组，形如：

        [[2026,8,7,7,9,null,null,[]],[23.462783, -0.01, -0.0004, 4,4,5]]
           └── 年月日时分                 └── 1 base = ? quote

    所以按 class 选择器取值的老写法（`div.YMlKec.fxKbKc`）现在 0 命中——
    抓 `/finance/quote/...` 还会先 302 到 beta 页，不跟跳转连页面都拿不到。
    这里按**时间戳最大**的那一点取值，而不是取数组最后一个：数组顺序不是契约。
    """
    pts = [((int(a), int(b), int(c), int(d), int(e)), f)
           for a, b, c, d, e, f in _GOOGLE_POINT.findall(html)]
    if not pts:
        raise ValueError("谷歌财经页里没解析到报价（页面结构可能又变了）")
    _, raw = max(pts, key=lambda x: x[0])
    try:
        return _sane(Decimal(raw))
    except (InvalidOperation, ValueError):
        raise ValueError(f"谷歌财经报价不是数字：{raw!r}")


async def fetch_rate_google(base: Optional[str] = None, quote: Optional[str] = None) -> Decimal:
    """谷歌财经。直接查 CNY-JPY，拿到的就是 1 CNY = ? JPY，不必倒数。失败抛异常。"""
    base = base or settings.FX_BASE
    quote = quote or settings.FX_QUOTE
    async with httpx.AsyncClient(
        timeout=25, follow_redirects=True,          # 必须跟跳转：/quote 会 302 到 /beta/quote
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"},
    ) as client:
        r = await client.get(GOOGLE_URL.format(base=base, quote=quote))
        r.raise_for_status()
        return parse_google(r.text)


async def fetch_rate_fallback(base: Optional[str] = None, quote: Optional[str] = None) -> Decimal:
    """备用源：通用汇率 API（中间价）。1 base = <返回值> quote。失败抛异常。"""
    base = base or settings.FX_BASE
    quote = quote or settings.FX_QUOTE
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "soroban/1.0"}) as client:
        r = await client.get(ER_API.format(base=base))
        r.raise_for_status()
        data = r.json()
    rate = (data.get("rates") or {}).get(quote)
    if not rate:
        raise ValueError(f"备用源没有 {base}->{quote} 的汇率")
    return _sane(Decimal(str(rate)))


async def _fetch(source: str, conf: dict) -> Decimal:
    """按源名取一次汇率。新增源只要在这里和 prefs.FX_SOURCE_CHOICES 各加一行。"""
    if source == SOURCE_BOC:
        return await fetch_rate_boc(conf.get("fx.boc_column"))
    if source == SOURCE_GOOGLE:
        return await fetch_rate_google()
    if source == SOURCE_ERAPI:
        return await fetch_rate_fallback()
    raise ValueError(f"未知的汇率源：{source}")


async def try_source(source: str, conf: dict) -> Optional[Decimal]:
    """试 attempts 次；全失败返回 None（不抛，交给调用方决定要不要降级）。"""
    attempts = int(conf.get("fx.attempts", 3))
    for i in range(attempts):
        try:
            return await _fetch(source, conf)
        except Exception as e:                              # noqa: BLE001
            log.warning("%s 汇率抓取失败（第 %d/%d 次）：%s",
                        SOURCE_LABELS.get(source, source), i + 1, attempts, e)
            if i + 1 < attempts and i < len(_BACKOFF):
                await asyncio.sleep(_BACKOFF[i])
    return None


def latest_stored(session: Session) -> Optional[FxRate]:
    return session.exec(select(FxRate).order_by(col(FxRate.date).desc())).first()


def last_success(session: Session, source: str) -> Optional[dt.datetime]:
    """某个源最近一次成功的时间。降级判定直接读它，不另存计时器。"""
    row = session.exec(
        select(FxRate).where(FxRate.source == source)
        .order_by(col(FxRate.fetched_at).desc())
    ).first()
    if row is None:
        return None
    t = row.fetched_at
    # 库里是 naive UTC（见 models.utcnow 的口径），统一到 aware，免得跨类型相减炸掉
    return t.replace(tzinfo=dt.timezone.utc) if t.tzinfo is None else t


def _down_for(session: Session, source: str) -> Optional[dt.timedelta]:
    """该源已经连续失败多久。None = **从未成功过**，视为一直在失败（不挡后面的源）。
    否则新装的用户会因为首选源不通而一个汇率都拿不到。"""
    ok = last_success(session, source)
    return None if ok is None else utcnow() - ok


def may_use(session: Session, chain: list[str], idx: int, hours: int) -> bool:
    """能不能用链上第 idx 个源：它**上面所有源**都得已经连续失败满 hours。

    首选源（idx=0）永远可用。hours=0 时等于「一失败就立刻降级」。
    这条规则的意义：不同源的口径本来就有差（中行折算价 vs 中间价），
    为一次网络抖动就换口径，账本会在两个数之间来回横跳。
    """
    if idx == 0:
        return True
    threshold = dt.timedelta(hours=max(0, hours))
    for upper in chain[:idx]:
        down = _down_for(session, upper)
        if down is not None and down < threshold:
            return False
    return True


def _store(session: Session, rate: Decimal, source: str) -> FxRate:
    today = dt.datetime.now(JST).date()
    row = session.exec(select(FxRate).where(FxRate.date == today)).first()
    if row:
        row.rate, row.source, row.fetched_at = rate, source, utcnow()
    else:
        row = FxRate(date=today, rate=rate, source=source)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


async def refresh_and_store(session: Session) -> Optional[FxRate]:
    """按优先级链取汇率并落库。规则见模块文档串。取不到就返回最近一条历史。"""
    from . import prefs

    conf = prefs.load(session)
    chain = conf["fx.sources"]
    hours = int(conf["fx.fallback_hours"])

    for idx, source in enumerate(chain):
        if not may_use(session, chain, idx, hours):
            # 上游还没失败够久：沿用上一次取到的值，**不写库也不换源**
            log.warning("上游源尚未连续失败满 %dh，不启用 %s，沿用上一次取到的值",
                        hours, SOURCE_LABELS.get(source, source))
            return latest_stored(session)
        rate = await try_source(source, conf)
        if rate is None:
            continue
        row = _store(session, rate, source)
        note = "" if idx == 0 else f"（已降级，首选源 {SOURCE_LABELS.get(chain[0], chain[0])} 不可用）"
        log.info("汇率已更新[%s]%s：1 %s = %s %s",
                 SOURCE_LABELS.get(source, source), note, settings.FX_BASE, rate, settings.FX_QUOTE)
        return row

    log.error("全部 %d 个汇率源都失败，沿用最近一次记录", len(chain))
    return latest_stored(session)


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


def _refresh_round_blocking() -> int:
    """在**工作线程**里跑完一轮刷新，返回下一次的间隔秒数。

    自开自关 Session、并用 `asyncio.run` 起一个属于本线程的事件循环来跑 `refresh_and_store`。
    这样 refresh_and_store 只有一份实现（HTTP 路由那条路径原样复用），而这条后台路径上
    的**同步 DB I/O 一点都不落在主事件循环上**。多起一个循环的开销对 6 小时一次的任务
    可以忽略；httpx 的 AsyncClient 在该循环内自建自销，不跨循环复用。"""
    from . import prefs

    with Session(get_engine()) as session:
        asyncio.run(refresh_and_store(session))
        # 间隔每轮重读：在「设置」页改完不必重启就能生效
        return int(prefs.load(session)["fx.refresh_seconds"])


async def fx_loop() -> None:
    """后台定时刷新（间隔 settings.FX_REFRESH，默认 6h）。任何异常都不结束循环。

    只读屏障期间跳过：本循环**直接用 Session 写** FxRate，绕过 HTTP 中间件，
    不自己检查就会在数据库迁移的拷贝过程中往源库插行（见 app/maintenance.py）。
    跳过没有代价——汇率下一轮再抓即可，抓不到还有历史兜底。

    DB 活全部 offload 到线程池：协程体里直接跑同步 Session/pymysql，会在「MySQL 掉线但
    TCP 还通」时把整个事件循环冻住（scheduler_loop 实测单次卡 384 秒——pymysql 的
    read_timeout 默认是 None，connect_timeout 只管建连）。那次是同一类问题、同一个修法，
    fx_loop 当时漏了。"""
    from ..maintenance import barrier

    interval = settings.FX_REFRESH          # 读不到设置时的兜底（如库还没建好）
    while True:
        try:
            reason = barrier.blocked_reason()      # 纯内存 + 一把锁，不碰库，留在事件循环上无妨
            if reason:
                log.info("汇率刷新跳过：%s", reason)
            else:
                interval = await run_in_threadpool(_refresh_round_blocking)
        except Exception as e:
            log.warning("汇率刷新循环异常: %s", e)
        await asyncio.sleep(interval)
