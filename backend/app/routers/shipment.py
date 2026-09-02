"""集运订单 CRUD。金额 = 运费(CNY→JPY) + 特殊费_日元。"""

import datetime as dt
from typing import Optional

from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, or_
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import ci_contains
from ..models.base import is_unconverted, not_deleted
from ..models import ShipmentOrder, Order, utcnow
from ..schemas import (
    ShipmentCreate, ShipmentOcrAttachResult, ShipmentRead, ShipmentUpdate, OrderItemRead, OrderBrief,
)
from .common import (
    MAX_OFFSET,
    guarded_bump, list_totals, mirror_to_staging, raise_conflict, raise_not_found, run_ocr,
    soft_delete, stamp_fx
)

router = APIRouter(
    prefix="/api/shipment", tags=["shipment"], dependencies=[Depends(get_current_user)]
)


def _counts_toward_total(order: Order) -> bool:
    """这一单算不算进货款合计。**逐条按列判**，与看板 `_valid_conds` 同构。

    原先这里是 `_excluded_statuses()`：把 `ledger_exclusions()` **拍平**成一个值集合，
    再拿它去比 `order.purchase_status`——列名（那个参数当时就叫 `_col`，
    名字本身已经承认它没被用上）被丢掉了。

    今天两张表各自只声明**一条**轴，所以拍平恰好等价、不出错。
    但 `LedgerBase.ledger_exclusions` 的 docstring 明写着这是个列表**正是为了**
    「将来加卖出/退货这类并行的状态轴时，往列表里追一项」——
    追那一项的当天，这里就会拿新轴的值（比如「已退款」）去比 `purchase_status`，
    **永远不命中**，于是集运页把一张退了款的单算进货款合计，
    而看板（按列判）不算。同一笔钱两个页面两个说法，且两边都不报错。

    看板那边为「别用鸭子类型取列」写过一整段（见 `dashboard._valid_conds`），
    这里是同一件事的另一半：**别把列丢掉**。

    `col.key` 取的是 ORM 属性名，与 `getattr` 对得上；写错列名会当场 AttributeError，
    而不是静默恒不命中。
    """
    return all(getattr(order, col.key) not in vals
               for col, vals in Order.ledger_exclusions())


def _brief(order: Order) -> OrderBrief:
    return OrderBrief(
        id=order.id, order_no=order.order_no, date=order.date, title=order.title,
        purchase_status=order.purchase_status, jpy_settled=order.jpy_settled,
        items=[OrderItemRead(id=it.id, name=it.name, quantity=it.quantity) for it in order.items],
        # 让**后端**说清这一单算不算进合计。不给的话，前端要么自己抄一份状态清单
        # （两份迟早对不上），要么就像对账单那样把所有行都列出来、下面却写一个
        # 剔除过的合计——明细加起来 21000、合计写 10000，收到的人只能认为这单子是错的。
        counted=_counts_toward_total(order),
    )


# `ShipmentRead` 里**不是本表列**的字段。下面那个 getattr 推导式会挨个去模型上取，
# 漏登记一个就是运行时 AttributeError（而且只在这条路径上炸）。
# `test_shipment_read_derived_fields_are_registered` 钉住这张表，把它变成测试期失败。
_DERIVED_FIELDS = {"orders", "orders_jpy", "landed_jpy", "unconverted"}


def _landed(s: ShipmentOrder, children: list[Order]) -> dict:
    """这张集运单的到岸成本。子订单已经在内存里，不再查库。

    **排除规则从模型上取**（`Order.ledger_exclusions()`），与看板同一套：退款/关闭/
    待付款的单没花钱，不该算进这张集运单的成本。另抄一份状态清单是这类合计出错的常见方式。

    `unconverted` 数的是「有货款、却没折算成日元」的行：`SUM` 对 NULL 视而不见，
    不数出来的话，缺汇率的单会让合计**静默变小**而笔数照旧——看板那边为同一件事
    专门有个 `_uncounted`，这里不能重蹈。本单自己缺汇率也算一条。
    """
    counted = [c for c in children if _counts_toward_total(c)]
    orders_jpy = sum(c.jpy_settled or 0 for c in counted)
    # 判据走 `models.base.is_unconverted`——与看板、列表页脚同一个函数。
    # 这里原先自己写了一份，漏了 `!= 0`：一张全是赠品（¥0）的子订单会被报成「未折算」，
    # 而同一页的页脚与看板都说 0 条。同一件事三个出口，说法必须一样。
    missing = sum(1 for c in counted if is_unconverted(c.price_cny, c.jpy_settled))
    if is_unconverted(s.price_cny, s.jpy_settled):
        missing += 1
    return {"orders_jpy": orders_jpy,
            "landed_jpy": orders_jpy + (s.jpy_settled or 0),
            "unconverted": missing}


def _shipment_read(s: ShipmentOrder, children: list[Order],
                   *, landed: bool = True) -> ShipmentRead:
    """只用本表的列构造响应，再挂上调用方过滤好的子订单。

    刻意不用 ShipmentRead.model_validate(s)：那样会为了填 orders 字段去懒加载 s.orders
    关系——每行一条 SQL、且**连已软删的子订单一起拉**，随后又被这份过滤过的列表整个覆盖，
    纯属白跑（正是本函数批量化想省掉的那部分）。

    `landed=False`（`brief=True` 那条路）时三个到岸字段留 **None** 而不是 0：
    那条路根本没查子订单，报 0 等于说「这单没花钱」，而实际可能挂着十几万日元。"""
    scalars = {k: getattr(s, k) for k in ShipmentRead.model_fields
               if k not in _DERIVED_FIELDS}
    extra = _landed(s, children) if landed else {}
    return ShipmentRead(**scalars, orders=[_brief(c) for c in children], **extra)


def _may_see_orders(current) -> bool:
    """这个调用方能不能看到集运单里挂的**账本订单**明细。

    人类登录恒为真（他本来就有全部权限）。插件令牌则要看它有没有 `orders:read`——
    没有的话这里只回一个空列表。

    **不是在收紧权限模型**（用户明确说过插件都是自己写的、不用防恶意插件），
    而是让那个勾选框说的话成立：用户取消勾选「读商品订单」，界面上就是这么写的，
    而 `GET /api/shipment` 会把每张集运单挂着的订单号、标题、状态、结算日元、
    以及每件物品的名称/数量/单价原样发出去——**他留下的那道闸，从另一个门整个绕开了**。
    """
    claims = getattr(current, "_plugin_claims", None)
    if not claims:
        return True                      # 人类登录
    return "orders:read" in set(claims.get("scp") or [])


def _read_many(session: Session, shipments: list[ShipmentOrder],
               brief: bool = False) -> list[ShipmentRead]:
    """批量构造响应，其中 orders 只含未软删的关联商品订单（不泄露已删数据）。

    一次 IN 查询取回**整页**的子订单（再用 selectinload 一次取回它们的物品），而不是每行
    各查一次——列表页一屏 50 行时，这是 1+1 条 SQL 与 50+50 条的差别。

    `brief=True` 时**完全跳过**子订单与物品：订单页/物品页拉这个接口只是为了填一个
    「选哪张集运单」的下拉，全仓没有一处读 `j.orders`。实测 200 张集运单展开后
    响应 1.1MB / 1073ms，而消费方只用 4 个标量字段——纯浪费。
    查询条数测试（tests/test_queries.py）钉的是条数、钉不住体积，1.1MB 它照样绿。"""
    if brief:
        return [_shipment_read(s, [], landed=False) for s in shipments]
    ids = [s.id for s in shipments]
    by_parent: dict[int, list[Order]] = {}
    if ids:
        children = session.exec(
            select(Order)
            .where(Order.shipment_order_id.in_(ids), not_deleted(Order))
            .options(selectinload(Order.items))
            .order_by(Order.date.desc(), Order.id.desc())
        ).all()
        for c in children:
            by_parent.setdefault(c.shipment_order_id, []).append(c)
    return [_shipment_read(s, by_parent.get(s.id, [])) for s in shipments]


def _read(session: Session, shipment: ShipmentOrder, *, current=None) -> ShipmentRead:
    """单条响应。**给了 `current` 就按调用方的授权收窄**，与列表同一条闸。

    `current` 默认 `None` = 不收窄，保留给那些中间件本来就不放插件进来的端点
    （它们没声明 `x-scope`，插件令牌拿到的是 403，压根到不了这里）。
    实测过五个返回 `ShipmentRead` 的端点：列表自己过闸，
    GET 单条 / POST 挂订单 / DELETE 摘订单都被中间件挡在 403，
    **只有 PATCH 声明了 `x-scope` 又没过闸**——所以那一处必须显式传 `current`。
    """
    brief = current is not None and not _may_see_orders(current)
    return _read_many(session, [shipment], brief=brief)[0]


@router.get("", openapi_extra={"x-scope": "shipment:read"})
def list_orders(
    session: Session = Depends(get_session),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    shipment_status: Optional[str] = None,
    q: Optional[str] = Query(None, description="模糊搜：集运单号 / 国际运单号 / 收货人"),
    recipient: Optional[str] = Query(None, description="按收货人精确筛选"),
    brief: bool = Query(False, description="只要集运单本身，不展开子订单与物品（供下拉选项用）"),
    legacy_status: Optional[str] = Query(
        None, alias="status", include_in_schema=False,
        description="已废弃：查询参数 status 已按业务段改名"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    current=Depends(get_current_user),
):
    # FastAPI 对未知 query 参数**默认忽略**：改名后漏改一处调用方，
    # 表现就是「筛选点了没反应、返回全量、HTTP 200、零日志」。响亮拒掉比静默返回错数据强。
    if legacy_status:
        raise HTTPException(status_code=400,
                            detail="查询参数 status 已改名为 shipment_status")
    conds = [not_deleted(ShipmentOrder)]
    if date_from:
        conds.append(ShipmentOrder.date >= date_from)
    if date_to:
        conds.append(ShipmentOrder.date <= date_to)
    if shipment_status:
        conds.append(ShipmentOrder.shipment_status == shipment_status)
    if recipient:
        conds.append(ShipmentOrder.recipient == recipient)
    if q:
        # 三个都搜：**手上只有一张国际运单号或者一个收件人名字**时，
        # 只搜集运单号等于搜不到——而那正是回头找一张单最常见的起点。
        # 三列都走 ci_contains：收货人也是 BinStr 列（键列一律二进制排序规则，与 SQLite 对齐），
        # 裸 contains 在 MySQL 上会变成大小写敏感——`test_binstr_columns_use_ci_contains_for_search`
        # 当场把这个写法拦了下来。
        conds.append(
            ci_contains(ShipmentOrder.shipment_no, q, session)
            | ci_contains(ShipmentOrder.intl_tracking_no, q, session)
            | ci_contains(ShipmentOrder.recipient, q, session)
        )

    totals = list_totals(session, ShipmentOrder, conds)
    rows = session.exec(
        select(ShipmentOrder)
        .where(*conds)
        .order_by(ShipmentOrder.date.desc(), ShipmentOrder.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    # 没有 `orders:read` 的插件令牌只拿集运单本身——理由见 `_may_see_orders`
    return {"items": _read_many(session, rows, brief=brief or not _may_see_orders(current)),
            **totals}


@router.post("/ocr")
async def ocr_shipment(file: UploadFile = File(...)):
    """识别集运「支付详情」截图。成品包裹页 → 集运单号/国际单号/订单时间/渠道；
    内含快递页 → 快递单号列表（要联动挂靠请改用 /{id}/ocr-express）。不落库。"""
    from ..services.ocr import recognize_shipment

    return await run_ocr(file, recognize_shipment)


@router.post("/{shipment_id}/ocr-express", response_model=ShipmentOcrAttachResult)
async def ocr_attach_express(
    shipment_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """识别「内含快递」截图，把截图里的快递单号对应的商品订单挂到本集运单。

    单号匹配不上商品订单 → 只在 unmatched 里回报（不建占位单）；已挂在别的集运单 → 跳过
    不强改（沿用 attach_order 的防误抢语义）。重复上传同一张截图是幂等的。
    路由为 async（run_ocr 要 await 读文件）；DB 用同步 Session，SQLite 建连时已
    check_same_thread=False。**所有碰库的调用都交给线程池**——包括下面那次
    fail-fast 的 `session.get`。原先这里写的是「本地库单次查询亚毫秒级，
    不构成事件循环阻塞」，那句话**只对 SQLite 成立**：切到 MySQL（一等公民后端）
    就是一次网络往返，卡住时按 `read_timeout=30` 能把事件循环冻半分钟。
    与其留一条理由半真的豁免，不如一次调用也包起来——代价为零。"""
    # 长度下界与 ocr.py 共用**同一个常量**：抄一份数字过去，两边一旦漂移，
    # 表现是「识别时判为可疑、挂靠时照挂」（或反过来），而两处都看不出不一致。
    from ..services.ocr import _TRACK_TYPICAL_MIN, recognize_shipment

    shipment = await run_in_threadpool(session.get, ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")

    # **推理之前把连接还回池里。** 上面那次 `session.get` 开了一个读事务，而 OCR 推理
    # 要跑几秒到十几秒——期间这条连接就是「idle in transaction」，在 MySQL 上还一路攥着
    # REPEATABLE READ 快照与 MDL，让并发的 DDL/迁移一起等。同时传几张图，池（20+20）
    # 也更容易见底：`_OCR_CONCURRENCY` 只压住「同时在推理的」，**排队等它的请求照样占着连接**。
    #
    # 这么做不影响正确性，因为上面那次读**本来就只是 fail-fast**（别为一张不存在的集运单
    # 白跑十几秒 OCR）；真正说了算的是下面那条 UPDATE 自带的 EXISTS 守卫
    # （集运单仍在、未软删），它在写入的那一刻原子地复核一次。
    # 换句话说：这一读握不握得住，从来就不是安全边界。
    #
    # 另外两条 OCR 路由不需要这一行——它们在推理前一个字节都不碰库
    # （`ocr_shipment` 压根没有 session 参数；`ocr_order` 的 platform_provider 在推理之后才查）。
    # 鉴权那一侧也已经还过一次（见 auth.get_current_user 末尾）。
    session.rollback()

    fields = await run_ocr(file, recognize_shipment)
    express_nos = fields.get("express_nos") or []

    # **整段写库搬进线程池。** 这是 `async def` 路由，而下面每个快递单号要发 4 条同步语句
    # （SELECT orders / UPDATE orders / refresh 再 SELECT / mirror_to_staging 的 SELECT），
    # 一张 20 个号的截图就是 80+ 次 pymysql 同步往返。跑在事件循环线程上时，
    # 整站（health、静态资源、插件卡片的轮询、其他人的所有请求）全部冻住；
    # MySQL 中途卡一下，`read_timeout=30` 会让**单条语句**把事件循环冻 30 秒。
    #
    # 这是本仓库修过三次的同一个故障：`scheduler_loop`（plugins.py）与
    # `wal_checkpoint_loop`（database.py，注释里写着「实测单次卡了 384 秒」）。
    # 第三次是这里。
    def _attach_all():
        attached: list[Order] = []
        skipped: list[Order] = []
        unmatched: list[str] = []

        for no in express_nos:
            # **短到不像快递单号的，一律不拿去自动挂靠。**
            # OCR 会把一个长号断成两截（`SF1234 56789012` → 取到 `56789012`）。
            # ocr.py 的 `_looks_split` 已经挡掉了「旁边紧邻另一段数字」那种，但那条判据
            # 依赖排版，挡不住所有情形——而这里是**不可逆后果**的所在：
            # 半截号拿去 `Order.express_no == no` 精确匹配，匹配不上只是漏一单，
            # 万一撞上别人的单号，就是把货挂到一张无关订单上，且挂靠是自动提交的。
            # 主流快递单号最短 12 位（顺丰/圆通/中通/申通），留 2 位余量取 10。
            # 判为读不出而不是静默丢弃：它会出现在 unmatched 里，用户看得见、能手动挂。
            if len(no) < _TRACK_TYPICAL_MIN:
                unmatched.append(no)
                continue
            matches = session.exec(
                select(Order).where(Order.express_no == no, not_deleted(Order))
            ).all()
            if not matches:
                unmatched.append(no)
                continue
            for od in matches:
                if od.shipment_order_id is not None and od.shipment_order_id != shipment_id:
                    skipped.append(od)                       # 已挂别的集运单：交给用户手动处理
                    continue
                # **只挂靠，不动状态**：订单的 status 只记国内段，国际段由所挂集运单表达
                # （见 Order.fulfillment_status）。这里曾写过 status="集运中"——那会污染国内段状态，
                # 一旦释放出来，回落到的就是被覆盖过的值而不是真实的「已签收」。
                values = {"shipment_order_id": shipment_id,
                          "version": Order.version + 1, "updated_at": utcnow()}
                if od.shipment_order_id == shipment_id:
                    # **已经挂在本单上了 → 一个字节都不写**。
                    # 原先这里也照发 UPDATE（WHERE 里放行了「已挂本单」以求幂等），
                    # 可 SET 里带着 `version + 1`——于是「幂等」只对挂靠关系成立，
                    # 对乐观锁不成立：同一张截图重传一次，这些订单的 version 就 +1，
                    # 正在编辑其中某单的人下一次保存直接 409，而他什么都没做错。
                    attached.append(od)
                    continue
                # 原子挂靠，守卫与 attach_order 同款：仍未挂靠、未软删、
                # 且集运单在极小竞态窗内没被并发软删。靠 rowcount 判定，避免「读-判断-写」双挂。
                res = session.execute(
                    sa_update(Order)
                    .where(
                        Order.id == od.id,
                        not_deleted(Order),
                        Order.shipment_order_id.is_(None),
                        select(ShipmentOrder.id)
                        .where(ShipmentOrder.id == shipment_id, not_deleted(ShipmentOrder))
                        .exists(),
                    )
                    .values(**values)
                )
                if res.rowcount != 1:                        # 并发被抢走/集运单被并发删除
                    skipped.append(od)
                    continue
                session.refresh(od)                          # 裸 UPDATE 绕过身份映射，重读拿新状态
                mirror_to_staging(session, od, None)         # 若由暂存导入：把新状态镜像回暂存行
                attached.append(od)

        session.commit()
        return ShipmentOcrAttachResult(
            shipment=_read(session, shipment),
            attached=[_brief(o) for o in attached],
            skipped=[_brief(o) for o in skipped],
            unmatched=unmatched,
            express_nos=express_nos,
            unreadable=int(fields.get("unreadable") or 0),
        )
    return await run_in_threadpool(_attach_all)


@router.post("", response_model=ShipmentRead)
def create_shipment(payload: ShipmentCreate, session: Session = Depends(get_session)):
    from ..services.fx import rate_for_date  # 局部导入避免循环

    shipment = ShipmentOrder(**payload.model_dump())
    if shipment.fx_rate is None:
        # 同商品订单：按**单据日期**折算，不是今天
        shipment.fx_rate = rate_for_date(
            session, shipment.date, what=f"建集运订单 {payload.shipment_no or '(无单号)'}")
    shipment.compute_money()
    session.add(shipment)
    session.commit()
    session.refresh(shipment)
    return _read(session, shipment)


@router.get("/{shipment_id}", response_model=ShipmentRead)
def get_shipment(shipment_id: int, session: Session = Depends(get_session)):
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")
    return _read(session, shipment)


@router.patch("/{shipment_id}", response_model=ShipmentRead,
               openapi_extra={"x-scope": "shipment:update"})
def update_shipment(shipment_id: int, payload: ShipmentUpdate,
                    session: Session = Depends(get_session),
                    current=Depends(get_current_user)):
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")
    if not guarded_bump(session, ShipmentOrder, shipment_id, payload.version):
        raise_conflict()

    data = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in data.items():
        setattr(shipment, key, value)
    stamp_fx(session, shipment)                 # create 当时 FxRate 表可能还是空的，每次 PATCH 重试补上
    shipment.compute_money()

    session.add(shipment)
    session.commit()
    session.refresh(shipment)
    # **这一条也要过 `_may_see_orders`。** 它是唯一一个既声明了 `x-scope`
    # （所以插件令牌进得来）、又回整份 `ShipmentRead` 的写端点：
    # 一个只勾了「读集运单」+「改集运单」、**刻意没勾**「读商品订单」的插件，
    # 发一个什么字段都不改的 `PATCH {version}`，响应里 orders 数组就是满的——
    # 订单号、商品标题、采购状态、结算日元、每件物品的名称与数量全在。
    # `_may_see_orders` 的文档说的「他留下的那道闸，从另一个门整个绕开了」，
    # 说的是 GET；这里是第二个门。
    return _read(session, shipment, current=current)


@router.post("/{shipment_id}/order/{order_id}", response_model=ShipmentRead)
def attach_order(shipment_id: int, order_id: int, session: Session = Depends(get_session)):
    """把一个商品订单挂到本集运单（点选添加）。同一个外键 shipment_order_id，与商品页共用。
    仅允许「未挂靠」的商品单：已挂在别的集运单 → 422（先移除再加，防误抢）。

    **不改订单状态**——两条挂靠路径都不改。订单的 status 只记国内段，
    界面显示的国际段状态由本集运单表达（见 Order.fulfillment_status），
    挂上就自动跟随、释放就自动回落，没有需要写的东西。"""
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")
    od = session.get(Order, order_id)
    if not od or od.is_delete:
        raise_not_found("商品订单")
    if od.shipment_order_id == shipment_id:
        return _read(session, shipment)               # 已挂本单，幂等
    # 原子挂载：仅当商品单在 DB 里仍未挂靠（且未软删）、且集运单当前仍存活时才成功，靠 rowcount 判定。
    # 避免「读-判断-写」在并发下双挂/误抢；EXISTS 子查询防极小竞态窗内集运单被并发软删导致挂到已删单；
    # version 在 DB 层自增，不丢失并发的自增（与 guarded_bump 同风格）。
    res = session.execute(
        sa_update(Order)
        .where(
            Order.id == order_id,
            Order.shipment_order_id.is_(None),
            not_deleted(Order),
            select(ShipmentOrder.id)
            .where(ShipmentOrder.id == shipment_id, not_deleted(ShipmentOrder))
            .exists(),
        )
        .values(shipment_order_id=shipment_id, version=Order.version + 1, updated_at=utcnow())
    )
    if res.rowcount != 1:                             # 已被并发挂到别的集运单，或集运单已被并发删除
        raise HTTPException(status_code=422, detail="该商品订单已挂靠其他集运单，请先移除")
    session.commit()
    return _read(session, shipment)


@router.delete("/{shipment_id}/order/{order_id}", response_model=ShipmentRead)
def detach_order(shipment_id: int, order_id: int, session: Session = Depends(get_session)):
    """从本集运单移除一个商品订单（解除外键）。仅当它确实挂在本单才动（幂等）。"""
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")
    od = session.get(Order, order_id)
    if not od or od.is_delete:
        raise_not_found("商品订单")
    # 原子解除：仅当它确实挂在本单才动（幂等）；version 在 DB 层自增，不丢并发自增。
    session.execute(
        sa_update(Order)
        .where(Order.id == order_id, Order.shipment_order_id == shipment_id)
        .values(shipment_order_id=None, version=Order.version + 1, updated_at=utcnow())
    )
    session.commit()
    return _read(session, shipment)


@router.delete("/{shipment_id}")
def delete_shipment(shipment_id: int, session: Session = Depends(get_session)):
    shipment = session.get(ShipmentOrder, shipment_id)
    if not shipment or shipment.is_delete:
        raise_not_found("集运订单")
    # 解除关联商品订单的挂靠，避免留下指向已删集运单的悬空外键。
    #
    # **软删的子订单也要解**（这里刻意不加 `not_deleted`，别按反射加回去）。
    # 软删的订单**可以**挂着集运单：`delete_order` 只软删、不解挂
    # （它自己的注释还写着「对齐集运删除时清子订单外键的做法」）。于是
    #   删订单 A → 删它所在的集运单 S → A 仍然指着 S
    # 而 A 是这条 UPDATE 唯一漏掉的那种行。往后只要在数据库层把 A 恢复出来
    # （`delete_taobao_orders` 的注释写明这是既定的找回路径），它就落进一个死角：
    # `shipment_no` / `fulfillment_status` 都判 `not ship.is_delete` ⇒ 显示为空，
    # 而「未挂靠」筛选判的是 `shipment_order_id IS NULL` ⇒ **它也不在待挂靠列表里**。
    # 两头都看不见，界面上再也挂不回去。
    #
    # 软删的语义是「回收站」，恢复出来该是**能继续用**的状态：
    # 挂靠的那张单已经没了，就该回到「未挂靠」，而不是挂在一个幽灵上。
    # 上面单条解挂那支（`unassign_order`）本来就没有这个过滤，两边现在口径一致。
    session.execute(
        sa_update(Order)
        .where(Order.shipment_order_id == shipment_id)
        .values(shipment_order_id=None, version=Order.version + 1, updated_at=utcnow())
    )
    soft_delete(shipment)
    session.add(shipment)
    session.commit()
    return {"ok": True}
