"""字符串列长度：所有 str 列都是定长 VARCHAR。超长时 SQLite 静默照存、MySQL 直接
「Data too long」→ 500，是典型的双引擎发散。这里守住两条规则：
  · 标识类列（订单号/快递号/账号…）超长 → 422
  · 自由文本列（商品标题/物品名/杂项名）超长 → 截断到列长（不打断爬虫整批同步）
并用一个元测试保证：新增列时不会漏掉校验。
"""
import pytest
from pydantic import BaseModel

from app import schemas as S
from app.models import MiscExpense, Order, OrderItem, OrderStaging, ShipmentOrder, TagOption

# 短文本列：超长截断而非拒绝（见 schemas._clip）。
# `title`（商品标题）不在此列——它已放宽成 TEXT 列，不限长也不截断。
CLIPPED = {"name"}
# 服务端决定或已有枚举白名单的列，不接受任意客户端字符串，无需长度校验
SERVER_OWNED = {"created_via", "purchase_status", "shipment_status", "import_status"}

# (输入 schema, 对应模型)
PAIRS = [
    (S.OrderCreate, Order), (S.OrderUpdate, Order),
    (S.ShipmentCreate, ShipmentOrder), (S.ShipmentUpdate, ShipmentOrder),
    (S.MiscCreate, MiscExpense), (S.MiscUpdate, MiscExpense),
    (S.StagingCreate, OrderStaging), (S.StagingUpdate, OrderStaging),
    (S.OrderItemIn, OrderItem), (S.StagingItemIn, OrderItem),
    (S.TagIn, TagOption),
]


def _max_length(model_cls: type[BaseModel], field: str):
    for meta in model_cls.model_fields[field].metadata:
        if hasattr(meta, "max_length"):
            return meta.max_length
    return None


@pytest.mark.parametrize("schema,model", PAIRS, ids=lambda x: getattr(x, "__name__", str(x)))
def test_every_varchar_input_field_is_bounded(schema, model):
    """元测试：schema 里每个对应到 VARCHAR 列的 str 字段，要么声明了与列等长的 max_length，
    要么在 CLIPPED 里（走截断）。加了新列却忘了校验就会红。"""
    cols = {c.name: c for c in model.__table__.columns}
    for name in schema.model_fields:
        col = cols.get(name)
        if col is None or not str(col.type).startswith("VARCHAR"):
            continue
        if name in SERVER_OWNED or name in CLIPPED:
            continue
        assert _max_length(schema, name) == col.type.length, (
            f"{schema.__name__}.{name} 未按 {model.__tablename__}.{name} "
            f"VARCHAR({col.type.length}) 限长"
        )


@pytest.mark.parametrize("schema,model", PAIRS, ids=lambda x: getattr(x, "__name__", str(x)))
def test_clipped_fields_actually_clip(schema, model):
    cols = {c.name: c for c in model.__table__.columns}
    for name in CLIPPED & set(schema.model_fields):
        col = cols.get(name)
        if col is None or not str(col.type).startswith("VARCHAR"):
            continue
        limit = col.type.length
        payload = {name: "字" * (limit + 50)}
        # 补齐必填字段
        for req, f in schema.model_fields.items():
            if f.is_required() and req not in payload:
                payload[req] = {"date": "2026-01-01", "version": 1}.get(req, "x")
        obj = schema(**payload)
        assert len(getattr(obj, name)) == limit, f"{schema.__name__}.{name} 未截断到 {limit}"


# --- 端到端：走真实路由确认 422 / 截断 ---------------------------------------

@pytest.mark.parametrize("field,limit", [
    ("order_no", 64), ("category", 64), ("platform", 32),
    ("express_no", 64), ("express_company", 64), ("platform_account", 64),
])
def test_order_overlong_identifier_is_422(client, field, limit):
    r = client.post("/api/orders", json={"date": "2026-04-01", field: "x" * (limit + 1)})
    assert r.status_code == 422, f"{field} 超长未被拒：{r.status_code}"


def test_order_at_limit_accepted(client):
    r = client.post("/api/orders", json={"date": "2026-04-01", "order_no": "x" * 64})
    assert r.status_code == 200, r.text


def test_shop_is_a_text_column_on_both_models():
    """商品标题必须是无长度上限的 TEXT：一单多件时爬虫会把各件标题拼起来，轻易超 255。"""
    for model in (Order, OrderStaging):
        assert model.__table__.columns["title"].type.length is None, \
            f"{model.__tablename__}.title 又变回定长列了，超长标题会被截断/500"


def test_shop_is_not_indexed():
    """TEXT 能用的前提：该列不参与任何索引/唯一约束——MySQL 给 TEXT 建索引必须写前缀长度，
    一旦有人给 title 加了 index，迁移 d0e1f2a3b4c5 的 MODIFY ... TEXT 会直接失败。"""
    for model in (Order, OrderStaging):
        indexed = {c.name for idx in model.__table__.indexes for c in idx.columns}
        assert "title" not in indexed, f"{model.__tablename__}.title 被加了索引，与 TEXT 列冲突"
        assert model.__table__.columns["title"].index is not True


def test_order_overlong_shop_is_kept_whole(client):
    """长标题原样保存，一个字都不丢。"""
    title = "商" * 400
    r = client.post("/api/orders", json={"date": "2026-04-01", "title": title})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == title


def test_staging_overlong_shop_is_kept_whole(client):
    title = "标" * 600
    r = client.post("/api/staging", json={"order_no": "LONGTITLE-1", "title": title})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == title


def test_long_title_survives_import_to_ledger(client):
    """导入账本时标题原样搬过去（两边都是 TEXT）。"""
    title = "超长商品标题 / " * 60
    s = client.post("/api/staging", json={"order_no": "LONGTITLE-2", "title": title}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    assert o["title"] == title


def test_overlong_item_name_is_clipped(client):
    r = client.post("/api/orders", json={
        "date": "2026-04-01", "items": [{"name": "物" * 400, "quantity": 1, "unit_price_cny": "1"}]})
    assert r.status_code == 200, r.text
    assert len(r.json()["items"][0]["name"]) == 255


def test_overlong_misc_name_is_clipped(client):
    r = client.post("/api/misc", json={"date": "2026-04-01", "name": "杂" * 400})
    assert r.status_code == 200, r.text
    assert len(r.json()["name"]) == 255


@pytest.mark.parametrize("field,limit", [
    ("shipment_no", 64), ("intl_tracking_no", 128), ("recipient", 128),
])
def test_shipment_overlong_identifier_is_422(client, field, limit):
    r = client.post("/api/shipment", json={"date": "2026-04-01", field: "x" * (limit + 1)})
    assert r.status_code == 422, f"{field} 超长未被拒：{r.status_code}"


@pytest.mark.parametrize("field,limit", [
    ("order_no", 64), ("platform_account", 64), ("platform", 32), ("express_no", 64),
])
def test_staging_overlong_identifier_is_422(client, field, limit):
    r = client.post("/api/staging", json={field: "x" * (limit + 1)})
    assert r.status_code == 422, f"{field} 超长未被拒：{r.status_code}"


def test_override_note_overlong_is_422(client):
    r = client.post("/api/misc", json={"date": "2026-04-01", "name": "n",
                                       "override_note": "x" * 256})
    assert r.status_code == 422


def test_shipment_weight_overflow_is_422(client):
    """weight = Numeric(8,2)：超上限在 MySQL 会 Out of range → 500，必须先卡成 422。"""
    r = client.post("/api/shipment", json={"date": "2026-04-01", "weight": "99999999.99"})
    assert r.status_code == 422, r.status_code


def test_shipment_weight_negative_is_422(client):
    assert client.post("/api/shipment",
                       json={"date": "2026-04-01", "weight": "-1"}).status_code == 422


def test_shipment_weight_nan_is_422(client):
    assert client.post("/api/shipment",
                       json={"date": "2026-04-01", "weight": "NaN"}).status_code == 422


def test_shipment_weight_ok(client):
    r = client.post("/api/shipment", json={"date": "2026-04-01", "weight": "4.567"})
    assert r.status_code == 200
    assert r.json()["weight"] == "4.57"    # 量化到 2 位


# --- Update 路径也要吃到同一套校验（曾经 Create/Update 各写一份，容易只加一半）---

def test_shipment_update_validates_weight(client):
    s = client.post("/api/shipment", json={"date": "2026-04-01"}).json()
    r = client.patch(f"/api/shipment/{s['id']}",
                     json={"version": s["version"], "weight": "99999999.99"})
    assert r.status_code == 422


def test_shipment_update_validates_special_fee(client):
    s = client.post("/api/shipment", json={"date": "2026-04-01"}).json()
    r = client.patch(f"/api/shipment/{s['id']}",
                     json={"version": s["version"], "special_fee_jpy": -1})
    assert r.status_code == 422


def test_shipment_update_validates_length(client):
    s = client.post("/api/shipment", json={"date": "2026-04-01"}).json()
    r = client.patch(f"/api/shipment/{s['id']}",
                     json={"version": s["version"], "recipient": "x" * 129})
    assert r.status_code == 422


def test_order_update_validates_length(client):
    o = client.post("/api/orders", json={"date": "2026-04-01"}).json()
    r = client.patch(f"/api/orders/{o['id']}",
                     json={"version": o["version"], "order_no": "x" * 65})
    assert r.status_code == 422


def test_order_update_keeps_long_shop_whole(client):
    o = client.post("/api/orders", json={"date": "2026-04-01"}).json()
    title = "商" * 400
    r = client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": title})
    assert r.status_code == 200 and r.json()["title"] == title


def test_misc_update_validates_length(client):
    m = client.post("/api/misc", json={"date": "2026-04-01", "name": "n"}).json()
    r = client.patch(f"/api/misc/{m['id']}", json={"version": m["version"], "category": "x" * 65})
    assert r.status_code == 422


def test_staging_update_validates_length(client):
    s = client.post("/api/staging", json={"order_no": "SU-LEN"}).json()
    r = client.patch(f"/api/staging/{s['id']}",
                     json={"version": s["version"], "express_no": "x" * 65})
    assert r.status_code == 422


def test_server_owned_names_are_real_columns():
    """豁免名单的元断言：留着旧名永远不会红，保护范围却在悄悄缩小。

    改名时最容易漏的就是这类名单——`status` 改成 `purchase_status` 之后，
    名单里那个 `status` 不再匹配任何字段，于是新列**静默地失去了长度校验**。
    """
    real = set()
    for _, model in PAIRS:
        real |= set(model.__table__.columns.keys())
    stale = SERVER_OWNED - real
    assert not stale, f"SERVER_OWNED 里这些名字已不是任何模型的列，名单该更新了：{sorted(stale)}"
