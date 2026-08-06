"""一次性回填：把「物品为最小单位」落到既有数据（幂等，可重复跑）。

规则（对齐 routers/common.build_items）：
- 订单/暂存 0 物品 → 自动生成 1 条（name=商品名、数量1、单价=订单价、auto=True 灰显待复核）。
- 有物品但全部无单价（老数据）→ 把订单价折成第一条单价(订单价/数量)、其余 0，全部 auto=True。
  数量>1 时单价取整到分，订单价按 Σ(单价×数量) 重算，可能与原值差几分（会在报告里列出）。
- 已有带单价的物品 → 跳过（幂等）。

用法：
    cd backend && .venv/bin/python -m tools.backfill_item_price          # 回填当前生效后端
    cd backend && .venv/bin/python -m tools.backfill_item_price --control # 额外回填本地 SQLite 控制库
不删任何数据；只补物品与单价。
"""
from __future__ import annotations

import sys
from decimal import ROUND_HALF_UP, Decimal

from sqlmodel import Session, select

from app.models import OrderItem, StagingItem, Order, OrderStaging

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
        obj.items = [item_cls(name=(obj.title or "未命名物品")[:255], quantity=1,
                              unit_unit_price_cny=seed, auto=True)]
        action = "auto_item"
    elif all(it.unit_price_cny is None for it in items):
        total = goods
        for i, it in enumerate(items):
            if i == 0:
                q = it.quantity or 1
                it.unit_price_cny = (total / q).quantize(_Q, rounding=ROUND_HALF_UP)
            else:
                it.unit_price_cny = Decimal("0.00")
            it.auto = True
        action = "priced_items"
    else:
        return {"action": "skip", "shift": Decimal("0")}   # 已迁移，幂等跳过

    old = Decimal(obj.price_cny or 0)
    obj.sync_from_items()                                   # 订单同时重算日元；暂存只重算 price_cny
    return {"action": action, "shift": Decimal(obj.price_cny or 0) - old}


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
