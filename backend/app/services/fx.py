"""汇率（CNY→JPY）的**存储、兜底与新鲜度**。核心自己不抓汇率。

分工：
  · **取数在插件** —— `plugins/soroban-plugin-fx` 抓中行/谷歌/通用 API，经通用写入通道
    `POST /api/plugins/ingest`（kind=fx.rate）交回来，落在 services/ingest/kinds/fx_rate.py。
    源顺序、重试次数、取价口径都是那个插件的私有参数，在插件卡片上调。
    **理由是变更频率**：抓网页的代码天然跟着别人改版走（中行的 `<tr data-currency>`、
    谷歌嵌在 JS 里的报价点，各错过一次），不该和账本核心同一个发布节奏。
  · **本模块只做四件事**，都是账本自洽必需的：
      1. 存 —— `store()` 追加一行（一天可以有多条）。**只 add/flush，提交由调用方决定**
         ——它常被夹在别的写事务中间，自己 commit 会让那笔事务无法回滚。
         `store` 只 add/flush **不 commit**：
         提交权归调用方（通用写入通道给每一项包了 savepoint，被击穿会让回执与事实相反）。
      2. 盖 —— `rate_for_date()`，各条入账路径统一走 `_rate_with_freshness_warning()`。
      3. 兜底 —— `ensure_manual_rate()`：没装插件时库里一条汇率都没有，
         用设置里的「手填汇率」记一条（source="manual"），账本至少能自洽运转。
      4. 新鲜度 —— `rate_age_hours()/is_expired()`，超过 `fx.stale_hours` 明确标出并告警。

**过期照样供给**，不返回 None：一个两天前的真实汇率比「没有」更接近事实，且订单会把
当时汇率逐行存下来、可审计可改。代价是它安静，所以过期必须在日志与界面上看得见。

`FxRate.source` 是**插件自报的标识**，核心不解释其含义（只在 ingest 处校验字符集）。
"""
import datetime as dt
import logging
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from sqlmodel import Session, col, select

from ..config import FX_MAX, FX_MIN, FX_QUANTUM, settings
from ..models import FxRate, utcnow

log = logging.getLogger("soroban.fx")
JST = dt.timezone(dt.timedelta(hours=9))

SOURCE_MANUAL = "manual"            # 用户在设置页手填的（不是抓来的）
# 核心**只认识自己会写的那一个源**。其余标识（"boc"/"google"/…）由汇率插件自定，
# 经 POST /api/plugins/ingest 原样存进 FxRate.source；认不出就原样透传裸 key。
# 它们的中文展示名在前端（frontend/src/constants）——那是展示层的事；
# 核心维护一份它已经不会产出、也无法穷举的源名表，只会烂掉。
SOURCE_LABELS = {SOURCE_MANUAL: "手填"}

_Q = FX_QUANTUM                 # 与手填校验共用同一量化精度（唯一真相见 config）
_MIN, _MAX = FX_MIN, FX_MAX     # 合理区间，越界视为脏数据不入库（与 schemas 校验同源）


def _sane(rate: Decimal) -> Decimal:
    """区间校验 + 量化。插件独立发版，交回来的值核心必须自己再验一遍。

    **舍入方式必须显式写出来。** 这里曾是全仓唯一一个不带 `rounding=` 的 `quantize()`，
    于是它用 decimal 的默认值 `ROUND_HALF_EVEN`，而 `schemas._q_fx`（手填那条路）
    对**同一个量**用的是 `ROUND_HALF_UP`。同一个汇率值走两条路会存成两个数：
    `20.00005` → 手填得 20.0001、插件回灌得 20.0000。
    差在第 4 位小数上，金额层面几乎看不出来——正因为看不出来才更该钉住：
    「同一个输入两条路两个结果」是这类账本最难查的一类不一致。
    """
    r = Decimal(rate)
    if not r.is_finite() or not (_MIN <= r <= _MAX):
        raise ValueError(f"汇率 {r} 不在合理区间 [{_MIN}, {_MAX}]（1元≈20円）")
    return r.quantize(_Q, rounding=ROUND_HALF_UP)


def latest_stored(session: Session) -> Optional[FxRate]:
    """全库最新的一条。一天可能有多条，所以要按 (日期, 抓取时刻) 两级排。

    只按 date 排的话，同一天的多条里返回哪一条取决于数据库的实现，
    表现是「刷新一下汇率就变了」——最难查的那种不确定性。

    `.limit(1)` 是**必需的**，不是优化：`.first()` 只是「取结果集的第一行」，
    它不会往 SQL 里加 LIMIT——整张 fxrate 会先被排序、传输、构造成 ORM 对象，
    然后丢掉除第一条以外的全部。而 fxrate 是**只增不删**的（一天多条，见 `pick_from`），
    且侧栏、看板、每一次建单都要走这里，代价随使用时长线性增长。
    """
    return session.exec(
        select(FxRate)
        .order_by(col(FxRate.date).desc(), col(FxRate.fetched_at).desc())
        .limit(1)
    ).first()


def rows_on(session: Session, d: dt.date) -> list[FxRate]:
    """某一天的全部汇率，新的在前。汇率页与 `pick_on` 共用。"""
    return list(session.exec(
        select(FxRate).where(FxRate.date == d).order_by(col(FxRate.fetched_at).desc())
    ).all())


def _recency_key(r: FxRate):
    """「多新」的排序键：抓取时刻优先，同一时刻用 id 打破平局。

    naive/aware 统一按 UTC 解读——SQLite 取回的时间戳可能是 naive，
    直接和 aware 比会 TypeError。
    """
    t = r.fetched_at
    if t is not None and t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (t or dt.datetime.min.replace(tzinfo=dt.timezone.utc), r.id or 0)


def pick_from(rows: list[FxRate]) -> Optional[FxRate]:
    """从**已经取出来的**某一天的多条里挑该用的那条：手填优先，其次该日最后一条。

    **自己排序，不依赖调用方**。原先的契约是「要求 rows 已按 fetched_at 倒序」，
    而契约的两端在不同文件（`rows_on` / `routers/fx.py` 的按天汇总）——这种隐式约定
    必然漂移，且漂移时**没有任何测试会红**：`pick_from` 的单元测试自己传有序的 rows，
    照样全绿，而建单会安静地用上当天**第一条**汇率而不是最后一条。
    一天几条数据，排一次的成本可以忽略；换来的是不变量由函数自己保证。

    顺带消掉一个不确定性：同一秒抓到两条时，原先返回哪条取决于数据库的实现
    （SQL 的 ORDER BY 对相等键不保证稳定）。现在用 id 打破平局，结果是确定的。

    单独抽出来是为了给汇率页按天汇总用：那里已经把整段时间的行一次查了出来，
    再对每天调一次 `pick_on` 就是每天多查一次库（730 天上限 = 730 次）。
    但也不能在那边照着规则再写一遍——页面显示的和建单实际用的会变成两个数，
    那种不一致要等到对账才发现。所以：规则只有这一份，两个入口都调它。
    """
    if not rows:
        return None
    ordered = sorted(rows, key=_recency_key, reverse=True)
    manual = [r for r in ordered if r.source == SOURCE_MANUAL]
    return (manual or ordered)[0]


def pick_on(session: Session, d: dt.date) -> Optional[FxRate]:
    """某一天该用哪一条：**手填优先，其次该日最后一条**。

    「手填优先」与 `can_advance_purchase` 是同一条原则：人明确填过的值，
    机器不该悄悄盖掉。一天多条之后这条规则必须落在**读**这一侧——
    写侧只管追加，不做取舍（追加式存储才能回看「那天几点是多少」）。
    """
    return pick_from(rows_on(session, d))




def store(session: Session, rate: Decimal, source: str,
          on: Optional[dt.date] = None) -> tuple[FxRate, bool]:
    """记一条汇率。**一天可以有多条**——每次抓取追加一条，不覆盖。返回 (行, 是否新建)。

    追加而不是覆盖，是为了两件事：① 补录几天前的订单要按**那一天**折算，而那天可能抓过
    好几次；② 汇率页要能回看「那天几点是多少」。取哪一条是**读**侧的事（见 `pick_on`）。

    ⚠️ **只 add/flush，不 commit**。提交由调用方决定。
    原先它自己 commit()，那样任何把它当子步骤的调用方都无法回滚——通用写入通道给每一项
    包了 `begin_nested()` savepoint，被调用方一 commit 就击穿：校验失败照样回
    「rejected」给插件，而那一行其实已经落库了。这是最难查的一类错误（回执与事实相反）。
    """
    d = on or dt.datetime.now(JST).date()
    row = FxRate(date=d, rate=rate, source=source)
    session.add(row)
    session.flush()
    return row, True



def rate_age_hours(row: Optional[FxRate]) -> Optional[float]:
    """这条汇率距今多少小时。没有行则 None。"""
    if row is None or row.fetched_at is None:
        return None
    t = row.fetched_at
    if t.tzinfo is None:                      # SQLite 取回可能是 naive，统一按 UTC
        t = t.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (utcnow() - t).total_seconds() / 3600)


def is_expired(session: Session, row: Optional[FxRate]) -> bool:
    """超过 `fx.stale_hours` 没更新 → 视为过期。

    与 FxRead.not_today（「不是今天的」）**不是一回事**：那个是日粒度、1 天前和 3 个月前
    长得一模一样；这个才是「还能不能信」的判断，可配置、并且会进日志。
    """
    from . import prefs

    age = rate_age_hours(row)
    if age is None:
        return False
    return age > int(prefs.load(session)["fx.stale_hours"])


def current_row(session: Session) -> Optional[FxRate]:
    """「当前汇率」那一条 = **现在建一张今天的单会用的那一条**。

    不能用 `latest_stored()`：它取的是全库最新（date desc, fetched_at desc），
    而建单走的是 `pick_on()`（当天**手填优先**，其次当天最后一条）。
    两者在一个很常见的场景下会分叉：用户今天手填了一条，之后汇率插件又抓了一条
    ——`latest_stored` 给插件那条，建单用手填那条。于是侧栏、看板、设置页显示的数字
    和账本里真正用的不是同一个，而用户手填的**本意**恰恰是「用我这个值」。
    显示层无视手填，是这套「手填优先」规则里唯一说话不算数的地方。

    当天一条都没有（凌晨还没抓、或很久没跑）→ 回退全库最新，与 `rate_for_date`
    在「那天没记录」时的兜底口径一致。

    **这里是唯一实现**：`GET /api/fx` 与看板都取它。抄第二份的代价已经付过一次——
    顶栏和账本显示两个数，而那种不一致要等到对账才发现。
    """
    return pick_on(session, dt.datetime.now(JST).date()) or latest_stored(session)


def current_rate(session: Session) -> Optional[Decimal]:
    """当前汇率的数值。库里空 → None。口径见 `current_row`。

    ⚠️ **过期也照样返回**，不返回 None。理由：拒绝供给会让建单直接失去日元金额，
    而一个两天前的真实汇率比「没有」更接近事实；订单会把当时用的汇率**逐行存下来**，
    事后可审计、可改。代价是它安静——所以取值时会在过期时记一条警告，
    界面上也会标出已过期多久。
    """
    row = current_row(session)
    return row.rate if row else None


def record_manual_rate(session: Session) -> Optional[FxRate]:
    """把设置里的「手填汇率」记成今天的一条。用户每次保存都追加一条。

    追加而不是覆盖：一天多条是这套存储的常态，而手填在 `pick_on` 里优先于机器抓的——
    所以当天此后的自动抓取不会把它盖掉，同时「今天几点改成了多少」也留得住。
    """
    from . import prefs

    raw = (prefs.load(session).get("fx.manual_rate") or "").strip()
    if not raw:
        return None
    try:
        rate = _sane(Decimal(raw))
    except (InvalidOperation, ValueError) as e:
        log.warning("手填汇率 %r 不可用（%s），忽略", raw, e)
        return None
    row, _ = store(session, rate, SOURCE_MANUAL)
    session.commit()
    log.info("已记下手填汇率：1 %s = %s %s", settings.FX_BASE, rate, settings.FX_QUOTE)
    return row


def ensure_manual_rate(session: Session) -> Optional[FxRate]:
    """库里一条汇率都没有时，按设置里的「手填汇率」记一条。

    这是系统**自带的唯一汇率来源**：自动获取归汇率插件管，没装插件时库是空的，
    新建订单就没有日元金额。手填值让账本至少能自洽运转。

    只在**完全没有**记录时才写，不覆盖任何已有行——它是起点，不是每天的兜底。
    写成一行真正的 FxRate（而不是读取时兜底）：这样 `rate_for_date` / `current_rate`
    一行都不用改，审计链路也统一（来源列写 "manual"，界面上标「手填」）。
    """
    from . import prefs

    if latest_stored(session) is not None:
        return None
    raw = (prefs.load(session).get("fx.manual_rate") or "").strip()
    if not raw:
        return None
    try:
        rate = _sane(Decimal(raw))
    except (InvalidOperation, ValueError) as e:
        log.warning("手填汇率 %r 不可用（%s），忽略", raw, e)
        return None
    # **不 commit**：这个函数是从 `rate_for_date` 里被调用的，而 `rate_for_date` 又被
    # `stamp_fx` 在**订单写事务的中间**调用。中途 commit 一次，后面任何一步校验失败
    # （compute_money 的金额上限之类 → 422）都已经**无法回滚**：用户收到 422，
    # 而这张单前半截的改动已经落库，订单价与物品从此不自洽。
    # 这正是 `store()` 文档串里写过的那个陷阱（击穿外层 savepoint），
    # 只是当时只堵了通用写入通道那条路，这条路径上又重现了一次。
    # 跟着外层事务走也更合理：外层回滚 = 那张单没建成 = 这条兜底汇率行也不必留。
    row, _ = store(session, rate, SOURCE_MANUAL)
    log.info("库里还没有汇率，已按设置里的手填值记一条：1 %s = %s %s",
             settings.FX_BASE, rate, settings.FX_QUOTE)
    return row


_NO_RATE_QUIET_FOR = 60.0        # 秒
_no_rate_warned_at = 0.0
_no_rate_suppressed = 0


def _warn_no_rate(what: str) -> None:
    """「库里没有任何汇率」**每分钟只喊一次**，后面的合并成一句计数。

    这是个**全局状态**，不是每一行的属性：库里没有汇率，就是所有行都没有。
    原先逐行喊，一次爬虫回灌 2000 单就是 2000 行一模一样的 WARNING——
    实测灌 120 单刷了 120 行。那不是「更详细」，是**把同一批里真正该看的东西冲掉了**
    （比如同一次请求里被拒的那三条记录，夹在两千行重复告警中间）。

    不做成「整批只喊一次」是因为这里看不见批的边界（六条调用路径，有的根本没有批）。
    时间窗对两种形态都成立：批量回灌落在一个窗口里，手工记账隔几分钟一单也照样提醒。
    """
    global _no_rate_warned_at, _no_rate_suppressed

    now = time.monotonic()
    if now - _no_rate_warned_at < _NO_RATE_QUIET_FOR:
        _no_rate_suppressed += 1
        return
    tail = (f"（同样原因的还有 {_no_rate_suppressed} 行没有单独记）"
            if _no_rate_suppressed else "")
    _no_rate_warned_at, _no_rate_suppressed = now, 0
    log.warning("%s：库里还没有任何汇率，本行日元金额留空"
                "（去设置页填「手填汇率」，或装汇率插件）%s", what, tail)


def reset_warning_throttle() -> None:
    """把节流状态清零。**测试专用**——它是模块级的，会跨用例泄漏。

    与 `runlog.reset()` 同一个道理：一条用例灌了几十单把窗口占住，
    下一条用例断言「应该有这条告警」就会莫名其妙地红，而且红的是**无关的**那条。
    """
    global _no_rate_warned_at, _no_rate_suppressed
    _no_rate_warned_at, _no_rate_suppressed = 0.0, 0


def _rate_with_freshness_warning(session: Session, row: Optional[FxRate], what: str,
                                 *, exact: bool = False) -> Optional[Decimal]:
    """选中一条汇率并按需告警。**所有给账本行盖汇率的路径都从这里出去。**

    原先只有建商品订单/建集运订单两处走告警版本，另外四条（杂项建单、三张表 PATCH 补价、
    暂存自身补价、暂存导入建订单）直接调 `rate_for_date`——既不触发手填兜底、也不告警。
    也就是说「没装插件也自洽」在**插件的主入账路径上根本不成立**。
    把兜底与告警下沉到这里，六条路径自动都有，而不是指望每个调用点记得挑对函数。
    """
    if row is None:
        if what:
            _warn_no_rate(what)
        return None
    if what and is_expired(session, row):
        if exact:
            # 取到的**就是那一天**的汇率：补录几天前的订单时它按定义就是旧的，
            # 而它恰恰是唯一正确的历史汇率。原先这里也喊「已过期、金额可能不准」，
            # 于是暂存导入（补录的必经路径）上这条警告恒真——一条永远在喊狼来了的
            # 假警报，真出事时反而没人信。所以这一支记 info，只留痕不报警。
            log.info("%s：用的是 %s 那天的汇率（%.0f 小时前抓的，%s）",
                     what, row.date, rate_age_hours(row) or 0,
                     SOURCE_LABELS.get(row.source, row.source))
        else:
            # 这一支才是真问题：**那天没有记录，退回了更旧的一条**（或压根没按日期查）。
            # 意味着取汇率的链路可能断了，而金额确实按一个不属于这笔账的汇率算了。
            log.warning("%s：用的汇率已过期 %.1f 小时（%s，%s），日元金额可能不准",
                        what, rate_age_hours(row) or 0, row.date,
                        SOURCE_LABELS.get(row.source, row.source))
    return row.rate


def rate_for_date(session: Session, d: Optional[dt.date],
                  what: str = "") -> Optional[Decimal]:
    """按日期取汇率：优先该日已记录的 FxRate，没有就退回最近一次记录的
    （**只读库、不重新调 API**）。补录上月支出该按当天牌价折算，故按日期查在前。

    传了 `what`（这行汇率是给谁盖的）就走告警版本：库里一条都没有时按设置里的手填值兜一条、
    用了过期汇率时记一条警告。不传 = 纯查询，安静。
    """
    row = pick_on(session, d) if d is not None else None
    exact = row is not None                 # 拿到的就是**那一天**的汇率，不是退回来的
    if row is None:
        # 那天没有记录（比如爬虫补录的是很久以前的单）→ 退回全库最新的一条。
        row = latest_stored(session) or (ensure_manual_rate(session) if what else None)
    return _rate_with_freshness_warning(session, row, what, exact=exact)


