"""淘宝订单 CRUD。软删过滤、乐观锁、金额重算、OrderItem 子表替换。"""

import datetime as dt
from typing import Optional

from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import ci_contains
from ..models import ShipmentOrder, OrderItem, ImportStatus, Order, OrderStaging, utcnow
from ..schemas import OrderCreate, OrderRead, OrderUpdate, norm_code, norm_id
from .common import (
    build_items, goods_seed, guarded_bump, list_totals, mirror_to_staging, raise_conflict,
    raise_not_found, run_ocr, soft_delete, stamp_fx
)

router = APIRouter(
    prefix="/api/orders", tags=["orders"], dependencies=[Depends(get_current_user)]
)


def _check_shipment(session: Session, shipment_id, *, lock: bool = False):
    """挂靠的集运订单必须存在且未软删（防悬空/无效外链）。

    用标量 SELECT 直读 DB，而非 session.get——后者命中身份映射缓存会返回加载时的旧
    is_delete，同事务里第二次校验就形同虚设。

    `lock=True` 时加 `FOR UPDATE`，用于**写入前的最后一次复核**。这一笔是必须的，
    因为「重发一次普通 SELECT」在两种引擎下根本不是一回事：

    - SQLite：pysqlite 只在 DML 前才发 BEGIN，SELECT 跑在 autocommit 下、没有读快照，
      所以重读确实能看到别人刚提交的软删 → TOCTOU 被闭合。
    - MySQL：InnoDB 默认 REPEATABLE READ，读视图在事务内**第一次一致性读**时就钉死了
      （而且钉住它的往往是更早的鉴权查询 `session.get(User, ...)`——FastAPI 的
      per-request 依赖缓存让 get_current_user 与本处理器共用同一个 Session）。
      之后所有普通 SELECT 都读同一份快照，重读结果与第一次**恒等**，等于没写。

    `FOR UPDATE` 是 locking read：读已提交的最新版本，并锁住该行直到本事务结束——
    对方要么已经软删完（我们读到并 422），要么被挡住直到我们提交。
    SQLAlchemy 的 sqlite 方言对 `FOR UPDATE` 是 no-op（编译产物里根本不出现），
    所以 SQLite 侧行为一字未变。"""
    if shipment_id is not None:
        stmt = select(ShipmentOrder.id).where(
            ShipmentOrder.id == shipment_id, ShipmentOrder.is_delete.is_(False)
        )
        if lock:
            stmt = stmt.with_for_update()
        if session.execute(stmt).first() is None:
            raise HTTPException(status_code=422, detail="所属集运订单不存在或已删除")


@router.get("", openapi_extra={"x-scope": "orders:read"})
def list_orders(
    session: Session = Depends(get_session),
    # 对外仍叫 ?id=（前端在用）；Python 侧换个名字，别遮蔽内建 id()
    only_id: Optional[int] = Query(
        None, alias="id", description="定位单条：供集运页点订单号跳转过来隔离显示"),
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    fulfillment_status: Optional[str] = None,
    purchase_status: Optional[str] = Query(None, description="只按订单**自身**的采购段状态筛（不看挂靠的集运单）"),
    platform: Optional[str] = None,
    platform_account: Optional[str] = None,
    express_no: Optional[str] = None,
    shipment_order_id: Optional[int] = None,
    unassigned: Optional[bool] = Query(None, description="仅未挂靠集运的订单（供集运页点选添加）"),
    recipient: Optional[str] = Query(None, description="按所属集运单的收货人筛选"),
    order_no: Optional[str] = Query(None, description="按订单号精确匹配（OCR 去重用，区别于模糊 q）"),
    q: Optional[str] = Query(None, description="按订单号搜索（子串模糊）"),
    legacy_status: Optional[str] = Query(
        None, alias="status", include_in_schema=False,
        description="已废弃：查询参数 status 已按业务段改名"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    # FastAPI 对未知 query 参数**默认忽略**：改名后漏改一处调用方，
    # 表现就是「筛选点了没反应、返回全量、HTTP 200、零日志」。响亮拒掉比静默返回错数据强。
    if legacy_status:
        raise HTTPException(status_code=400,
                            detail="查询参数 status 已改名为 fulfillment_status（显示口径）或 purchase_status（订单自身）")
    conds = [Order.is_delete.is_(False)]
    if only_id is not None:
        conds.append(Order.id == only_id)
    if unassigned:
        conds.append(Order.shipment_order_id.is_(None))
    if recipient:
        # 收货人在**集运表**上，不在订单上。用子查询而不是给订单加一列冗余字段：
        # 加列就要在挂靠/解挂/改集运单收货人三处同步，漏一处就是两张表说法不一，
        # 而这种不一致没有任何报错、只会让筛选悄悄少几单。
        conds.append(Order.shipment_order_id.in_(
            select(ShipmentOrder.id).where(ShipmentOrder.recipient == recipient,
                                           ShipmentOrder.is_delete.is_(False))
        ))
    if date_from:
        conds.append(Order.date >= date_from)
    if date_to:
        conds.append(Order.date <= date_to)
    if fulfillment_status:
        # 筛选口径必须与**显示**口径一致（fulfillment_status）：显示的是集运单状态、
        # 筛选却按订单自身状态，就会出现「列表里明明显示着一堆『已发出』，
        # 筛『已发出』却一条都搜不到」。用相关子查询而非 JOIN——不改变结果行的形状，
        # 也就不会影响上面那句 count 与下面的分页。
        ship_status = (
            select(ShipmentOrder.shipment_status)
            .where(ShipmentOrder.id == Order.shipment_order_id,
                   ShipmentOrder.is_delete.is_(False))
            .scalar_subquery()
        )
        conds.append(func.coalesce(ship_status, Order.purchase_status) == fulfillment_status)
    if purchase_status:
        conds.append(Order.purchase_status == purchase_status)
    if platform:
        conds.append(Order.platform == platform)
    if platform_account:
        conds.append(Order.platform_account == platform_account)
    if express_no:
        # 按写入时的同一套规则归一再比：库里存的是 UPPER(TRIM(...))，查询侧不归一就永远差一点
        conds.append(Order.express_no == norm_code(express_no))
    if shipment_order_id is not None:
        conds.append(Order.shipment_order_id == shipment_order_id)
    if order_no:
        # 精确匹配：OCR 去重靠它，不受子串 q 的 20 条上限影响。同样按写入规则归一（只去首尾空格）
        conds.append(Order.order_no == norm_id(order_no))
    if q:   # 统一模糊搜：物品名 / 商品标题 / 订单号 / 快递号（物品名用 EXISTS 子查询，不重复行）
        conds.append(
            ci_contains(Order.order_no, q, session)
            | Order.title.contains(q, autoescape=True)
            | Order.express_no.contains(q, autoescape=True)
            | Order.items.any(OrderItem.name.contains(q, autoescape=True))
        )

    totals = list_totals(session, Order, conds)
    rows = session.exec(
        select(Order)
        .where(*conds)
        # 两个关系都预加载：items 供 OrderRead 展开，shipment_order 供 fulfillment_status。
        # 少一个都会变成整页逐行发 SQL（N+1）——tests/test_queries.py 钉着查询条数。
        .options(selectinload(Order.items), selectinload(Order.shipment_order))
        .order_by(Order.date.desc(), Order.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [OrderRead.model_validate(r) for r in rows], **totals}


@router.post("/ocr")
async def ocr_order(
    file: UploadFile = File(...),
    # 用户上传时自己选的来源。**不传 = 保持自动判别**，行为与加这个参数之前逐字节相同——
    # 三条既有的上传测试都只发 files 不带它，那不是疏漏，是「缺省必须不变」的验收条件。
    # 放 Form 不放 Query：这本来就是 multipart 请求，提示与图片同体不会错配；
    # 中文值进 query 要 percent-encode，还会落进 access log 和浏览器历史。
    platform_hint: Optional[str] = Form(None, description="用户指定的来源平台；不传=自动判别"),
    session: Session = Depends(get_session),
):
    """识别订单详情截图，抽取快递公司/快递号/订单号/成交价供前端自动填表。"""
    import functools

    from ..services.ocr import PLATFORM_CHOICES, recognize_order
    from .plugins import platform_provider

    hint = (platform_hint or "").strip() or None
    # 认不得的值**必须报错，不许静默忽略**。静默忽略的表现是：对话框弹了、用户选了、
    # 图也传上去了、HTTP 200、汇总照常显示——而那句选择一路蒸发，没有任何人会发现。
    # 同一个教训在这个文件里已经有先例（下面拒掉 legacy `status` 那段）。
    if hint and hint not in PLATFORM_CHOICES:
        raise HTTPException(422, f"来源平台只能是：{'、'.join(PLATFORM_CHOICES)}（收到「{hint}」）")

    fields = await run_ocr(file, functools.partial(recognize_order, platform_hint=hint))
    # 认出不是闲鱼时，顺带告诉前端「这台机器上有没有插件在管这个平台」。
    # 判断放后端是因为答案取决于**用户装了什么、配了哪些账号**，前端无从得知；
    # 而让前端去 GET /api/plugins 自己比对，就得在前端写一份平台↔插件的对应关系
    # ——`test_frontend_does_not_hardcode_any_plugin_id` 正是不许这么做。
    if fields.get("platform_warning"):
        # **交给线程池**：`platform_provider` 对每个已发现的插件各做一次 `session.get`，
        # 而这是 `async def` 路由——直接跑就是把 N 次同步 DB 往返压在事件循环线程上。
        # 与 `ocr_attach_express` 同一条理由（见那里的长注释），只是量级小一些：
        # 插件数少，但「小的也会冻」——MySQL 卡一下，单条语句就是 30 秒。
        fields["platform_plugin"] = await run_in_threadpool(
            platform_provider, session, fields.get("platform") or "")
    return fields


@router.post("", response_model=OrderRead)
def create_order(payload: OrderCreate, session: Session = Depends(get_session)):
    from ..services.fx import rate_for_date  # 局部导入避免循环

    data = payload.model_dump(exclude={"items", "price_cny"})   # 订单价由物品派生，不直接落库
    order = Order(**data)
    _check_shipment(session, order.shipment_order_id)   # 挂靠的集运单不存在/已删 → 友好 422（而非 FK 撞库转 409）
    if order.fx_rate is None:
        # 按**下单那天**折算，不是今天。爬虫会抓回前几天买的东西，
        # 用今天的汇率记几天前的单是实打实的记错账。那天没有记录就退回最新的（见 rate_for_date）。
        order.fx_rate = rate_for_date(
            session, order.date, what=f"建商品订单 {order.order_no or '(无单号)'}")
    # 最小单位是物品：至少 1 条（无物品则按商品名+货款自动生成，灰显可改）。
    # 播种用「货款」= 订单价种子 - 邮费，避免把邮费也摊进物品单价（否则 sync 加邮费会重复计）。
    seed_goods = goods_seed(payload.price_cny, payload.postage_cny, payload.items)
    order.items = [OrderItem(**d) for d in build_items(payload.items, seed_goods, payload.title)]
    order.sync_from_items()                   # price_cny = Σ(单价×数量) + 邮费，并重算日元
    session.add(order)
    session.flush()                           # 写入并占写锁；FK 保证集运单硬存在
    # flush 后复核集运单仍未软删，闭合「校验通过→集运单被并发软删→订单仍挂上」的 TOCTOU。
    # lock=True 不是可选项：MySQL 的 REPEATABLE READ 会让普通重读拿到与第一次相同的快照，
    # 不加锁读这一步在 MySQL 上是死代码（见 _check_shipment 的说明）。
    _check_shipment(session, order.shipment_order_id, lock=True)
    session.commit()
    session.refresh(order)
    return order


@router.get("/{order_id}", response_model=OrderRead)
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        raise_not_found("淘宝订单")
    return order


@router.patch("/{order_id}", response_model=OrderRead)
def update_order(order_id: int, payload: OrderUpdate, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        raise_not_found("淘宝订单")
    if not guarded_bump(session, Order, order_id, payload.version):
        raise_conflict()
    # 集运单存活校验放在 guarded_bump 之后：此时写事务已开启并持写锁，校验与写入同一事务。
    # 同样必须 lock=True——MySQL 下不加锁的 SELECT 读的是事务开头钉住的快照，
    # 看不见并发提交的软删（与 attach_order 把 EXISTS 塞进 UPDATE 语句是同一个道理）。
    if "shipment_order_id" in payload.model_fields_set:
        _check_shipment(session, payload.shipment_order_id, lock=True)

    # price_cny 由物品派生，不接受直接改（订单列表 RMB 只读，改价走物品）
    data = payload.model_dump(exclude_unset=True, exclude={"version", "items", "price_cny"})
    for key, value in data.items():
        setattr(order, key, value)

    built = None
    if payload.items is not None:            # 给了 items 就整体替换（[] → 自动补 1 条占位）
        # 种子价**只**认本次显式传来的 price_cny，绝不回退到订单当前价。
        # 回退过会造成两处反直觉：① 用户把所有单价清空再保存，旧总价会整个折到第一条物品上
        # （而「只清空部分单价」是记 0 待补价——同一个动作两种结果）；② 同一次请求既改邮费又
        # 送无单价物品时，货款被重算成「旧总价 − 新邮费」，总价看着没变、货款却悄悄改了。
        # 现在没给种子就是「不知道单价」，一律记 0 + auto（灰显待补价），与部分清空口径一致。
        seed_goods = goods_seed(payload.price_cny, order.postage_cny, payload.items)
        built = build_items(payload.items, seed_goods, order.title)
        order.items = [OrderItem(**d) for d in built]
    elif not order.items:
        # 兜底：历史订单可能一条物品都没有 → 补占位，守住「≥1 物品」不变量。
        # 这条路径**必须**用订单当前价当种子：此时价格只存在于订单行上，不接过来就丢了。
        seed = payload.price_cny if payload.price_cny is not None else order.price_cny
        order.items = [OrderItem(**d)
                       for d in build_items([], goods_seed(seed, order.postage_cny), order.title)]
    stamp_fx(session, order)                 # 同上：缺汇率的行每次 PATCH 都重试补，顺带自愈存量脏行
    order.sync_from_items()                  # 无论是否改物品：价格恒由物品派生，并按新 fx/override 重算日元
    mirror_to_staging(session, order, built)  # 若由暂存导入：镜像回暂存行，避免删单后重导丢失编辑

    session.add(order)
    session.commit()
    session.refresh(order)
    return order


@router.delete("/{order_id}")
def delete_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order or order.is_delete:
        raise_not_found("淘宝订单")
    soft_delete(order)
    session.add(order)
    # 若此单是从暂存导入的：删除后把暂存行的挂靠清掉、状态回「待处理」，使其可重新导入
    # （对齐集运删除时清子订单外键的做法，避免暂存行永远卡在「已导入」且指向已删订单）。
    session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.imported_order_id == order_id)
        .values(imported_order_id=None, import_status=ImportStatus.pending.value,
                version=OrderStaging.version + 1, updated_at=utcnow())
    )
    session.commit()
    return {"ok": True}
