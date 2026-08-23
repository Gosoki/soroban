"""淘宝抓取暂存（全部淘宝订单）→ 人工确认「导入」才进正式账本。

机器人（将来）只写这里；现在支持手动新建/内联编辑。一单多物用 StagingItem 子表，
结构对齐账本的 Order/OrderItem。导入=从暂存行生成 Order（含全部物品）。"""

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..auth import get_current_user
from ..database import get_session
from ..db.dialect import ci_contains
from ..models.base import guard_cny
from ..models import (
    OrderItem,
    CreatedVia,
    can_advance_purchase,
    StagingItem,
    ImportStatus,
    Order,
    OrderStaging,
    PurchaseStatus,
    utcnow,
)
from ..schemas import StagingCreate, StagingItemRead, StagingRead, StagingUpdate, OrderRead
from .common import build_items, goods_seed, guarded_bump, raise_conflict, raise_not_found, stamp_fx

router = APIRouter(
    prefix="/api/staging", tags=["staging"], dependencies=[Depends(get_current_user)]
)

# 已导入行：暂存字段 → 账本字段的映射（单一真源写穿账本；status=导入工作流状态留暂存自身）。
# price_cny 不在此列：它由物品单价×数量派生（见 sync_from_items），改价走物品、不直接写。
# 写穿被拒时给用户看的中文列名（只用于错误文案，缺项回落到列名本身）。
_SHARED_LABELS = {
    "order_date": "下单日期",
    "order_no": "订单号",
    "purchase_status": "交易状态",
    "title": "商品标题",
}

_SHARED_TO_ORDER = {
    "order_date": "date",
    "order_no": "order_no",
    "title": "title",
    "url": "url",
    "platform_account": "platform_account",
    "platform": "platform",
    "express_no": "express_no",
    "express_company": "express_company",
    "postage_cny": "postage_cny",
    "fx_rate": "fx_rate",
    "purchase_status": "purchase_status",
}


def _linked_order(session: Session, row: OrderStaging) -> Optional[Order]:
    """已导入且账本订单仍在（未软删）→ 返回该订单，否则 None。"""
    if row.imported_order_id is None:
        return None
    order = session.get(Order, row.imported_order_id)
    return order if order and not order.is_delete else None


def _overlay(row: OrderStaging, order: Optional[Order]) -> StagingRead:
    """已导入行的共享字段用账本的实时值覆盖显示（单一真源，两页永远一致）。

    字段清单**从 `_SHARED_TO_ORDER` 派生**，不再手抄一份。原先这里是九行手写赋值，
    与 `_SHARED_TO_ORDER`、`mirror_to_staging` 构成三份各自维护的清单——已经漂了一项
    （这里漏了 `platform`）。当时没造成可见错误，因为 `mirror_to_staging` 会把它写进
    暂存行、读出来正好是对的；但那是**巧合掩盖了发散**：任何一条绕过 mirror 的写路径
    都会让它现形。`test_naming.py` 的守卫钉住「三处覆盖同一份清单」。
    """
    data = StagingRead.model_validate(row)
    if order is not None:
        for staging_field, order_field in _SHARED_TO_ORDER.items():
            setattr(data, staging_field, getattr(order, order_field))
        data.price_cny = order.price_cny   # 派生列，不在 _SHARED_TO_ORDER 里（改价走物品）
        data.items = [
            StagingItemRead(id=it.id, name=it.name, quantity=it.quantity,
                            unit_price_cny=it.unit_price_cny, auto=it.auto)
            for it in order.items
        ]
    return data


def _read_many(session: Session, rows: list[OrderStaging]) -> list[StagingRead]:
    """批量构造响应：一次 IN 查询取回整页已导入行对应的账本订单（连同物品），
    而不是逐行 session.get + 逐单懒加载 items——一屏 100 行时那是 200 条 SQL。"""
    ids = [r.imported_order_id for r in rows if r.imported_order_id is not None]
    linked: dict[int, Order] = {}
    if ids:
        for o in session.exec(
            select(Order)
            .where(Order.id.in_(ids), Order.is_delete.is_(False))
            .options(selectinload(Order.items))
        ).all():
            linked[o.id] = o
    return [_overlay(r, linked.get(r.imported_order_id)) for r in rows]


def _read(session: Session, row: OrderStaging) -> StagingRead:
    return _overlay(row, _linked_order(session, row))


@router.get("", openapi_extra={"x-scope": "staging:read"})
def list_staging(
    session: Session = Depends(get_session),
    import_status: Optional[str] = Query(
        None, description="导入工作流状态（待处理/已导入/已忽略）。"
             "原先它的 wire 名是 status——同一个词在四个端点上指三件事，已按业务段拆开"),
    platform: Optional[str] = None,
    platform_account: Optional[str] = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    order_no: Optional[str] = Query(
        None, description="按订单号**精确**匹配（OCR 去重用，区别于模糊 q）。"
             "与 routers/orders.py 上同名参数同一个用途：OCR 认出一张截图后要问"
             "「这个单号是不是已经有了」，那必须是精确的一问一答——"
             "走模糊 q 再在前端按等号过滤，会被 limit 截断（命中数超过一页时"
             "真正那条排在后面就找不到），表现是**静默多建一条重复的暂存行**；"
             "而且 q 走的是大小写不敏感的 ci_contains，与前端的 === 口径也对不上"),
    q: Optional[str] = Query(None, description="模糊搜：物品名/商品标题/订单号/快递号"),
    legacy_status: Optional[str] = Query(
        None, alias="status", include_in_schema=False,
        description="已废弃：查询参数 status 已按业务段改名"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    # FastAPI 对未知 query 参数**默认忽略**：改名后漏改一处调用方，
    # 表现就是「筛选点了没反应、返回全量、HTTP 200、零日志」。响亮拒掉比静默返回错数据强。
    if legacy_status:
        raise HTTPException(status_code=400,
                            detail="查询参数 status 已改名为 import_status")
    conds = []
    if import_status:
        conds.append(OrderStaging.import_status == import_status)
    if platform:
        conds.append(OrderStaging.platform == platform)
    if platform_account:
        conds.append(OrderStaging.platform_account == platform_account)
    if date_from:
        conds.append(OrderStaging.order_date >= date_from)
    if date_to:
        conds.append(OrderStaging.order_date <= date_to)
    if order_no:
        # 精确：与写入端同一口径（schemas.norm_id 只去空格、不改大小写），
        # 所以这里也用等值比较，不做归一。
        conds.append(OrderStaging.order_no == order_no)
    if q:   # 统一模糊搜：物品名 / 商品标题 / 订单号 / 快递号（物品名用 EXISTS 子查询，不重复行）
        conds.append(
            ci_contains(OrderStaging.order_no, q, session)
            | OrderStaging.title.contains(q, autoescape=True)
            | OrderStaging.express_no.contains(q, autoescape=True)
            | OrderStaging.items.any(StagingItem.name.contains(q, autoescape=True))
        )
    total = session.exec(select(func.count()).select_from(OrderStaging).where(*conds)).one()
    rows = session.exec(
        select(OrderStaging)
        .where(*conds)
        .options(selectinload(OrderStaging.items))   # 一次取回整页的物品，避免每行懒加载(N+1)
        .order_by(OrderStaging.scraped_at.desc(), OrderStaging.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": _read_many(session, rows), "total": total}


@router.post("", response_model=StagingRead, openapi_extra={"x-scope": "staging:write"})
def create_staging(payload: StagingCreate, session: Session = Depends(get_session)):
    from ..services.fx import rate_for_date  # 局部导入避免循环

    row = OrderStaging(**payload.model_dump(exclude={"items", "price_cny"}))  # 价由物品派生
    if row.fx_rate is None:                  # 按下单日期匹配汇率；无记录则退回当前(入库当天)汇率
        row.fx_rate = rate_for_date(
            session, row.order_date, what=f"暂存行 {row.order_no or '(无单号)'}")
    # 最小单位是物品：至少 1 条。播种用「货款」= 种子价 - 邮费，避免邮费摊进单价再被 sync 重复计
    seed_goods = goods_seed(payload.price_cny, payload.postage_cny, payload.items)
    row.items = [StagingItem(**d) for d in build_items(payload.items, seed_goods, payload.title)]
    row.sync_from_items()                    # price_cny = Σ(单价×数量) + 邮费
    session.add(row)
    session.commit()
    session.refresh(row)
    return _read(session, row)


def _cleared_prices(items):
    """把现有物品的单价清成 None，好让 `build_items` 按种子价重新折算。

    与前端 `Staging/index.vue` 补价时做的事逐字节相同
    （`row.items.map(it => ({...it, unit_price_cny: null, auto: true}))`）——
    两处口径必须一致，否则「插件补的价」和「人补的价」会摊出不同的明细。
    `build_items` 只读 name / quantity / unit_price_cny / auto 四项，所以用轻量替身即可。
    """
    from types import SimpleNamespace

    return [SimpleNamespace(name=it.name, quantity=it.quantity,
                            unit_price_cny=None, auto=True) for it in (items or [])]


@router.patch("/{row_id}", response_model=StagingRead, openapi_extra={"x-scope": "staging:write"})
def update_staging(row_id: int, payload: StagingUpdate, session: Session = Depends(get_session),
                   current=Depends(get_current_user)):
    row = session.get(OrderStaging, row_id)
    if not row:
        raise_not_found("暂存记录")
    data = payload.model_dump(exclude_unset=True, exclude={"items", "version", "price_cny"})  # 价由物品派生
    order = _linked_order(session, row)       # 普通 SELECT，不取写锁

    # 锁序恒为 orders → orderstaging。全仓另外几条同时写这两张表的路径
    # （orders.update_order / orders.delete_order / import_staging / tags.soft_delete_account_orders）
    # 都是这个方向；这里若反过来（先暂存后账本）就构成 AB-BA 锁环——同一对「暂存行 ↔ 已导入订单」
    # 被两条路径并发写时，MySQL/InnoDB 报 1213 死锁并回滚一方，而 main.py 只挂了
    # IntegrityError / ValueError 两个 handler，OperationalError(1213) 会直接逃成裸 500。
    # SQLite 是单写者串行，这个 bug 在本地测试里**永远复现不出来**。
    if order is not None:
        # 已导入：共享字段写穿到账本（唯一真源），仅「导入状态」留在暂存自身。
        # 账本侧也走乐观锁：原子自增账本 version，让订单页也能察觉此次改动。
        if not guarded_bump(session, Order, order.id, order.version):
            raise_conflict()
    # 暂存行乐观锁：加载后被爬虫/他人改过 → 409，前端刷新（version 原子自增 + 刷新 updated_at）
    if not guarded_bump(session, OrderStaging, row_id, payload.version):
        raise_conflict()

    if order is not None:
        for key, value in data.items():
            if key in _SHARED_TO_ORDER:
                # 写穿时不许把账本必填列写空。约束的真身不在 StagingUpdate 上——爬虫回灌、
                # 未导入行编辑都合法地要 null，放 schema 会误伤；只有写穿这一刻才必须挡。
                # 不挡的话 null 会一路走到 commit，被数据库拦成 409「数据完整性冲突」，
                # 而前端把 409 当乐观锁冲突，弹「数据已变，已刷新」——用户完全不知道错在哪。
                # 也**不要**在这里 coalesce 成默认值（import_staging 那样）：导入是「批量入账时
                # 兜底未知值」，编辑是「用户明确按了清除」，把清除静默换成今天的日期比 409 更糟。
                # **交易状态：自动化写入只许向前推进。**
                # `can_advance_purchase` 的规则此前在后端**一个写路径上都没被调用过**——
                # 它只活在前端与插件客户端里。而插件的 `_patch` 收到 409 之后
                # **只重新取 version、原样重发同一个 patch dict**：
                # 「用户在这一轮抓取里改过状态」恰恰是唯一会触发 409 的信号，
                # 而重试把插件那一刻的旧决策一起带了过来。实测过的序列：
                #   ¥1000 的单已导入 → 插件开始一轮抓取（快照记下「待收货」）
                #   → 用户把它标「退款」（看板 −¥1000，暂存 version 被镜像顶高）
                #   → 插件发 PATCH「已签收」→ 409 → 重取 version 原样重发 → 200
                #   → 账本回到「已签收」⇒ **一笔已退款的钱重新进了看板合计**，全程 200、零日志。
                # 这正是 `can_advance_purchase` 第 2 条（终态不许被自动改写）存在的理由。
                #
                # 判据是「调用者是不是插件」，不是一刀切：那个函数的 docstring 明写
                # 「只约束自动化写入，人在界面上手动改不走这里——用户说了算」。
                # 用 422 而不是 409：409 会让插件再重试一次，正好又掉进同一个洞。
                if (key == "purchase_status" and getattr(current, "_plugin_claims", None)
                        and value and value != order.purchase_status
                        and not can_advance_purchase(order.purchase_status, value)):
                    raise HTTPException(status_code=422, detail=(
                        f"不接受把已导入订单的交易状态从「{order.purchase_status}」改成「{value}」："
                        "自动写入只能向前推进，且终态（退款/交易关闭）不允许被自动改写。"
                        "确要回退请在「商品订单」页手动改。"))
                col = Order.__table__.columns.get(_SHARED_TO_ORDER[key])
                if value is None and col is not None and not col.nullable:
                    raise HTTPException(
                        status_code=422,
                        detail=f"该记录已导入账本，「{_SHARED_LABELS.get(key, key)}」不能清空（账本必填）",
                    )
                setattr(order, _SHARED_TO_ORDER[key], value)
                setattr(row, key, value)   # 暂存行自身原始列也同步，避免 tags._data_values / 列表筛选读到陈旧值
            elif key == "import_status":
                # 已导入行的**导入状态**不接受直接改：/ignore 端点特意用 `imported_order_id IS NULL`
                # 挡住「已导入 → 已忽略」，PATCH 若能改就是同一个动作两个入口给相反结论，
                # 且会留下「状态=已忽略、却还挂着活账本单」的发散行（_overlay 仍按账本覆盖显示，
                # tags._data_values 又会把它当已丢弃而跳过 → 账号占用凭空消失）。
                # 工作流状态只由 /import、/ignore、删除来推进。
                if value != row.import_status:
                    raise HTTPException(
                        status_code=409,
                        detail="该记录已导入账本，导入状态不能直接修改"
                               "（要撤销请先在「商品订单」页删除对应订单）",
                    )
        # **插件不许改一张已导入订单的钱。**
        # 下面那几行是「写穿账本」——暂存与账本是同一条记录的两个视图，人在哪一页改都该一致，
        # 这对**人**是对的。但对插件不是：那笔钱可能是人导入后手工核过、改过的，
        # 而插件回灌是**定时反复**发生的，覆盖一次就永久了（HTTP 200、无 409、runlog 也不记）。
        #
        # 仓库里那个淘宝插件自己很克制（已导入的行只推进状态、只在账本那格为空时补快递号），
        # 但那是**插件的自觉，不是核心的强制**——`runlog` 存在的理由逐字就是这一句。
        # 换一个插件、或哪天改这段时忘了，同一个洞立刻复发。
        #
        # 只挡「带钱的字段」，不挡状态/快递号/链接那些——插件补空格正是它该做的事，
        # 也是它现在唯一会对已导入行做的事。所以这条闸对现有插件**一次都不会触发**，
        # 它防的是下一个插件。
        if getattr(current, "_plugin_claims", None):
            money = [f for f in ("items", "price_cny", "postage_cny", "fx_rate")
                     if getattr(payload, f, None) is not None]
            if money:
                raise HTTPException(status_code=403, detail=(
                    f"这一行已经导入账本了，插件不能再改它的金额字段（{'、'.join(money)}）。"
                    "那笔钱可能已经被人工核对过——要更正请在「商品订单」页改。"
                    "状态、快递单号、快递公司、商品链接不受此限。"))
        if payload.items is not None:                   # 物品写穿账本（单一真源）+ 暂存镜像，两页一致
            # 种子只认本次显式传来的 price_cny，不回退到当前价——理由同 orders.update_order
            seed_goods = goods_seed(payload.price_cny, order.postage_cny, payload.items)
            built = build_items(payload.items, seed_goods, order.title)
            order.items = [OrderItem(**d) for d in built]
            row.items = [StagingItem(**d) for d in built]
        elif payload.price_cny is not None and not order.price_cny:
            # `price_cny` 单独送来（**不带 items**）时的口径：**只在这一行现在一分钱都没有时才补**。
            #
            # 为什么要有：淘宝插件对「整单一条物品都没解析出来」写了一条兜底——把订单实付当**种子价**
            # 推上来，它自己的注释是「宁可明细摊得不准，也要保住『订单总额 = 实付』这个底线」。
            # 但那条路径上 `row["items"]` 是 `[]`，而插件侧 `if row.get("items") and …` 把空列表判假
            # ⇒ items 不进 body ⇒ 发出来的是 `PATCH {price_cny}` 不带 items
            # ⇒ 这里 `payload.items is None` ⇒ 价被 `model_dump(exclude=...)` 丢掉
            # ⇒ `sync_from_items()` 按**原有物品**重算 ⇒ **金额一分没变**。
            # 全程 200 OK，插件记一笔 updated，`runlog` 也不会记（那不是拒收，是成功）。
            #
            # 三种做法里选了这一种：422 会把插件这条**救场**路径打进 failed 桶；
            # 「当种子重建物品」会覆盖用户已经手工拆好的明细。
            # 「只补空格」与插件自己的「空格可以补，非空格绝不覆盖」、以及前端 `noPrice` 那道判据同源，
            # 只会增加信息、不会覆盖任何东西。
            built = build_items(_cleared_prices(order.items),
                                goods_seed(payload.price_cny, order.postage_cny), order.title)
            order.items = [OrderItem(**d) for d in built]
            row.items = [StagingItem(**d) for d in built]
        # 缺汇率就补一条——与 orders.update_order:220、misc、shipment 三处同一刀。
        # 漏掉这里的后果不是报错：jpy_auto/jpy_settled 一起是 NULL，看板 SUM 跳过它、
        # 笔数照数，「笔数 +1、金额 +0」。而汇率格在暂存页是可编辑且可清空的，
        # 清一下就能让一条已导入的账本单悄悄变成不计钱的行。
        stamp_fx(session, order)
        row.fx_rate = order.fx_rate                     # 暂存镜像跟着走，免得两页显示不同汇率
        order.sync_from_items()                         # 账本价+日元由物品派生（fx 变也重算）
        # **暂存价从账本单镜像过来，不是拿暂存自己那份物品重算。**
        # 这一行原先写的是 `row.sync_from_items()`，注释还写着「暂存价镜像」——
        # 但它做的是重算，不是镜像。已导入的**0 物品**暂存行（`import_staging` 的 else 分支
        # 只给账本单补物品、不回写 `row.items`）走到这里，算出来的就是 `0 + 邮费`：
        #
        #   暂存 ¥300 / 0 物品 → 导入得到订单 ¥300 → 在**暂存页**改一下标题
        #   （甚至只送一个 version、一个字段都没改，也会走到这里；插件的 express_no 更新同样）
        #   ⇒ 暂存行变 ¥0.00，而 PATCH 的响应里还是 300（`_overlay` 用账本值覆盖显示）
        #   → 在订单页删掉该单（暂存复位成「待处理」，此时不再有账本值可覆盖）
        #   → 再点「导入账本」⇒ 建出一张 **¥0.00 / 0 円** 的订单。
        #
        # 与 `common.mirror_to_staging` 是**同一个伤害的两条路**（见审计报告 §154）。
        # 那次只修了订单 PATCH 那条，这条漏了——而 §154 的守卫走的正是订单那条路，
        # 够不到这一行，所以它一直是绿的。
        row.price_cny = guard_cny(order.price_cny) if order.price_cny is not None else None
        session.add(order)
        session.add(row)
        session.commit()                                # order_no 撞账本唯一索引 → IntegrityError → 409
        session.refresh(row)
        return _read(session, row)

    # 未导入：编辑暂存自身副本
    #
    # `import_status` 同样不接受直接改——这里和上面「已导入」那支是**同一条规则**：
    # 导入状态只由 /import、/ignore、删除来推进。原先只在已导入分支挡，未导入行
    # 就能被 PATCH 成「已导入」而 `imported_order_id` 仍是 NULL：列表按状态筛选时
    # 它算已导入（用户以为账已经记了），`/import` 却照样能再导一次（那里判的是
    # imported_order_id），于是同一笔货可以进账本两遍。
    if data.get("import_status") not in (None, row.import_status):
        raise HTTPException(
            status_code=422,
            detail="导入状态不能直接修改：请用「导入账本」或「忽略」来推进",
        )
    data.pop("import_status", None)
    for key, value in data.items():
        setattr(row, key, value)
    if payload.items is not None:                       # 给了 items 就整体替换（[] → 自动补 1 条占位）
        # 种子只认本次显式传来的 price_cny，不回退到当前价——理由同 orders.update_order
        seed_goods = goods_seed(payload.price_cny, row.postage_cny, payload.items)
        row.items = [StagingItem(**d) for d in build_items(payload.items, seed_goods, row.title)]
    elif payload.price_cny is not None and not row.price_cny:
        # 口径同上面已导入那一支（见那里的长注释）：只补空格，绝不覆盖。
        row.items = [StagingItem(**d) for d in build_items(
            _cleared_prices(row.items), goods_seed(payload.price_cny, row.postage_cny), row.title)]
    row.sync_from_items()
    session.add(row)
    session.commit()
    session.refresh(row)
    return _read(session, row)


@router.delete("/{row_id}", openapi_extra={"x-scope": "staging:promote"})
def delete_staging(row_id: int, session: Session = Depends(get_session)):
    row = session.get(OrderStaging, row_id)
    if not row:
        raise_not_found("暂存记录")
    if _linked_order(session, row) is not None:
        # 已导入且账本单仍在：直接删暂存会孤立账本 Order（永远占死 order_no 唯一号、无法再导入）。
        # 要求先在「商品订单」页删掉对应订单，再删此暂存，保持暂存↔账本一致。
        raise HTTPException(
            status_code=409,
            detail="该记录已导入账本，请先在「商品订单」页删除对应订单，再删此暂存记录",
        )
    session.delete(row)
    session.commit()
    return {"ok": True}


@router.post("/{row_id}/ignore", response_model=StagingRead, openapi_extra={"x-scope": "staging:promote"})
def ignore_staging(row_id: int, session: Session = Depends(get_session)):
    # 原子标记忽略：version 在 DB 层自增（而非 Python 读-改-写），避免并发忽略/爬虫写
    # 丢失 version 自增、绕过乐观锁；与 import_staging 的原子门闸同风格。
    # 门闸再加 imported_order_id IS NULL：已导入的行不允许翻成「已忽略」，否则状态=已忽略
    # 却仍挂着活账本单（_read 还会覆盖显示），导致工作流状态与账本发散。
    res = session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.id == row_id, OrderStaging.imported_order_id.is_(None))
        .values(import_status=ImportStatus.ignored.value,
                version=OrderStaging.version + 1, updated_at=utcnow())
    )
    if res.rowcount != 1:
        row = session.get(OrderStaging, row_id)     # 区分「不存在」与「已导入」
        if row is None:
            raise_not_found("暂存记录")
        raise HTTPException(status_code=409, detail="该记录已导入账本，不能忽略")
    session.commit()
    row = session.get(OrderStaging, row_id)
    return _read(session, row)


@router.post("/{row_id}/import", response_model=OrderRead, openapi_extra={"x-scope": "staging:promote"})
def import_staging(row_id: int, session: Session = Depends(get_session)):
    """从暂存行生成正式淘宝订单（含全部物品），并标记暂存为已导入。"""
    from ..services.fx import JST, rate_for_date  # 局部导入避免循环

    row = session.get(OrderStaging, row_id)
    if not row:
        raise_not_found("暂存记录")
    if row.imported_order_id is not None:
        raise HTTPException(status_code=409, detail="该记录已导入")

    order = Order(
        # 兜底日期用 **JST 今天**：全仓的「今天」都是 JST（fx.JST、fx_rate ingest、
        # 暂存页「入库日期」显示），只有这里用服务器本地时区。部署在 UTC 机器上时，
        # JST 的 00:00–09:00 之间导入的单会被记成**前一天**，与同一批次里 order_date
        # 有值的单错开一天，看板按日汇总时对不上。
        date=row.order_date or dt.datetime.now(JST).date(),
        order_no=row.order_no,
        title=row.title,
        url=row.url,                         # 商品链接随单迁移
        platform_account=row.platform_account,
        platform=row.platform,               # 来源随单迁移到账本
        express_no=row.express_no,
        express_company=row.express_company,   # 快递公司与单号是一对，一起迁移
        postage_cny=row.postage_cny,         # 邮费随单迁移
        # 优先暂存记录的汇率；否则按下单日期匹配（库里空则按手填值兜底，过期会留痕）
        fx_rate=row.fx_rate or rate_for_date(
            session, row.order_date, what=f"暂存导入建单 {row.order_no or '(无单号)'}"),
        purchase_status=row.purchase_status or PurchaseStatus.paid.value,   # 订单状态一同迁移
        created_via=CreatedVia.imported.value,
    )
    # 物品（含单价/auto）随单迁移；订单价由物品派生（= 暂存价，一致）。暂存无物品时兜底自动生成 1 条。
    if row.items:
        order.items = [OrderItem(name=it.name, quantity=it.quantity,
                                 unit_price_cny=it.unit_price_cny, auto=it.auto)
                       for it in row.items]
    else:
        # 0 物品兜底：种子用货款(总价-邮费)，避免 sync 再加邮费重复计（对齐其它 build_items 站点）
        seed_goods = goods_seed(row.price_cny, row.postage_cny)
        order.items = [OrderItem(**d) for d in build_items([], seed_goods, row.title)]
    order.sync_from_items()
    session.add(order)
    session.flush()                             # 拿到 order.id

    # 原子门闸：只有 imported 仍为空的那次导入能成功，防并发/重复导入建重复单
    claimed = session.execute(
        sa_update(OrderStaging)
        .where(OrderStaging.id == row_id, OrderStaging.imported_order_id.is_(None))
        .values(
            import_status=ImportStatus.imported.value,
            # **导入时兜底过的列都要回写，不能只回写一个。**
            # 这里对三个字段做了 coalesce（order_date→今天、fx_rate→按日期匹配、
            # purchase_status→待发货），而原先只把第三个写回暂存行。
            # 另外两列于是**永远停在 NULL**：`_overlay` 读的时候用账本值覆盖，
            # 所以页面上看着是对的；但 `list_staging` 的 date_from/date_to 筛的是**原始列**
            # ⇒ 一条 OCR 认不出下单时间的暂存行导入之后，任何日期筛选（包括「本月」）
            # 都会把它剔掉，而它明明显示着落在范围内的日期。
            order_date=order.date,
            fx_rate=order.fx_rate,
            purchase_status=order.purchase_status,          # 快照对齐账本（此后以账本为准，读时覆盖）
            imported_order_id=order.id,
            version=OrderStaging.version + 1,
            updated_at=utcnow(),
        )
    )
    if claimed.rowcount != 1:                    # 被别人抢先导入 → 回滚刚建的订单
        # 显式 rollback：get_session 的 with 块退出时 Session.close() 也会回滚，但那是隐式副作用。
        # 这里刚 flush 过一张 Order，把「不提交」写成代码而不是靠调用方的清理顺序。
        session.rollback()
        raise HTTPException(status_code=409, detail="该记录已导入")

    session.commit()                            # order_no 与账本冲突 → IntegrityError → 全局 409
    session.refresh(order)
    return order
