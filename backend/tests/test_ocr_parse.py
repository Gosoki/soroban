"""OCR 纯解析层（不跑引擎、不需要 rapidocr）：喂造好的 token 列表验字段抽取。

RapidOCR 结果的形状是 [[box, text, score], ...]，box 为 4 个角点。这里直接造这种结构。
"""
import pytest

from app.services import ocr


def tok(text, x=0.0, y=0.0, w=200.0, h=20.0):
    return [[[x, y], [x + w, y], [x + w, y + h], [x, y + h]], text, 0.99]


def row(y, *cells):
    """同一行的若干框（x 依次递增）。"""
    return [tok(t, x=100.0 + i * 220, y=y) for i, t in enumerate(cells)]


# --- 原语 -------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("订单编号 1234567890123456", "1234567890123456"),
    ("abc", None),
    ("", None),
])
def test_longest_digit_run(text, expected):
    assert ocr._longest_digit_run(text, 10) == expected


@pytest.mark.parametrize("text,expected", [
    ("SF1234567890", "SF1234567890"),
    ("顺丰 sf1234567890", "SF1234567890"),
    ("ATPTSTKH", None),                 # 纯字母（地址缩写）不该被当单号
    ("12345", None),                    # 太短
    ("JD0123456789", "JD0123456789"),
])
def test_extract_tracking(text, expected):
    assert ocr._extract_tracking(text, 8) == expected


@pytest.mark.parametrize("text,expected", [
    ("成品包裹号 2304513-1", "2304513-1"),
    ("2026-07-19", None),               # 日期必须被排除
    ("订单时间 2026-07-19 12:00:00", None),
])
def test_extract_package_no(text, expected):
    assert ocr._extract_package_no(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2026-07-19", "2026-07-19"),
    ("2026/7/9", "2026-07-09"),
    ("2026–07–19", "2026-07-19"),       # 全角破折号
    ("2026-02-31", None),               # 不存在的日期
    ("无日期", None),
])
def test_extract_date(text, expected):
    assert ocr._extract_date(text) == expected


@pytest.mark.parametrize("kw,canonical", [
    ("顺丰速运", "顺丰速运"), ("中通快递", "中通快递"), ("汇通", "百世快递"),
    ("EMS国际", "EMS"), ("没有这个", None),
])
def test_match_company(kw, canonical):
    assert ocr._match_company(kw) == canonical


# --- 商品订单（闲鱼）字段抽取 -------------------------------------------------

def test_parse_order_fields_full():
    result = [
        *row(100, "闲鱼"),
        *row(200, "顺丰速运", "SF7654321098"),
        *row(300, "订单编号", "2612345678901234"),
        *row(400, "成交价", "¥1,234.50"),
        *row(500, "下单时间", "2026-07-19 12:00:00"),
    ]
    f = ocr.parse_order_fields(result)
    assert f["express_company"] == "顺丰速运"
    assert f["express_no"] == "SF7654321098"
    assert f["order_no"] == "2612345678901234"
    assert f["price_cny"] == "1234.50"
    assert f["order_date"] == "2026-07-19"


def test_parse_price_thousand_separator():
    f = ocr.parse_order_fields(row(100, "成交价", "¥1,234.50"))
    assert f["price_cny"] == "1234.50"


def test_parse_price_same_box():
    f = ocr.parse_order_fields([tok("成交价 ¥88.00")])
    assert f["price_cny"] == "88.00"


def test_parse_order_fields_empty():
    f = ocr.parse_order_fields([])
    assert all(v is None for k, v in f.items())


def test_detect_status_lifecycle():
    assert ocr._detect_status("交易成功", False) == "已入仓"
    assert ocr._detect_status("交易关闭", False) == "交易关闭"
    assert ocr._detect_status("等待卖家发货", False) == "待发货"
    assert ocr._detect_status("卖家已发货", False) == "待收货"
    assert ocr._detect_status("随便什么", True) == "待收货"    # 有快递号即已发货
    assert ocr._detect_status("随便什么", False) == "待发货"


def test_detect_status_values_are_valid_enum():
    from app.models import OrderStatus
    valid = {s.value for s in OrderStatus}
    for text in ("交易成功", "交易关闭", "等待卖家发货", "卖家已发货", "无信息"):
        for has_express in (True, False):
            assert ocr._detect_status(text, has_express) in valid


def test_reject_other_platform():
    assert ocr._detect_other_platform("京东自营 白条") == "京东"
    assert ocr._detect_other_platform("天猫旗舰店") == "淘宝"
    assert ocr._detect_other_platform("普通文本") is None
    # 闲鱼线索存在时不该判为其它平台（由 recognize_order 组合判定）
    assert ocr._is_xianyu("蚂蚁森林能量") is True


# --- 集运截图 ---------------------------------------------------------------

def test_parse_shipment_package_page():
    result = [
        *row(100, "日本空运-广东直飞EMS"),
        *row(200, "国际单号", "EB861624386CN"),
        *row(300, "成品包裹号", "2304513-1"),
        *row(400, "订单时间", "2026-07-19 08:30:00"),
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["kind"] == "package"
    assert f["intl_tracking_no"] == "EB861624386CN"
    assert f["shipment_no"] == "2304513-1"
    assert f["date"] == "2026-07-19"
    assert f["channel"] == "日本空运-广东直飞EMS"


def test_parse_shipment_express_list_page():
    result = [
        *row(100, "内含快递"),
        *row(200, "快递单号", "SF1111111111"),
        *row(300, "快递单号", "YT2222222222"),
        *row(400, "快递单号", "SF1111111111"),   # 重复应去重
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["kind"] == "express_list"
    assert f["express_nos"] == ["SF1111111111", "YT2222222222"]


def test_parse_shipment_empty():
    f = ocr.parse_shipment_fields([])
    assert f["kind"] == "unknown" and f["express_nos"] == []


def test_shipment_chrome_words_not_taken_as_channel():
    result = [
        *row(100, "支付详情"),
        *row(200, "国际单号", "EB861624386CN"),
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["channel"] != "支付详情"
