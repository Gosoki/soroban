"""灌入演示数据。运行：python -m app.demo

会建 admin（若无）+ 一批真实感的代购/集运数据：集运订单、淘宝订单（含一单多物、
退款、日元直付、已取消示例）、杂项、以及暂存页的待导入订单。已有淘宝数据则跳过。
"""

import datetime as dt
import json
from decimal import Decimal

from sqlmodel import Session, select

from .auth import hash_password
from .database import create_db_and_tables, get_engine
from .models import (
    ColumnLayout, FxRate, ShipmentOrder, MiscExpense, OrderItem, StagingItem, TagOption,
    Order, OrderStaging, User,
)

# 列布局默认：顺序 + 统一列宽（≈ 刚好显示日期，取整多留一点 = 110）。demo 注入库，reset 后即此默认序。
COL_W = 110
COL_LAYOUTS = {
    "staging": ["order_date", "platform_account", "shop", "price_cny", "order_status",
                "items", "order_no", "express_no", "scraped_at", "fx_rate", "status"],
    "orders": ["date", "platform_account", "shop", "items", "status", "shipment_order_id",
               "jpy_settled", "jpy_override", "price_cny", "fx_rate", "express_no", "order_no"],
}

D = lambda y, m, d: dt.date(y, m, d)  # noqa: E731


def main() -> None:
    create_db_and_tables()
    with Session(get_engine()) as s:
        if not s.exec(select(User).where(User.username == "admin")).first():
            s.add(User(username="admin", password_hash=hash_password("admin123"), display_name="管理员"))
            s.commit()

        if s.exec(select(Order)).first():
            print("已有淘宝数据，跳过演示数据灌入。")
            return

        # 汇率（供导入/预填）——已有当日汇率就不重复插入
        if not s.exec(select(FxRate).where(FxRate.date == D(2026, 7, 9))).first():
            s.add(FxRate(date=D(2026, 7, 9), rate=Decimal("23.8642")))

        # 标签选项（列头可管理的下拉集：淘宝账号 / 集运收货人）
        for _field, _vals in (("platform_account", ["acctA", "acctB"]),
                              ("recipient", ["本人", "家人", "朋友"])):
            for _v in _vals:
                s.add(TagOption(field=_field, value=_v))

        # —— 集运订单 ——
        jf1 = ShipmentOrder(date=D(2026, 6, 5), shipment_no="JF-2606A", weight=Decimal("4.5"),
                           intl_tracking_no="LP00612345678", status="已签收",
                           price_cny=Decimal("180"), fx_rate=Decimal("20.5"), special_fee_jpy=1200,
                           note="含关税消费税", recipient="本人")
        jf2 = ShipmentOrder(date=D(2026, 6, 20), shipment_no="JF-2606B", weight=Decimal("2.1"),
                           intl_tracking_no="LP00612399999", status="已发出",
                           price_cny=Decimal("95"), fx_rate=Decimal("21"), recipient="家人")
        jf3 = ShipmentOrder(date=D(2026, 7, 5), shipment_no="JF-2607A", status="打包中")
        for j in (jf1, jf2, jf3):
            j.compute_money()
            s.add(j)
        s.commit()
        for j in (jf1, jf2, jf3):
            s.refresh(j)

        # —— 商品订单（date, order_no, shop, account, express, price, rate, status, jf, items, override）——
        orders = [
            dict(date=D(2026, 5, 28), order_no="TB250528001", shop="谷子屋", platform_account="acctA",
                 express_no="SF1001", price_cny="320", fx_rate="20.5", status="已签收", jf=jf1.id,
                 items=[("初音未来 手办", 1)]),
            dict(date=D(2026, 5, 30), order_no="TB250530007", shop="万代官方旗舰店", platform_account="acctA",
                 express_no="SF1002", price_cny="460", fx_rate="20.5", status="已签收", jf=jf1.id,
                 items=[("MG 高达模型", 2)]),
            dict(date=D(2026, 6, 2), order_no="TB250602013", shop="痛包周边专营", platform_account="acctA",
                 express_no="YT2003", price_cny="88", fx_rate="20.8", status="已签收", jf=jf1.id,
                 items=[("亚克力立牌", 3), ("金属徽章", 5)]),
            dict(date=D(2026, 6, 18), order_no="TB250618022", shop="二次元周边店", platform_account="acctB",
                 express_no="ZT3004", price_cny="55", fx_rate="21", status="待收货", jf=jf2.id,
                 items=[("角色抱枕套", 1)]),
            dict(date=D(2026, 6, 19), order_no="TB250619031", shop="手办工房", platform_account="acctB",
                 express_no="ZT3005", price_cny="130", fx_rate="21", status="待收货", jf=jf2.id,
                 items=[("景品手办", 1)]),
            dict(date=D(2026, 7, 3), order_no="TB250703044", shop="谷子屋", platform_account="acctA",
                 express_no="SF1006", price_cny="60", fx_rate="23.86", status="待发货", jf=jf3.id,
                 items=[("吧唧/徽章", 10)]),
            # 退款：打退款标记，金额/物品照显，但不计入合计（不再用负数冲抵）
            dict(date=D(2026, 7, 4), order_no="TB250704050", shop="挂件小铺", platform_account="acctA",
                 price_cny="25", fx_rate="23.86", status="退款", items=[("亚克力挂件", 1)]),
            # 日元直付（只填覆盖日元）
            dict(date=D(2026, 7, 6), order_no="TB250706061", shop="日亚代付", platform_account="acctB",
                 override=3500, status="待发货", items=[("日亚补款", 1)]),
            # 交易关闭（不计入看板）
            dict(date=D(2026, 7, 7), order_no="TB250707070", shop="测试店", platform_account="acctA",
                 price_cny="200", fx_rate="23", status="交易关闭", items=[("已关闭的订单", 1)]),
        ]
        for t in orders:
            o = Order(
                date=t["date"], order_no=t["order_no"], shop=t["shop"], platform_account=t["platform_account"],
                express_no=t.get("express_no"), status=t["status"], shipment_order_id=t.get("jf"),
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
            dict(order_no="TB250708081", platform_account="acctA", shop="谷子屋", order_status="待发货",
                 price_cny="45", order_date=D(2026, 7, 8), items=[("色纸", 2), ("明信片套装", 1)]),
            dict(order_no="TB250708090", platform_account="acctA", shop="手办工房", order_status="待收货",
                 price_cny="150", order_date=D(2026, 7, 8), items=[("景品公仔", 1)]),
            dict(order_no="TB250707100", platform_account="acctB", shop="日用百货", order_status="已签收",
                 price_cny="39", order_date=D(2026, 7, 7), items=[("洗发水(非集运)", 1)]),
            dict(order_no="TB250709110", platform_account="acctA", shop="画集屋", order_status="待付款",
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
