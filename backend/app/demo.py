"""灌入演示数据。运行：python -m app.demo

会建 admin（若无，走 seed.ensure_admin，认 SOROBAN_ADMIN_*）+ 一批真实感的代购/集运数据：
集运订单、淘宝订单（含一单多物、退款、日元直付、已取消示例）、杂项、暂存页的待导入订单。

**只在库里四张业务表都空时才灌**，且当前后端不是本地 SQLite 时必须显式
`SOROBAN_DEMO_YES=1` —— 它写的是**当前生效的数据库**，那可能就是你的真实账本。
"""

import datetime as dt
import json
import os
from decimal import Decimal

from sqlmodel import Session, select

from . import single_process
from .database import control_url, create_db_and_tables, current_backend, get_engine
from .seed import ensure_admin
from .models import (
    ColumnLayout, FxRate, ShipmentOrder, MiscExpense, OrderItem, StagingItem, TagOption,
    Order, OrderStaging,
)

# 列布局默认：顺序 + 统一列宽（≈ 刚好显示日期，取整多留一点 = 110）。demo 注入库，reset 后即此默认序。
COL_W = 110
COL_LAYOUTS = {
    "staging": ["order_date", "platform_account", "title", "price_cny", "purchase_status",
                "items", "order_no", "express_no", "scraped_at", "fx_rate", "import_status"],
    "orders": ["date", "platform_account", "title", "items", "purchase_status", "shipment_order_id",
               "jpy_settled", "jpy_override", "price_cny", "fx_rate", "express_no", "order_no"],
}

D = lambda y, m, d: dt.date(y, m, d)  # noqa: E731


def main() -> None:
    """灌入演示数据。**写的是当前生效的后端**——可能就是用户的 MySQL 账本。

    这个脚本没有 HTTP 入口、也不在 PyInstaller 的导入图里（打包版根本没有它），
    所以只在源码模式下可达。但它原先的唯一一道闸是「已有 Order 行就跳过」——
    只记了集运单与杂项的新用户会被放行，然后往真库里灌 3 集运 / 9 订单 / 4 杂项 / 4 暂存，
    全程没有确认、也**不说自己在写哪个库**。
    """
    # **闸必须排在任何一次动库之前。** 第一版把它放在 `create_db_and_tables()` 之后，
    # 而那一句做的是「对**当前生效的数据后端**跑完整条 alembic upgrade」——
    # MySQL 后端的用户跑一次 `python -m app.demo`，生产库先被跑完整链迁移，
    # **然后**才打印「已中止」。`current_backend()` 只是比较两个模块级引擎
    # （import 期就解析好了），建库之前调它是安全的。
    backend = current_backend()
    print(f"[demo] 即将写入当前生效的数据库：{backend}")
    if backend != "sqlite" and os.environ.get("SOROBAN_DEMO_YES") != "1":
        # 远程库多半是真账本。要往它写，必须显式说一声。
        print("[demo] 当前后端不是本地 SQLite —— 这多半是你的真实账本。"
              "确认要往它灌演示数据，请设 SOROBAN_DEMO_YES=1 再跑。已中止。")
        return

    # **也要拿单进程闸**：这一路同样会跑完整条迁移，而 soroban 正开着时就是
    # 「两个进程同时 ALTER 同一个库」——正是把建库从启动器搬进 lifespan 要消灭的那件事。
    try:
        single_process.acquire(control_url())
    except single_process.MultipleInstances:
        print("[demo] soroban 正在运行 —— 先关掉它再灌演示数据"
              "（这个脚本会跑迁移，不能和正在用库的进程同时动手）。已中止。")
        return

    create_db_and_tables()
    with Session(get_engine()) as s:
        # 闸只看商品订单是不够的：只记了集运单/杂项的新用户会被放行。
        # **它必须排在建号之前**：第一版把 `ensure_admin()` 写在了前面，
        # 于是一本已经在用的账本会先凭空多出一个管理员，紧接着才打印
        # 「跳过演示数据灌入（不覆盖任何现有数据）」——那句话当场就是假的。
        for _model, _label in ((Order, "商品订单"), (ShipmentOrder, "集运订单"),
                               (MiscExpense, "杂项支出"), (OrderStaging, "暂存订单")):
            if s.exec(select(_model)).first():
                print(f"[demo] 库里已经有{_label}，跳过演示数据灌入（不覆盖任何现有数据）。")
                return

        # 建号交给 `seed.ensure_admin()`：它认 SOROBAN_ADMIN_USER/PASS。
        # 原先这里硬写 `admin`/`admin123`，于是改过账号名的用户会在自己的真库里
        # 悄悄多出一个用公开默认口令的管理员。
        ensure_admin()

        # 汇率（供导入/预填）——已有当日汇率就不重复插入
        if not s.exec(select(FxRate).where(FxRate.date == D(2026, 7, 9))).first():
            s.add(FxRate(date=D(2026, 7, 9), rate=Decimal("23.8642")))

        # 标签选项（列头可管理的下拉集：淘宝账号 / 集运收货人）
        # 已存在的标签跳过：`TagOption` 有 `(field, value)` 唯一索引，
        # 无条件 add 会让下面第一个 commit 直接 IntegrityError 崩掉。
        for _field, _vals in (("platform_account", ["acctA", "acctB"]),
                              ("recipient", ["本人", "家人", "朋友"])):
            for _v in _vals:
                if not s.exec(select(TagOption).where(
                        TagOption.field == _field, TagOption.value == _v)).first():
                    s.add(TagOption(field=_field, value=_v))

        # —— 集运订单 ——
        jf1 = ShipmentOrder(date=D(2026, 6, 5), shipment_no="JF-2606A", weight=Decimal("4.5"),
                           intl_tracking_no="LP00612345678", shipment_status="已签收",
                           price_cny=Decimal("180"), fx_rate=Decimal("20.5"), special_fee_jpy=1200,
                           note="含关税消费税", recipient="本人")
        jf2 = ShipmentOrder(date=D(2026, 6, 20), shipment_no="JF-2606B", weight=Decimal("2.1"),
                           intl_tracking_no="LP00612399999", shipment_status="已发出",
                           price_cny=Decimal("95"), fx_rate=Decimal("21"), recipient="家人")
        jf3 = ShipmentOrder(date=D(2026, 7, 5), shipment_no="JF-2607A", shipment_status="打包中")
        for j in (jf1, jf2, jf3):
            j.compute_money()
            s.add(j)
        s.commit()
        for j in (jf1, jf2, jf3):
            s.refresh(j)

        # —— 商品订单（date, order_no, title, account, express, price, rate, purchase_status, jf, items, override）——
        orders = [
            dict(date=D(2026, 5, 28), order_no="TB250528001", title="谷子屋", platform_account="acctA",
                 express_no="SF1001", price_cny="320", fx_rate="20.5", purchase_status="已签收", jf=jf1.id,
                 items=[("初音未来 手办", 1)]),
            dict(date=D(2026, 5, 30), order_no="TB250530007", title="万代官方旗舰店", platform_account="acctA",
                 express_no="SF1002", price_cny="460", fx_rate="20.5", purchase_status="已签收", jf=jf1.id,
                 items=[("MG 高达模型", 2)]),
            dict(date=D(2026, 6, 2), order_no="TB250602013", title="痛包周边专营", platform_account="acctA",
                 express_no="YT2003", price_cny="88", fx_rate="20.8", purchase_status="已签收", jf=jf1.id,
                 items=[("亚克力立牌", 3), ("金属徽章", 5)]),
            dict(date=D(2026, 6, 18), order_no="TB250618022", title="二次元周边店", platform_account="acctB",
                 express_no="ZT3004", price_cny="55", fx_rate="21", purchase_status="待收货", jf=jf2.id,
                 items=[("角色抱枕套", 1)]),
            dict(date=D(2026, 6, 19), order_no="TB250619031", title="手办工房", platform_account="acctB",
                 express_no="ZT3005", price_cny="130", fx_rate="21", purchase_status="待收货", jf=jf2.id,
                 items=[("景品手办", 1)]),
            dict(date=D(2026, 7, 3), order_no="TB250703044", title="谷子屋", platform_account="acctA",
                 express_no="SF1006", price_cny="60", fx_rate="23.86", purchase_status="待发货", jf=jf3.id,
                 items=[("吧唧/徽章", 10)]),
            # 退款：打退款标记，金额/物品照显，但不计入合计（不再用负数冲抵）
            dict(date=D(2026, 7, 4), order_no="TB250704050", title="挂件小铺", platform_account="acctA",
                 price_cny="25", fx_rate="23.86", purchase_status="退款", items=[("亚克力挂件", 1)]),
            # 日元直付（只填覆盖日元）
            dict(date=D(2026, 7, 6), order_no="TB250706061", title="日亚代付", platform_account="acctB",
                 override=3500, purchase_status="待发货", items=[("日亚补款", 1)]),
            # 交易关闭（不计入看板）
            dict(date=D(2026, 7, 7), order_no="TB250707070", title="测试店", platform_account="acctA",
                 price_cny="200", fx_rate="23", purchase_status="交易关闭", items=[("已关闭的订单", 1)]),
        ]
        for t in orders:
            o = Order(
                date=t["date"], order_no=t["order_no"], title=t["title"], platform_account=t["platform_account"],
                express_no=t.get("express_no"), purchase_status=t["purchase_status"], shipment_order_id=t.get("jf"),
                price_cny=Decimal(t["price_cny"]) if "price_cny" in t else None,
                fx_rate=Decimal(t["fx_rate"]) if "fx_rate" in t else None,
                jpy_override=t.get("override"),
            )
            o.compute_money()
            o.items = [OrderItem(name=n, quantity=q) for n, q in t.get("items", [])]
            s.add(o)

        # —— 杂项 ——
        misc = [
            dict(date=D(2026, 6, 5), name="国际运费差价补款", price_cny="120", fx_rate="20.5", category="运费"),
            dict(date=D(2026, 6, 21), name="打包气泡膜", price_cny="30", fx_rate="21", category="包材"),
            dict(date=D(2026, 7, 1), name="煤炉出品手续费", override=800, category="手续费"),
            dict(date=D(2026, 7, 8), name="关税补缴", override=650, category="税费"),
        ]
        for m in misc:
            e = MiscExpense(
                date=m["date"], name=m["name"], category=m.get("category"),
                price_cny=Decimal(m["price_cny"]) if "price_cny" in m else None,
                fx_rate=Decimal(m["fx_rate"]) if "fx_rate" in m else None,
                jpy_override=m.get("override"),
            )
            e.compute_money()
            s.add(e)

        # —— 暂存（待处理，演示「导入 / 忽略」；含一单多物）——
        staging = [
            dict(order_no="TB250708081", platform_account="acctA", title="谷子屋", purchase_status="待发货",
                 price_cny="45", order_date=D(2026, 7, 8), items=[("色纸", 2), ("明信片套装", 1)]),
            dict(order_no="TB250708090", platform_account="acctA", title="手办工房", purchase_status="待收货",
                 price_cny="150", order_date=D(2026, 7, 8), items=[("景品公仔", 1)]),
            dict(order_no="TB250707100", platform_account="acctB", title="日用百货", purchase_status="已签收",
                 price_cny="39", order_date=D(2026, 7, 7), items=[("洗发水(非集运)", 1)]),
            dict(order_no="TB250709110", platform_account="acctA", title="画集屋", purchase_status="待付款",
                 price_cny="78", order_date=D(2026, 7, 9), items=[("设定集", 1), ("A3 海报", 2)]),
        ]
        for st in staging:
            items = st.pop("items")
            row = OrderStaging(
                price_cny=Decimal(st.pop("price_cny")), fx_rate=Decimal("23.86"), **st
            )
            row.items = [StagingItem(name=n, quantity=q) for n, q in items]
            s.add(row)

        # —— 列布局默认（顺序 + 统一列宽）——
        for _t, _keys in COL_LAYOUTS.items():
            s.add(ColumnLayout(
                table_name=_t,
                columns_json=json.dumps([{"key": k, "width": COL_W} for k in _keys], ensure_ascii=False),
            ))

        s.commit()
        print("演示数据已灌入：3 集运 / 9 淘宝 / 4 杂项 / 4 暂存待导入 / 2 列布局。")


if __name__ == "__main__":
    main()
