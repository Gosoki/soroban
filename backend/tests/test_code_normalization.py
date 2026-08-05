"""单号类列的写入归一。

背景：这些值是**匹配键**（集运「内含快递」截图靠 express_no 精确匹配商品订单）。OCR 提取时
已 `.upper()`，用户手输可能小写或带粘贴来的首尾空格；而字符串精确比较在 **SQLite 上区分
大小写、MySQL 默认不区分**——不归一就是「同一份数据两种后端行为不同」。

约定（见 schemas._norm_code / _norm_id）：
  · express_no / intl_tracking_no —— TRIM + UPPER（无唯一约束，可安全改写历史数据）
  · order_no / shipment_no       —— 只 TRIM，不改大小写（这两列有唯一索引，批量改写有撞约束
                                    风险；且实际都是纯数字/数字-数字，大小写本就不是问题）
"""
import pytest


# --- express_no：TRIM + UPPER ------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  sf1234567890  ", "SF1234567890"),
    ("sf1234567890", "SF1234567890"),
    ("SF1234567890", "SF1234567890"),
    ("\tjd0987654321\n", "JD0987654321"),
    ("1234567890", "1234567890"),
])
def test_order_express_no_normalized(client, raw, expected):
    o = client.post("/api/orders", json={"date": "2028-01-01", "express_no": raw}).json()
    assert o["express_no"] == expected


def test_blank_express_no_becomes_null(client):
    """空白串归 NULL：否则 `is_not(None)` 之类的过滤会把空值当成有效值放行。"""
    o = client.post("/api/orders", json={"date": "2028-01-01", "express_no": "   "}).json()
    assert o["express_no"] is None


def test_staging_express_no_normalized(client):
    s = client.post("/api/staging", json={"order_no": "N-1", "express_no": " yt55556666 "}).json()
    assert s["express_no"] == "YT55556666"


def test_express_no_normalized_on_patch(client):
    o = client.post("/api/orders", json={"date": "2028-01-01"}).json()
    r = client.patch(f"/api/orders/{o['id']}",
                     json={"version": o["version"], "express_no": " sf000111222 "})
    assert r.json()["express_no"] == "SF000111222"


def test_intl_tracking_no_normalized(client):
    s = client.post("/api/shipment",
                    json={"date": "2028-01-01", "intl_tracking_no": " eb861624386cn "}).json()
    assert s["intl_tracking_no"] == "EB861624386CN"


# --- 归一带来的实际收益：过滤/匹配稳定命中 -----------------------------------

def test_express_filter_matches_regardless_of_input_case(client):
    """用户小写手输、OCR 大写提取，两边都归一后按快递号筛选必然命中。"""
    client.post("/api/orders", json={"date": "2028-02-01", "order_no": "EF-1",
                                     "platform": "闲鱼", "express_no": "sf777888999"})
    got = client.get("/api/orders", params={"express_no": "SF777888999"}).json()
    assert got["total"] >= 1


def test_import_carries_normalized_express_no(client):
    s = client.post("/api/staging", json={"order_no": "N-IMP", "express_no": " zt121212 "}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    assert o["express_no"] == "ZT121212"


def test_ocr_extracted_uppercase_matches_manually_typed_lowercase(client):
    """这条是修复的核心场景：手输小写的单，被 OCR 大写的号匹配上。"""
    from app.services import ocr
    o = client.post("/api/orders", json={"date": "2028-02-02", "order_no": "EF-2",
                                         "platform": "闲鱼", "express_no": "sf313131313"}).json()
    extracted = ocr._extract_tracking("顺丰速运 sf313131313", 8)     # OCR 侧恒大写
    assert extracted == "SF313131313"
    assert client.get(f"/api/orders/{o['id']}").json()["express_no"] == extracted


# --- order_no / shipment_no：只 TRIM ------------------------------------------

def test_order_no_trimmed_not_uppercased(client):
    o = client.post("/api/orders", json={"date": "2028-03-01", "order_no": "  ab12345  ",
                                         "platform": "淘宝"}).json()
    assert o["order_no"] == "ab12345"       # 去空格，但不转大写


def test_shipment_no_trimmed_not_uppercased(client):
    s = client.post("/api/shipment", json={"date": "2028-03-01",
                                           "shipment_no": " 2304513-1 "}).json()
    assert s["shipment_no"] == "2304513-1"


def test_trimming_closes_the_unique_constraint_hole(client):
    """粘贴带来的首尾空格曾让唯一约束形同虚设（" 123" 与 "123" 被当成两张单）。"""
    client.post("/api/orders", json={"date": "2028-03-02", "order_no": "TRIM-1", "platform": "淘宝"})
    r = client.post("/api/orders", json={"date": "2028-03-02", "order_no": "  TRIM-1  ",
                                         "platform": "淘宝"})
    assert r.status_code == 409


def test_blank_order_no_becomes_null(client):
    """全空白的订单号归 NULL，才能落进「允许多条空单号」那条部分唯一索引。"""
    a = client.post("/api/orders", json={"date": "2028-03-03", "order_no": "   "}).json()
    b = client.post("/api/orders", json={"date": "2028-03-03", "order_no": ""})
    assert a["order_no"] is None
    assert b.status_code == 200 and b.json()["order_no"] is None


def test_staging_blank_order_no_allows_multiple(client):
    a = client.post("/api/staging", json={"order_no": "  "})
    b = client.post("/api/staging", json={"order_no": ""})
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["order_no"] is None and b.json()["order_no"] is None
