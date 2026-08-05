"""Shared router helpers: optimistic lock (DB-level guard), soft delete, errors, item building,
OCR 截图上传（校验 + 线程池执行）。"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import update as sa_update
from sqlmodel import Session

from ..models import utcnow

_CNY_Q = Decimal("0.01")     # 人民币量化到分
MAX_OCR_BYTES = 10 * 1024 * 1024      # 截图上限 10MB（手机截图通常 < 2MB）


def goods_seed(price_cny, postage_cny):
    """「订单价种子」→「货款种子」：扣掉邮费。没给价就没有种子（None）。

    订单价 = Σ(单价×数量) + 邮费，而 sync_from_items 事后会自己再加一次邮费；所以喂给
    build_items 的种子必须是**已扣邮费**的货款，否则邮费被算两遍。这个换算在建单/改单/
    导入/暂存共 6 处出现过，各写一份就迟早有一处忘了减——收敛到这里。"""
    return None if price_cny is None else price_cny - (postage_cny or 0)


def build_items(items_in, seed_goods, fallback_name):
    """把「物品输入 + 货款种子价 + 兜底物品名」规整成 ≥1 条物品 dict(name/quantity/price_cny/auto)。

    - seed_goods 是**货款**（已扣掉邮费），不是订单总价：订单价 = Σ(单价×数量) + 邮费，
      种子若含邮费，sync_from_items 会把邮费再加一遍。所有调用点都传 `订单价 - 邮费`。
    - fallback_name 是没有物品明细时拿来当物品名的商品标题（即 Order.shop 列，见其命名说明）。

    系统最小单位是物品，订单必须有 ≥1 物品（见 README「物品为最小单位」）：
    - 没给物品 → 自动生成 1 条（name=兜底名、数量 1、单价=货款种子、auto=True 灰显可改）。
    - 给了物品但都没单价、却有货款种子（如爬虫只知订单总价）→ 把货款折成第一条单价(货款/数量)、
      其余置 0，全部 auto=True 待人工拆分复核。
    - 给了带单价的物品 → 原样采用（单价 None→0）；auto 沿用客户端回传（未改动的自动项保持灰）。
    返回的 dict 同时适用 OrderItem 与 StagingItem 构造。"""
    if seed_goods is not None and seed_goods < 0:     # 邮费>总价等异常输入 → 货款夹到 0，绝不落负单价
        seed_goods = Decimal("0.00")
    if not items_in:
        return [{"name": (fallback_name or "未命名物品")[:255], "quantity": 1,
                 "price_cny": seed_goods if seed_goods is not None else Decimal("0.00"), "auto": True}]
    any_priced = any(it.price_cny is not None for it in items_in)
    if not any_priced and seed_goods is not None:
        out = []
        for i, it in enumerate(items_in):
            if i == 0:
                q = it.quantity or 1
                unit = (Decimal(seed_goods) / q).quantize(_CNY_Q, rounding=ROUND_HALF_UP)
                out.append({"name": it.name, "quantity": it.quantity, "price_cny": unit, "auto": True})
            else:
                out.append({"name": it.name, "quantity": it.quantity, "price_cny": Decimal("0.00"), "auto": True})
        return out
    # 有单价的原样用；没单价的记 0 并标 auto（灰显=待补价），避免误当作真实 ¥0
    return [{"name": it.name, "quantity": it.quantity,
             "price_cny": (it.price_cny if it.price_cny is not None else Decimal("0.00")),
             "auto": (True if it.price_cny is None else bool(getattr(it, "auto", False)))}
            for it in items_in]


def not_found(name: str = "记录"):
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name}不存在")


def conflict():
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

    订单页 PATCH 与集运页「内含快递」自动挂靠都会改 order.status，故放 common 供两处共用。"""
    from sqlmodel import select

    from ..models import OrderStaging, StagingItem

    st = session.exec(
        select(OrderStaging).where(OrderStaging.imported_order_id == order.id)
    ).first()
    if st is None:
        return
    st.order_date, st.order_no, st.shop = order.date, order.order_no, order.shop
    st.platform_account, st.platform, st.express_no = order.platform_account, order.platform, order.express_no
    st.postage_cny, st.fx_rate, st.order_status = order.postage_cny, order.fx_rate, order.status
    if built_items is not None:
        st.items = [StagingItem(**d) for d in built_items]
    st.sync_from_items()
    st.updated_at = utcnow()
    st.version = st.version + 1   # 镜像也算一次对暂存行的写：必须自增乐观锁版本，
    #                              否则暂存页拿旧 version 保存不会 409，会用陈旧表单悄悄覆盖镜像值。
    session.add(st)


async def run_ocr(file: UploadFile, recognizer: Callable[[bytes], dict]) -> dict:
    """校验上传的截图并在线程池里跑 recognizer（商品订单/集运订单两条 OCR 路由共用）。

    OCR 为 CPU 密集且较慢（首次还要加载模型），放线程池 → 不阻塞事件循环，前端可连续上传；
    真正的串行化在 services/ocr.py 的 _infer_lock（RapidOCR 引擎非保证可重入）。"""
    from fastapi.concurrency import run_in_threadpool

    from ..services.ocr import OcrUnavailable

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="请上传图片文件")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(data) > MAX_OCR_BYTES:
        raise HTTPException(status_code=413, detail="图片过大（上限 10MB）")
    try:
        return await run_in_threadpool(recognizer, data)
    except OcrUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
