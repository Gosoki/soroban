"""对抗性输入：超大/非有限数值、负数、注入串、超长文本、Unicode、类型混淆。
目标是「绝不 500」——要么正常处理、要么干净的 4xx。"""
import pytest

BAD_NUMBERS = ["NaN", "Infinity", "-Infinity", "1e400", "9" * 40, "-1", "abc", ""]


@pytest.mark.parametrize("val", BAD_NUMBERS)
def test_order_price_bad_numbers_never_500(client, val):
    r = client.post("/api/orders", json={"date": "2026-04-01", "price_cny": val})
    assert r.status_code == 422, f"{val!r} → {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("val", BAD_NUMBERS)
def test_misc_price_bad_numbers_never_500(client, val):
    r = client.post("/api/misc", json={"date": "2026-04-01", "name": "x", "price_cny": val})
    assert r.status_code == 422, f"{val!r} → {r.status_code}"


@pytest.mark.parametrize("val", ["NaN", "Infinity", "1e400", "0", "0.0001", "4.9999", "50.0001", "-20"])
def test_fx_rate_out_of_range_rejected(client, val):
    r = client.post("/api/orders", json={"date": "2026-04-01", "fx_rate": val})
    assert r.status_code == 422, f"{val!r} → {r.status_code}"


@pytest.mark.parametrize("val", ["5", "20.1234", "50"])
def test_fx_rate_in_range_accepted(client, val):
    r = client.post("/api/orders", json={"date": "2026-04-01", "fx_rate": val})
    assert r.status_code == 200, r.text


def test_jpy_override_overflow_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-04-01", "jpy_override": 2_147_483_648})
    assert r.status_code == 422


def test_jpy_override_negative_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-04-01", "jpy_override": -1})
    assert r.status_code == 422


def test_item_quantity_zero_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-04-01",
                                         "items": [{"name": "a", "quantity": 0}]})
    assert r.status_code == 422


def test_item_quantity_negative_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-04-01",
                                         "items": [{"name": "a", "quantity": -5}]})
    assert r.status_code == 422


def test_item_quantity_absurd_rejected(client):
    r = client.post("/api/orders", json={"date": "2026-04-01",
                                         "items": [{"name": "a", "quantity": 10 ** 9}]})
    assert r.status_code == 422


def test_derived_price_overflow_is_422_not_500(client):
    """物品数量×单价绕过单字段校验 → compute_money 必须兜住，返回 422。"""
    r = client.post("/api/orders", json={
        "date": "2026-04-01",
        "items": [{"name": "a", "quantity": 1_000_000, "price_cny": "9999999.99"}],
    })
    assert r.status_code == 422, r.text


def test_bad_date_rejected(client):
    for d in ["2026-02-31", "not-a-date", "", "2026-13-01"]:
        r = client.post("/api/orders", json={"date": d})
        assert r.status_code == 422, f"{d!r} → {r.status_code}"


SQLISH = [
    "'; DROP TABLE orders; --",
    "1' OR '1'='1",
    "\\", "%", "_", "100%", "a%b_c",
    "<script>alert(1)</script>",
    "\x00null-byte",
    "🎌絵文字テスト",
    "а" * 200,           # 西里尔字母，测多字节
]


@pytest.mark.parametrize("s", SQLISH)
def test_hostile_strings_in_shop_roundtrip(client, s):
    r = client.post("/api/orders", json={"date": "2026-04-01", "shop": s})
    # NUL 字节被 SQLite/驱动拒绝是可接受的（4xx/409），但不能 500
    assert r.status_code in (200, 409, 422), f"{s!r} → {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        assert r.json()["shop"] == s


@pytest.mark.parametrize("s", SQLISH)
def test_hostile_strings_in_search_never_500(client, s):
    r = client.get("/api/orders", params={"q": s})
    assert r.status_code == 200, f"{s!r} → {r.status_code}"


def test_overlong_shop_rejected_or_stored(client):
    """shop 列是 VARCHAR(255)：SQLite 不强制，MySQL 会报错。当前应至少不 500。"""
    r = client.post("/api/orders", json={"date": "2026-04-01", "shop": "x" * 5000})
    assert r.status_code in (200, 409, 422), r.status_code


def test_overlong_order_no_rejected_or_stored(client):
    r = client.post("/api/orders", json={"date": "2026-04-01", "order_no": "x" * 500})
    assert r.status_code in (200, 409, 422), r.status_code


def test_overlong_tag_value_rejected(client):
    r = client.post("/api/tags/recipient", json={"value": "x" * 500})
    assert r.status_code == 422


def test_unknown_tag_field_rejected(client):
    assert client.get("/api/tags/password").status_code == 422
    # '..' 被 URL 规范化掉 → 落到 /api/etc（无此路由）；总之不能当成合法字段处理
    assert client.post("/api/tags/../etc", json={"value": "x"}).status_code in (404, 405, 422)


def test_unknown_layout_table_rejected(client):
    assert client.get("/api/layout/users").status_code == 422
    assert client.put("/api/layout/users", json={"columns": []}).status_code == 422


def test_layout_roundtrip(client):
    cols = [{"key": "date", "width": 120}, {"key": "shop", "width": 200}]
    assert client.put("/api/layout/orders", json={"columns": cols}).status_code == 200
    got = client.get("/api/layout/orders").json()["columns"]
    assert got == cols


def test_layout_negative_width_stored_verbatim(client):
    """当前无宽度下限校验——记录现状，改了要同步这条。"""
    r = client.put("/api/layout/misc", json={"columns": [{"key": "date", "width": -50}]})
    assert r.status_code == 200


def test_wrong_types_never_500(client):
    for body in [
        {"date": "2026-04-01", "items": "not-a-list"},
        {"date": "2026-04-01", "items": [{"name": None}]},
        {"date": "2026-04-01", "shipment_order_id": "abc"},
        {"date": "2026-04-01", "jpy_override": "abc"},
        {"date": 12345},
        {},
    ]:
        r = client.post("/api/orders", json=body)
        assert r.status_code == 422, f"{body} → {r.status_code} {r.text[:200]}"


def test_ocr_rejects_non_image(client):
    r = client.post("/api/orders/ocr", files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_ocr_rejects_empty_image(client):
    r = client.post("/api/orders/ocr", files={"file": ("a.png", b"", "image/png")})
    assert r.status_code == 400


def test_ocr_rejects_oversized(client):
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024 + 1)
    r = client.post("/api/orders/ocr", files={"file": ("a.png", big, "image/png")})
    assert r.status_code == 413
