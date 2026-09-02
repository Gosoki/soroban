"""Shared router helpers: optimistic lock (DB-level guard), soft delete, errors, item building,
OCR 截图上传（校验 + 线程池执行）。"""

import logging
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Callable

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import update as sa_update
from sqlmodel import Session

from ..models import utcnow
from ..models.base import unconverted_clause
from ..models.base import guard_cny

log = logging.getLogger("soroban")


# 分页 offset 的上界。**必须有**，而且两个引擎都要能接住同一个值：
# `?offset=99999999999999999999` 在 SQLite 上是
# `OverflowError: Python int too large to convert to SQLite INTEGER`，
# 在真 MySQL 上是 `(1064, 'You have an error in your SQL syntax')` ——
# 两者都不在 `main.py` 那五个 exception handler 的类型里，**双双裸 500**。
# 而「接受哪一段」本身两边还不一样，等于同一个请求换个后端就是两种行为。
#
# 取一千万：这本账本几万条到头了，一千万条早已远超任何真实翻页；
# 越界回 422（FastAPI 自带的参数校验），说得清、可预期。
MAX_OFFSET = 10_000_000

_CNY_Q = Decimal("0.01")     # 人民币量化到分
MAX_OCR_BYTES = 10 * 1024 * 1024      # 截图上限 10MB（手机截图通常 < 2MB）
_OCR_CONCURRENCY = 2         # 同时在解码/推理的 OCR 请求数上限，见 run_ocr
_OCR_LIMITER = None


def _ocr_limiter():
    """OCR 专用并发闸。**懒建**：CapacityLimiter 要绑定到当前 async 后端，
    模块导入时还没有事件循环。"""
    global _OCR_LIMITER
    if _OCR_LIMITER is None:
        import anyio

        _OCR_LIMITER = anyio.CapacityLimiter(_OCR_CONCURRENCY)
    return _OCR_LIMITER


def goods_seed(price_cny, postage_cny, items=None):
    """「订单价种子」→「货款种子」：扣掉邮费。没给价就没有种子（None）。

    订单价 = Σ(单价×数量) + 邮费，而 sync_from_items 事后会自己再加一次邮费；所以喂给
    build_items 的种子必须是**已扣邮费**的货款，否则邮费被算两遍。这个换算在建单/改单/
    导入/暂存共 6 处出现过，各写一份就迟早有一处忘了减——收敛到这里。

    **邮费大于订单价时在这里拒绝，而不是往下夹零。**
    `build_items` 对负种子的处理是 `if seed_goods < 0: seed_goods = 0`——
    于是「价 10、邮费 100」不会报错，而是把物品单价全记成 0，
    再由 `sync_from_items` 得出「订单价 = 0 + 邮费 = 100」：
    **一张原价 200 的单被静默改成 100，物品单价 0.00，全程 200 OK。**

    这条闸原先只挂在 `OrderCreate` / `StagingCreate` 的 model_validator 上，
    于是同一份 body：POST → 422（对），PATCH → 200 + 静默改写（实测）。
    而生产者是真的：淘宝插件在「单价全解析失败」的降级分支下推
    `price_cny=实付` + 全部 `unit_price_cny=None`，未导入的暂存行走整体更新——
    只要实付 < 解析出的邮费（运费券、红包、部分退款），同一批抓取里
    **新单 422 进 failed 桶、老单被静默改成「订单价 = 邮费」**。

    放在这里而不是各个 schema 上，是因为这七个调用点里两个值的来源各不相同
    （有的两个都来自请求体、有的一个来自请求体一个来自库里的现存行），
    只有这里两个值必然同时在手。抛 ValueError 而不是 HTTPException：
    这是业务层校验，`main.py` 有全局兜底把它转成干净的 422（见那里的说明）。"""
    if price_cny is None:
        return None
    postage = postage_cny or 0
    # **判据必须与 `schemas._check_postage_within_total` 同口径**：只有走「种子价路径」
    # （物品都不带单价）时 price_cny 才参与计算，才谈得上「邮费超过总价」。
    # 原先这里是**无条件**比较，而 schema 那边明确跳过带价物品 ⇒ 同一份 body 两处判据打架：
    #     {price_cny: "5.00", postage_cny: "10.00", items: [A×1 @100.00]}
    # 真实订单价 = 100 + 10 = 110，邮费 10 远小于它，却被一个**即将被忽略的字段**否决成 422。
    # `items=None` 表示调用方不知道（例如从库里的行反推），保持检查。
    seeded = items is None or not any(
        getattr(it, "unit_price_cny", None) is not None for it in items)
    if seeded and postage > price_cny:
        raise ValueError(
            f"邮费（{postage}）不能大于订单总价（{price_cny}）——"
            f"订单价 = 商品单价×数量 + 邮费。请先改总价，或把邮费调小。")
    return price_cny - postage


_RESIDUAL_SUFFIX = "（金额尾差）"


def _is_residual(it) -> bool:
    """这一行是不是**上一次折算自己生成的**「金额尾差」占位行。

    尾差行是派生产物，不是用户输入。而前端保存时会把服务端返回的 items 原样回传
    （物品编辑器、订单页展开面板都这样），于是它会作为「一条没有单价的物品」再喂回来
    ——`build_items` 不认识它，当成普通物品重新折算，**又补一条新的尾差行**。
    实测：同一张单每改一次多一条，1 条能涨到 5 条（总价始终守恒，坏的是条数）。

    三个条件同时满足才认，免得误伤用户自己起名叫「…（金额尾差）」的真物品：
    名字后缀 + `auto=True`（用户一编辑就会变 False）+ 数量为 1（生成时固定是 1）。
    即使误判，后果也只是它被重算一遍，总价仍然守恒。
    """
    return (bool(getattr(it, "auto", False))
            and (it.quantity or 1) == 1
            and (getattr(it, "name", "") or "").endswith(_RESIDUAL_SUFFIX))


def build_items(items_in, seed_goods, fallback_name):
    """把「物品输入 + 货款种子价 + 兜底物品名」规整成 ≥1 条物品 dict(name/quantity/unit_price_cny/auto)。

    - seed_goods 是**货款**（已扣掉邮费），不是订单总价：订单价 = Σ(单价×数量) + 邮费，
      种子若含邮费，sync_from_items 会把邮费再加一遍。所有调用点都传 `订单价 - 邮费`。
    - fallback_name 是没有物品明细时拿来当物品名的商品标题（即 Order.title 列，见其命名说明）。

    系统最小单位是物品，订单必须有 ≥1 物品（见 README「物品为最小单位」）：
    - 没给物品 → 自动生成 1 条（name=兜底名、数量 1、单价=货款种子、auto=True 灰显可改）。
    - 给了物品但都没单价、却有货款种子（如爬虫只知订单总价）→ 把货款折成第一条单价(货款/数量)、
      其余置 0，全部 auto=True 待人工拆分复核。
    - 给了带单价的物品 → 原样采用（单价 None→0）；auto 沿用客户端回传（未改动的自动项保持灰）。
    返回的 dict 同时适用 OrderItem 与 StagingItem 构造。"""
    if seed_goods is not None and seed_goods < 0:     # 邮费>总价等异常输入 → 货款夹到 0，绝不落负单价
        seed_goods = Decimal("0.00")
    items_in = list(items_in or [])
    if not items_in:
        return [{"name": (fallback_name or "未命名物品")[:255], "quantity": 1,
                 "unit_price_cny": seed_goods if seed_goods is not None else Decimal("0.00"),
                 "auto": True}]
    any_priced = any(it.unit_price_cny is not None for it in items_in)
    if not any_priced and seed_goods is not None:
        # **剔掉上一轮自己生成的尾差行——只在这一支。**
        # 这一支要按 seed_goods 重新折算，留着它就会被当成「一条没有单价的物品」
        # 再折一遍，于是每保存一次多一条（实测 1 条能涨到 5 条）。
        #
        # ⚠️ 这行过滤**原先放在所有分支之前**，理由写的是「带价那一支也不该把它原样留下」。
        # 那句话是错的，而且代价是真金白银：带价那一支只是原样采用 items，
        # 尾差行被删掉之后**没有任何地方把那笔钱加回去**。而前端的 `toPayload`
        # 恰恰是带着单价整体回传的（物品编辑器改任一格都会触发），于是
        #   建单 ¥100.00 / A×3 → [A@33.33, A（金额尾差）@0.01]
        #   随便改一下物品名再保存 → 尾差行被剔、总价变成 99.99
        # 一次静默缩水，200 OK、无日志，误差上限是 数量×0.01（数量 1000 时 9.99 元）。
        # 而账本金额与爬虫抓到的实付金额从此对不上——正是上面那段 ROUND_DOWN
        # 长注释拼命要守住的那个不变量。
        # 带价那一支把尾差行**当普通物品留着**才是对的：它带着真实的单价，
        # 总价因此守恒；它也不会在那一支里累积（那一支根本不生成新的尾差行）。
        items_in = [it for it in items_in if not _is_residual(it)] or items_in
        out, residual = [], Decimal("0.00")
        for i, it in enumerate(items_in):
            if i == 0:
                q = it.quantity or 1
                # **向下**取整到分，余数单独成行——不能只写 `(seed/q).quantize(HALF_UP)`。
                # 订单价是 Σ(单价×数量) 派生出来的（price_from_items），所以「单价」一旦
                # 被舍入，乘回数量就不再等于种子价，账本金额与爬虫抓到的实付金额对不上：
                #   ¥100.00 / 3  → 33.33 → 回乘 99.99（少 1 分）
                #   ¥  5.00 /1000→  0.01 → 回乘 10.00（**翻一倍**，HALF_UP 向上舍的后果）
                #   ¥  0.40 /1000→  0.00 → 回乘  0.00（**整单金额归零**）
                # 后两种在「一批小商品按件录数量」时是能真实发生的，而且没有任何提示。
                # ROUND_DOWN 保证余数恒为非负且 < 数量×0.01，再补一行把它加回去 ⇒ 总额精确。
                unit = (Decimal(seed_goods) / q).quantize(_CNY_Q, rounding=ROUND_DOWN)
                out.append({"name": it.name, "quantity": it.quantity,
                            "unit_price_cny": unit, "auto": True})
                residual = Decimal(seed_goods) - unit * q
            else:
                out.append({"name": it.name, "quantity": it.quantity,
                            "unit_price_cny": Decimal("0.00"), "auto": True})
        if residual:
            # 只在除不尽时才出现。名字带「尾差」是给人看的：这一分钱落在哪儿必须一目了然，
            # 否则复核的人会以为系统算错了。auto=True → 前端灰显，跟其余待拆分的行同待遇。
            # 名字先按后缀长度截断再拼，保证后缀**一定**在——直接 `f"{name}后缀"[:255]`
            # 在长名字下会把后缀截掉，于是下一次 `_is_residual` 认不出它、又开始叠加。
            head = (items_in[0].name or "")[:255 - len(_RESIDUAL_SUFFIX)]
            out.append({"name": f"{head}{_RESIDUAL_SUFFIX}",
                        "quantity": 1, "unit_price_cny": residual, "auto": True})
        return out
    # 有单价的原样用；没单价的记 0 并标 auto（灰显=待补价），避免误当作真实 ¥0
    return [{"name": it.name, "quantity": it.quantity,
             "unit_price_cny": (it.unit_price_cny if it.unit_price_cny is not None else Decimal("0.00")),
             "auto": (True if it.unit_price_cny is None else bool(getattr(it, "auto", False)))}
            for it in items_in]


def raise_not_found(name: str = "记录"):
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name}不存在")


def raise_conflict():
    """P5：乐观锁冲突 → 409，前端提示刷新。"""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="数据已被他人或机器人修改，请刷新后重试",
    )


def guarded_bump(session: Session, model, obj_id: int, expected_version: int) -> bool:
    """原子地在 DB 层用 `WHERE version=expected` 守卫并自增 version（同时刷新 updated_at）。
    返回 False 表示版本已变（并发/交错写），调用方应抛 409。此 UPDATE 与后续的字段改动
    在同一事务提交，保证并发下不会丢失更新。"""
    conds = [model.id == obj_id, model.version == expected_version]
    if hasattr(model, "is_delete"):                     # 暂存表用硬删、无 is_delete 列，跳过该条件
        conds.append(model.is_delete.is_(False))
    res = session.execute(
        sa_update(model).where(*conds).values(version=model.version + 1, updated_at=utcnow())
    )
    return res.rowcount == 1


def stamp_fx(session: Session, obj) -> None:
    """货款有值却没汇率 → 按这一行的记账日期补一条。

    为什么必须在 **create 和 update 两处都调**：`compute_money()` 缺汇率就算不出
    jpy_auto/jpy_settled，而看板的 `SUM(jpy_settled)` 对 NULL 视而不见——这笔钱会被
    静默吞掉，但笔数照数（「笔数 +1、金额 +0」）。只在 create 补是关不上的：
    全新部署、没装汇率插件、或插件还没成功跑过一次时，FxRate 表是空的，create 只能盖上 None；
    真正闭合缺口的是 update 侧那一刀，因为每次 PATCH 都会重试，顺带还能自愈存量脏行。

    取 `rate_for_date` 而非 `current_rate`：一笔补录的上月支出该按当天牌价折算，
    不该按今天的。staging.py 早就是这个口径，这里向既有惯例收敛。"""
    if obj.price_cny is not None and obj.fx_rate is None:
        from ..services.fx import rate_for_date          # 局部导入避免循环
        obj.fx_rate = rate_for_date(
            session, obj.date, what=f"补 {type(obj).__name__} 的汇率")


def soft_delete(obj) -> None:
    """软删一行：置 is_delete，并像其它写入一样推进乐观锁版本 + updated_at。

    为什么删除也要 bump：删除同样是「对这一行的一次写」。批量软删
    （tags.soft_delete_account_orders）本来就 bump，单条删除不 bump 会让两条路径语义不一，
    也让 updated_at 记不到「什么时候被删的」——数据库层排查/恢复时就少了这条线索。"""
    obj.is_delete = True
    obj.version = (obj.version or 0) + 1
    obj.updated_at = utcnow()


def mirror_to_staging(session: Session, order, built_items) -> None:
    """若此商品单由暂存导入而来：把账本当前的共享字段(+物品)镜像回其暂存行，保持「暂存=账本镜像」。
    否则删单/清账本会把暂存复位为待处理、再导入时用到陈旧的暂存快照，丢掉在订单页做的物品/价格编辑。
    built_items 为 build_items 的产物（非空才镜像物品；None=仅镜像共享字段，如只改了状态）。

    订单页 PATCH 与集运页「内含快递」自动挂靠都会改共享字段，故放 common 供两处共用。
    （原注释说的是「都会改 order.status」，与 shipment.py 里「只挂靠、不动状态」的现行
    行为直接矛盾——那是旧 OCR 自动挂靠时代的遗留说法。）

    字段清单从 `_SHARED_TO_ORDER` 派生：它、`_overlay`、这里，原本是三份手写清单。"""
    from sqlmodel import select

    from ..models import OrderStaging, StagingItem
    from .staging import _SHARED_TO_ORDER

    st = session.exec(
        select(OrderStaging).where(OrderStaging.imported_order_id == order.id)
    ).first()
    if st is None:
        return
    # **改单号时，撞的可能是暂存表的索引，而不是账本的。**
    # 两张表的唯一性契约不一样：账本的活跃唯一键是 `(order_no, COALESCE(platform,''))`
    # ——注释明写「不同来源下允许同号」；而暂存表是 `order_no` **单列**的部分唯一索引
    # （不分平台、不分是否已导入）。镜像会把新单号推进暂存行，于是**账本这边合法的改名，
    # 会被暂存表的索引否决**：用户在订单页把号改成 BBB（纠正 OCR 认错的号），
    # 而 BBB 正被另一条他可能根本没在看的**未导入暂存行**占着 ⇒ IntegrityError
    # ⇒ 全局 handler 转成 409「数据完整性冲突」⇒ 前端把 409 当乐观锁冲突整表重拉，
    # 编辑消失，提示里一个字都没提暂存表。
    # 先查一次说清楚。`staging.py` 里那段注释正好把这种失败模式写成了「不能这么干」的理由，
    # 同一条推理没被应用到这里。
    if order.order_no and st.order_no != order.order_no:
        clash = session.exec(
            select(OrderStaging).where(OrderStaging.order_no == order.order_no,
                                       OrderStaging.id != st.id)
        ).first()
        if clash:
            where = "已导入账本" if clash.imported_order_id else "还没导入"
            raise HTTPException(status_code=409, detail=(
                f"订单号「{order.order_no}」在暂存里已经被另一条记录占着"
                f"（来源 {clash.platform or '未标'}，{where}）。"
                "账本允许不同来源同号，但暂存表的订单号是全表唯一的——"
                "请先去暂存页处理掉那一条，或换一个号。"))
    for staging_field, order_field in _SHARED_TO_ORDER.items():
        setattr(st, staging_field, getattr(order, order_field))
    if built_items is not None:
        st.items = [StagingItem(**d) for d in built_items]
    # **价从账本单镜像过来，不要拿暂存自己那份 items 重算。**
    # 原先无条件调 `st.sync_from_items()`，而 `built_items is None` 的意思恰恰是
    # 「这次没动物品」。暂存行**没有物品**时（`import_staging` 的 0 物品分支明确会产出
    # 这种状态，`tools/backfill_item_price.py` 整个工具也是为它写的），
    # 重算出来的就是 `0 + 邮费` ⇒ 暂存行的金额被静默改写成 0。
    #
    # 实测过的完整链路：暂存 ¥300 / 0 物品 → 导入得到 ¥300、6000 円 → 给订单随手加个备注
    # （任何一次 PATCH 都会走到这里）⇒ 暂存行变 ¥0.00 → 在订单页删掉该单
    # （`delete_order` 把暂存复位成「待处理」）→ 再点一次「导入账本」
    # ⇒ 建出一张 **¥0.00 / 0 円** 的订单，看板合计随之静默缩水。
    # 导入期间界面还看不出来——`_overlay` 用账本值覆盖显示。
    #
    # 镜像账本价在两种情形下都对：物品被镜像过来时，`Σ(items)+邮费` 本来就等于账本价；
    # 没镜像时，账本价才是唯一可信的那个数。这也正是本函数的定位——「暂存 = 账本镜像」。
    st.price_cny = guard_cny(order.price_cny) if order.price_cny is not None else None
    st.updated_at = utcnow()
    st.version = st.version + 1   # 镜像也算一次对暂存行的写：必须自增乐观锁版本，
    #                              否则暂存页拿旧 version 保存不会 409，会用陈旧表单悄悄覆盖镜像值。
    session.add(st)


async def run_ocr(file: UploadFile, recognizer: Callable[[bytes], dict]) -> dict:
    """校验上传的截图并在线程池里跑 recognizer（商品订单/集运订单两条 OCR 路由共用）。

    OCR 为 CPU 密集且较慢（首次还要加载模型），放线程池 → 不阻塞事件循环，前端可连续上传；
    真正的串行化在 services/ocr.py 的 _infer_lock（RapidOCR 引擎非保证可重入）。

    **并发上限单独设一个 CapacityLimiter，不复用默认线程池的 40 个令牌。**
    `_infer_lock` 只串行化「推理」那一段，**解码在锁外**——40 路并发上传就是 40 份
    解码后的位图同时在内存里（实测两路 8000×8000 就 +1.2GB，线性叠加）。
    限流器把同时在解码/推理的请求压到 _OCR_CONCURRENCY 路，内存尖峰随之封顶。
    写法要注意：`run_in_threadpool(fn, data, limiter=...)` 会把 limiter 当成 fn 的
    关键字参数传下去（TypeError）；必须走 `anyio.to_thread.run_sync` + partial。
    也**不要**去替换 `run_in_threadpool` 这个名字——`contextmanager_in_threadpool`
    也走它，换掉会连带劫持所有生成器依赖。
    """
    import functools

    import anyio.to_thread

    from ..services.ocr import OcrUnavailable

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="请上传图片文件")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(data) > MAX_OCR_BYTES:
        raise HTTPException(status_code=413, detail="图片过大（上限 10MB）")
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(recognizer, data), limiter=_ocr_limiter())
    except OcrUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MemoryError:
        # 像素闸之下仍可能撞上（多路并发、机器本来就紧）。漏成裸 500 的话前端只会说
        # 「服务器错误」，用户不知道这是「稍后重试就好」而不是「这张图坏了」。
        log.warning("OCR 内存不足：%s bytes", len(data))
        raise HTTPException(status_code=503, detail="服务器内存不足，请稍后重试或换一张更小的截图",
                            headers={"Retry-After": "10"})


def list_totals(session: Session, model, conds: list) -> dict:
    """列表页脚要的三个数：条数 / 日元合计 / 有钱但没折算的行数。

    **一条 SQL 全算完**，替换掉原先那条只数条数的查询——页脚不该让列表接口多打一次库。

    `sum_jpy` 求的是**当前筛选出的全部行**，不是屏幕上那 50 行：翻页时页脚跟着变的话
    这个数没有任何用处。

    `unconverted` 必须一起给：`SUM` 对 NULL 视而不见，缺汇率的行会让合计静默变小
    而条数照旧——看板那边为同一件事专门有个 `_uncounted`，页脚不能重蹈。

    **刻意不套 `ledger_exclusions()`**：这是「你正在看的这些行加起来多少」，
    不是看板的「你花了多少」。用户筛出退款单时，页脚报 0 才是撒谎。
    两者口径不同是有意的，所以页脚的措辞是「筛选合计」而不是「支出」。
    """
    from sqlalchemy import case, func
    from sqlmodel import select

    n, total_jpy, missing = session.exec(
        select(
            func.count(),
            func.coalesce(func.sum(model.jpy_settled), 0),
            # 判据走 `models.base.unconverted_clause`——与看板、集运到岸同一份规则。
            # 这三处历史上分叉过两次，每次都是漏抄 `!= 0`（显式填 0 的行折算过去也是 0 円，
            # 没有任何金额会被 SUM 吞掉，报出来只是噪音）。
            func.coalesce(func.sum(case((unconverted_clause(model), 1), else_=0)), 0),
        ).select_from(model).where(*conds)
    ).one()
    return {"total": int(n), "sum_jpy": int(total_jpy), "unconverted": int(missing)}
