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
    PurchaseStatus,
    ShipmentOrder,
    ShipmentStatus,
    StagingItem,
    ImportStatus,
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
    assert "import_status" in cols and "purchase_status" in cols
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
    order = {s.value for s in PurchaseStatus}
    shipment = {s.value for s in ShipmentStatus}
    assert not (order & shipment), f"两个状态枚举又有了同名值：{sorted(order & shipment)}"


def test_two_legs_use_distinct_words():
    """两段各用各的词，且不重名：
      · 订单（国内段）：快递签收 = 「已签收」，就是淘宝/闲鱼页面的「交易成功」
      · 集运单（国际段）：包裹到本人手上 = 「已送达」
    曾经两边都叫「已签收」，而 EXCLUDED_STATUSES 这类集合同时作用于两张表——
    同字面量意味着「改一个枚举会静默影响另一张表的过滤」。"""
    assert PurchaseStatus.signed.value == "已签收"
    assert ShipmentStatus.delivered.value == "已送达"
    assert "已入仓" not in {s.value for s in PurchaseStatus}, "已入仓 已取消，别再冒出来"
    assert "已签收" not in {s.value for s in ShipmentStatus}


def test_status_enum_attribute_names_do_not_collide_confusingly():
    """属性名也不许跨枚举撞：曾经两边都有 `arrived`，写 `Status.arrived` 得先想清楚是哪个枚举。
    现在订单侧只到 signed（已签收，国内段终点），集运侧 delivered（已送达）。"""
    assert not hasattr(ShipmentStatus, "arrived")
    assert not hasattr(ShipmentStatus, "signed")
    assert not hasattr(PurchaseStatus, "delivered")
    assert not hasattr(PurchaseStatus, "consolidating"), "国际段状态不该回到订单枚举里"


def test_staging_status_enum_untouched():
    assert {s.value for s in ImportStatus} == {"待处理", "已导入", "已忽略"}


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
        r"key: 'order_status'": "key: 'purchase_status'",
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
    import ast

    from app.schemas import StagingCreate, StagingItemIn
    from tests.test_consistency import plugin_source

    src = plugin_source("taobao_scraper", "normalize.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 取**真正被推送的那两个 dict 字面量**的键，而不是 grep 文件里有没有出现某个词。
    # 文件里同时有 demo_order、注释、docstring，随便哪处出现 "purchase_status" 都能骗过
    # 文本断言 —— 第一版就是这样：把 normalize() 里的键改成旧名 `trade_status`，
    # 守卫照样全绿（别处还有那个词）。
    produced = {}
    for fn in ("normalize", "demo_order"):
        node = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == fn), None)
        assert node is not None, f"插件里没有 {fn}()，跨仓契约的形状变了"
        keys = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Dict):
                keys |= {k.value for k in sub.keys
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        produced[fn] = keys

    allowed = set(StagingCreate.model_fields) | set(StagingItemIn.model_fields)
    for fn, keys in produced.items():
        unknown = keys - allowed
        assert not unknown, (
            f"插件 {fn}() 推的这些键后端不认：{sorted(unknown)}\n"
            f"名字对不上 → 字段被 extra=forbid 拒成 422，整批静默丢同步。"
            f"后端接受：{sorted(allowed)}")

    # 必须真的推这几项，否则「键没写错」也可能是「整段没了」
    for fn in produced:
        must = {"title", "purchase_status"} if fn == "normalize" else {"purchase_status"}
        missing = must - produced[fn]
        assert not missing, f"插件 {fn}() 不再推 {sorted(missing)} 了"
    assert "unit_price_cny" in produced["normalize"] | produced["demo_order"], \
        "插件不再推物品单价了"


def test_frontend_status_words_match_backend():
    """改状态字面量最容易漏前端。test_consistency.py 已全面比对，这里钉住这两个词。"""
    js = (_REPO / "frontend" / "src" / "constants.js").read_text(encoding="utf-8")
    assert "'已签收'" in js and "'已送达'" in js
    assert "'已入仓'" not in js, "前端还留着已取消的「已入仓」"
    assert "已签收: 3" in js, "constants.js 的 PURCHASE_STATUS_RANK 没跟着改"
