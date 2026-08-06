"""命名歧义的防回归。

审计「三、命名歧义」全部落地后，这里把每条约定写成断言——否则下一个人很容易又加一个
`shop`、又让两个枚举共用一个字面量。每条都附了「当初为什么是坑」。
"""
import re
from pathlib import Path

import pytest
from sqlmodel import SQLModel

from app.models import (
    MiscExpense,
    Order,
    OrderItem,
    OrderStaging,
    OrderStatus,
    ShipmentOrder,
    ShipmentStatus,
    StagingItem,
    StagingStatus,
)

_REPO = Path(__file__).resolve().parents[2]


# --- 一、列名不该骗人 ---------------------------------------------------------

def test_no_shop_column_anywhere():
    """`shop` 存的其实是**商品标题**（爬虫 normalize、UI 列头都叫「商品」），已改名 title。"""
    for name, table in SQLModel.metadata.tables.items():
        assert "shop" not in table.columns, f"{name} 又出现了 shop 列——它存的是商品标题，请叫 title"


@pytest.mark.parametrize("model", [Order, OrderStaging])
def test_title_column_exists(model):
    assert "title" in model.__table__.columns


def test_item_price_column_is_unit_scoped():
    """物品的价是**单价**，订单的 price_cny 是**总价**。两者曾同名，且有 Σ单价×数量=总价 的关系，
    是最容易看串的一处。"""
    for model in (OrderItem, StagingItem):
        cols = model.__table__.columns
        assert "unit_price_cny" in cols, f"{model.__tablename__} 缺 unit_price_cny"
        assert "price_cny" not in cols, \
            f"{model.__tablename__} 又出现了 price_cny——物品是单价，请叫 unit_price_cny"


def test_order_total_price_column_kept():
    """反向确认：订单/暂存/杂项上的 price_cny（总价）**没有**被误改。"""
    for model in (Order, OrderStaging, ShipmentOrder, MiscExpense):
        assert "price_cny" in model.__table__.columns


def test_staging_two_statuses_are_distinguishable():
    """一行上两个「状态」：导入工作流 vs 真实交易状态。旧名 status / order_status 看不出区别。"""
    cols = OrderStaging.__table__.columns
    assert "import_status" in cols and "trade_status" in cols
    assert "status" not in cols and "order_status" not in cols


def test_created_via_replaces_source():
    """`source`（手填/导入/机器人）与 `platform`（淘宝/闲鱼，UI 标签就叫「来源」）都读作「来源」。"""
    for model in (Order, ShipmentOrder, MiscExpense):
        cols = model.__table__.columns
        assert "created_via" in cols, f"{model.__tablename__} 缺 created_via"
        assert "source" not in cols, f"{model.__tablename__} 又出现了 source，请用 created_via"


# --- 二、枚举字面量不该跨表重名 ------------------------------------------------

def test_order_and_shipment_status_literals_are_disjoint():
    """订单状态与集运状态曾共用「已签收」——而 EXCLUDED_STATUSES 这类集合会同时作用于两张表，
    同字面量意味着「改一个枚举会静默影响另一张表的过滤」。现在必须完全不相交。"""
    order = {s.value for s in OrderStatus}
    shipment = {s.value for s in ShipmentStatus}
    assert not (order & shipment), f"两个状态枚举又有了同名值：{sorted(order & shipment)}"


def test_order_status_uses_warehoused_not_received():
    """订单的这一步是「国内快递**被集运仓**签收」，叫「已入仓」比「已签收」准确，
    也避开与集运单「已签收」（国际包裹**本人**收到）的碰撞。"""
    assert OrderStatus.warehoused.value == "已入仓"
    assert "已签收" not in {s.value for s in OrderStatus}
    assert ShipmentStatus.received.value == "已签收"


def test_status_enum_attribute_names_do_not_collide_confusingly():
    """`OrderStatus.arrived`（已到达）与旧的 `ShipmentStatus.arrived`（已签收）同名不同义，
    写 `Status.arrived` 时得先想清楚是哪个枚举。现在集运侧叫 received。"""
    assert not hasattr(ShipmentStatus, "arrived")
    assert OrderStatus.arrived.value == "已到达"


def test_staging_status_enum_untouched():
    assert {s.value for s in StagingStatus} == {"待处理", "已导入", "已忽略"}


# --- 三、函数名不该骗人 -------------------------------------------------------

def test_replace_data_name_conveys_destructiveness():
    """它会**先清空**目标库再拷；叫 copy_data 读不出破坏性。"""
    from app.services import db_migrate
    assert hasattr(db_migrate, "replace_data")
    assert not hasattr(db_migrate, "copy_data")


def test_raise_helpers_are_prefixed():
    """`not_found()` / `conflict()` 是 raise 而不是 return，不加前缀读起来像构造器。"""
    from app.routers import common
    assert hasattr(common, "raise_not_found") and hasattr(common, "raise_conflict")
    assert not hasattr(common, "not_found") and not hasattr(common, "conflict")


# --- 四、跨层一致：前端 / 爬虫也得跟着改 ---------------------------------------

def test_frontend_has_no_stale_field_names():
    """前端按字段名读写后端返回的行；漏改一个就是永远显示「—」或保存无效——而且不会报错。"""
    stale = {
        r"\brow\.shop\b": "row.title",
        r"\bit\.price_cny\b": "it.unit_price_cny",
        r"key: 'shop'": "key: 'title'",
        r"key: 'order_status'": "key: 'trade_status'",
    }
    hits = []
    for f in (_REPO / "frontend" / "src").rglob("*.vue"):
        text = f.read_text(encoding="utf-8")
        for pat, want in stale.items():
            if re.search(pat, text):
                hits.append(f"{f.relative_to(_REPO)}: {pat} → 应为 {want}")
    assert not hits, "前端还有旧字段名：\n" + "\n".join(hits)


def test_scraper_pushes_current_field_names():
    """爬虫按字段名 POST /api/staging；名字对不上 → 字段被忽略或 422，整批静默丢同步。"""
    norm = _REPO / "scraper" / "soroban-scraper-taobao" / "taobao_scraper" / "normalize.py"
    if not norm.is_file():
        pytest.skip("插件未安装")
    text = norm.read_text(encoding="utf-8")
    assert '"title"' in text and '"trade_status"' in text and '"unit_price_cny"' in text
    assert '"shop"' not in text and '"order_status"' not in text


def test_frontend_order_status_matches_backend_after_rename():
    """改状态字面量最容易漏前端。test_consistency.py 已全面比对，这里只钉住「已入仓」这一条。"""
    js = (_REPO / "frontend" / "src" / "constants.js").read_text(encoding="utf-8")
    assert "'已入仓'" in js
    orders_vue = (_REPO / "frontend" / "src" / "views" / "Orders" / "index.vue").read_text(encoding="utf-8")
    assert "已入仓: 3" in orders_vue, "Orders 页的 STATUS_RANK 没跟着改"
