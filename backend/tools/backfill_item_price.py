"""一次性回填：把「物品为最小单位」落到既有数据（幂等，可重复跑）。

规则**直接调用** `routers/common.build_items`，不在这里重写一份：
- 订单/暂存 0 物品 → 自动生成 1 条（name=商品名、数量1、单价=订单价、auto=True 灰显待复核）。
- 有物品但全部无单价（老数据）→ 把订单价折成第一条单价(订单价/数量)、其余 0，全部 auto=True；
  除不尽的余数单独成一条「（金额尾差）」，保证 Σ(单价×数量) 与原订单价**分毫不差**。
- 已有带单价的物品 → 跳过（幂等）。

这里曾经抄过一份同规则的实现，靠注释「对齐」。抄本与正本必然发散——`build_items`
把取整方式从 HALF_UP 改成 ROUND_DOWN+尾差行的那天，抄本还在按老规则回填，
同一批数据经两条路径会得到不同的总价。规则只能有一份。

用法：
    cd backend && .venv/bin/python -m tools.backfill_item_price          # 回填当前生效后端
    cd backend && .venv/bin/python -m tools.backfill_item_price --control # 额外回填本地 SQLite 控制库
不删任何数据；只补物品与单价。
"""
from __future__ import annotations

import sys
from decimal import Decimal

from sqlmodel import Session, select

from app.models import OrderItem, StagingItem, Order, OrderStaging
from app.models.base import items_carry_no_price
from app.routers.common import build_items

_Q = Decimal("0.01")


def _backfill_row(obj, item_cls) -> dict:
    """回填单个订单/暂存行。返回 {action, shift}。"""
    items = list(obj.items)
    # 种子一律用「货款」= 订单价 - 邮费：sync_from_items 会再加一次邮费，若种子含邮费则重复计
    # （对齐 routers/common.build_items 各站点的 seed_goods 口径）。夹到 ≥0，绝不落负单价。
    goods = Decimal(obj.price_cny or 0) - Decimal(obj.postage_cny or 0)
    if goods < 0:
        goods = Decimal("0.00")
    if not items:
        # price 用 0.00 而非 None（订单价 NULL 时）→ 二次运行时该物品已有价，走 skip，保证幂等
        seed = goods if obj.price_cny is not None else Decimal("0.00")
        obj.items = [item_cls(**d) for d in build_items([], seed, obj.title)]
        action = "auto_item"
    elif all(it.unit_price_cny is None for it in items):
        obj.items = [item_cls(**d) for d in build_items(items, goods, obj.title)]
        action = "priced_items"
    else:
        return {"action": "skip", "shift": Decimal("0")}   # 已迁移，幂等跳过
    # 两条路径都**精确**保总价：数量=1 时单价即货款原值；数量>1 时 build_items 用
    # ROUND_DOWN + 尾差行把余数补回去。所以容差是 0，任何偏移都说明有 bug。
    tolerance = Decimal("0")

    # **第一道校验：造出来的物品必须真的带上了单价。**
    # 原先只有下面那道 shift 校验，而它是**间接**的——靠「单价落 NULL ⇒ sync_from_items
    # 把总价重算成只剩邮费 ⇒ 偏移非零」这条链子来发现拼错的字段名。
    # 2026-08-23 那条链子被剪断了：`sync_from_items` 现在遇到「单价全 NULL」直接不动价
    # （见 `items_carry_no_price`——那是为历史订单被 PATCH 时静默清零修的），
    # 于是 shift 恒为 0，这道检测**静默失灵**，回填反而会把一批无价物品写进库。
    # 直接问「物品有没有价」比观察副作用可靠：它不依赖任何下游行为，也早一步。
    if items_carry_no_price(obj.items):
        raise RuntimeError(
            f"回填 {type(obj).__name__}#{obj.id} 造出的物品一条单价都没有"
            f"（{len(obj.items)} 条）。多半是构造时字段名拼错被 SQLModel 静默丢弃了"
            f"——table=True 关掉了 Pydantic 校验。已中止，未写入任何数据。")

    old = Decimal(obj.price_cny or 0)
    obj.sync_from_items()                                   # 订单同时重算日元；暂存只重算 price_cny
    shift = Decimal(obj.price_cny or 0) - old
    # 事后校验：SQLModel(table=True) 关掉了 Pydantic 校验，构造时传错的字段名会被**静默丢弃**，
    # 单价落 NULL → sync_from_items 把总价重算成「只剩邮费」。这里曾因 unit_price_cny 拼成
    # unit_unit_price_cny 而把历史订单的货款整额清零，且二次运行会把 0 固化。
    # 回填只该产生「取整」级别的分位误差，出现更大的偏移一律视为 bug，立即中止而不是写库。
    if abs(shift) > tolerance:
        raise RuntimeError(
            f"回填 {type(obj).__name__}#{obj.id} 使总价从 {old} 变成 {obj.price_cny}"
            f"（偏移 {shift}，容差 {tolerance}），远超取整误差。已中止，未写入任何数据。"
        )
    return {"action": action, "shift": shift}


def backfill(engine) -> dict:
    rep = {"orders": {"auto_item": 0, "priced_items": 0, "skip": 0},
           "staging": {"auto_item": 0, "priced_items": 0, "skip": 0},
           "shifts": []}
    with Session(engine) as s:
        for obj in s.exec(select(Order)).all():       # 含软删，保证每单都有物品
            r = _backfill_row(obj, OrderItem)
            rep["orders"][r["action"]] += 1
            if r["shift"]:
                rep["shifts"].append(("order", obj.id, str(r["shift"])))
        for obj in s.exec(select(OrderStaging)).all():
            r = _backfill_row(obj, StagingItem)
            rep["staging"][r["action"]] += 1
            if r["shift"]:
                rep["shifts"].append(("staging", obj.id, str(r["shift"])))
        s.commit()
    return rep


def main() -> None:
    from app.database import control_engine, current_backend, get_engine

    print(f"当前生效后端: {current_backend()}")
    rep = backfill(get_engine())
    print("数据引擎回填:", rep["orders"], "| staging:", rep["staging"])
    for kind, oid, shift in rep["shifts"]:
        print(f"  微调 {kind}#{oid}: 总价 {'+' if not shift.startswith('-') else ''}{shift} 元（单价取整）")
    if "--control" in sys.argv and control_engine() is not get_engine():
        rep2 = backfill(control_engine())
        print("控制库(SQLite)回填:", rep2["orders"], "| staging:", rep2["staging"])


if __name__ == "__main__":
    main()
