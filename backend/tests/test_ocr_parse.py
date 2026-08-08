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
    assert ocr._detect_purchase_status("交易成功", False) == "已签收"   # 闲鱼「交易成功」= 国内快递签收
    assert ocr._detect_purchase_status("交易关闭", False) == "交易关闭"
    assert ocr._detect_purchase_status("等待卖家发货", False) == "待发货"
    assert ocr._detect_purchase_status("卖家已发货", False) == "待收货"
    assert ocr._detect_purchase_status("随便什么", True) == "待收货"    # 有快递号即已发货
    assert ocr._detect_purchase_status("随便什么", False) == "待发货"


def test_detect_status_values_are_valid_enum():
    from app.models import PurchaseStatus
    valid = {s.value for s in PurchaseStatus}
    for text in ("交易成功", "交易关闭", "等待卖家发货", "卖家已发货", "无信息"):
        for has_express in (True, False):
            assert ocr._detect_purchase_status(text, has_express) in valid


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


# --- 关键词表的前缀安全（新增守卫）---------------------------------------------

def test_status_rule_table_is_prefix_safe():
    """靠前的关键词若是靠后关键词的子串，靠后那条**永远轮不到**——静默的死规则。

    今天表里最危险的是裸「退款」。将来做退货流程加「退款中」（非终态）时，
    它会被「退款」先吞成终态，而终态之后 `can_advance_purchase` 对该单一律 False，
    等于这张单从此再也不会被自动更新。那一刻这条测试先红，而不是等账本对不上。

    同一状态之间的子串是允许的（「确认收货」⊂「待确认收货」都是待收货，谁先命中都一样）。
    """
    from app.services.ocr import _EXPRESS, _STATUS_RULES

    flat = [(p, st) for patterns, st in _STATUS_RULES for p in patterns if p is not _EXPRESS]
    bad = []
    for i, (early, st_e) in enumerate(flat):
        for late, st_l in flat[i + 1:]:
            if early in late and st_e != st_l:
                bad.append(f"「{early}」({st_e}) 排在「{late}」({st_l}) 之前且是它的子串 → 后者永不命中")
    assert not bad, "关键词表有被吞并的死规则：\n  " + "\n  ".join(bad)


def test_terminal_rules_precede_the_express_number_rule():
    """退款单同样带快递号。终态必须排在「有快递号→待收货」之前，否则退款单被识别成待收货。

    这是历史事故的直接封堵：`_detect_purchase_status` 的注释记着这件事，但注释拦不住重排。
    """
    from app.models.base import PURCHASE_TERMINAL_STATUSES
    from app.services.ocr import _EXPRESS, _STATUS_RULES

    express_at = next(i for i, (ps, _) in enumerate(_STATUS_RULES) if _EXPRESS in ps)
    late_terminal = [st for ps, st in _STATUS_RULES[express_at:] if st in PURCHASE_TERMINAL_STATUSES]
    assert not late_terminal, f"终态 {late_terminal} 排在了「有快递号」之后，退款单会被识别成待收货"


# --- 商品标题里的快递词不许抢走快递锚点 ---------------------------------------

def _xianyu_page(title: str, express_row=("中通快递", "78912345678901")):
    """一张典型的闲鱼订单截图：状态 / 标题+价 / 成交价 / 订单编号 / 快递行。"""
    return (row(50, "等待卖家发货")
            + row(100, title, "¥520.00")
            + row(200, "成交价", "¥500.00")
            + row(300, "订单编号", "2612345678901234")
            + row(400, *express_row))


@pytest.mark.parametrize("title", [
    "全新未拆 顺丰包邮 手办一个",       # 中文：标题里带承运商名
    "Pokemon items 全新未拆",           # 英文：items 里的裸子串 ems
    "菜鸟驿站自提 手办一个",
])
def test_courier_word_in_title_does_not_hijack_the_express_anchor(title):
    """商品标题里出现快递词，不许把快递号抓成订单号。

    原实现是「整页第一个含公司关键词的框即锚点、命中即 break」，加上 `_same_row_value`
    没有距离上限的向下兜底——标题框抢走锚点后，express_no 一路抓到页面下方的订单号，
    两者完全相同；还连带把「等待卖家发货」判成「待收货」。
    """
    out = ocr.parse_order_fields(_xianyu_page(title))
    assert out["order_no"] == "2612345678901234"
    assert out["express_no"] == "78912345678901", "快递号被标题里的快递词带偏了"
    assert out["express_no"] != out["order_no"], "快递号被抓成了订单号"
    assert out["express_company"] == "中通快递"


def test_title_courier_word_without_any_real_express_row_yields_no_number():
    """页面上根本没有快递行时，标题里的快递词不许凭空造出一个快递号。"""
    page = (row(50, "等待卖家发货")
            + row(100, "全新未拆 顺丰包邮 手办一个", "¥520.00")
            + row(200, "成交价", "¥500.00")
            + row(300, "订单编号", "2612345678901234"))
    out = ocr.parse_order_fields(page)
    assert out["express_no"] is None, "没有快递行却抓出了快递号（多半是订单号）"
    assert out["order_no"] == "2612345678901234"


def test_real_label_beats_company_name_as_anchor():
    """有「快递单号」这种真标签时，优先用它——公司名只是补充信息。"""
    page = (row(100, "顺丰包邮 手办", "¥520.00")
            + row(300, "订单编号", "2612345678901234")
            + row(400, "快递单号", "78912345678901")
            + row(430, "中通快递"))
    out = ocr.parse_order_fields(page)
    assert out["express_no"] == "78912345678901"
    assert out["express_company"] == "中通快递"


def test_platform_markers_dont_collide_with_couriers():
    """平台强标记不许与快递公司名相交。

    裸「京东」曾同时是 COMPANY_MAP 的键（→ 京东物流）和 `_JD_MARKERS` 的成员：闲鱼卖家用
    京东快递发货时，同一次识别会既给出 express_company='京东物流'、又给出
    reject_reason='疑似京东订单截图'，前端据此不建单——两个结论直接打架。
    """
    couriers = set(ocr.COMPANY_MAP) | set(ocr.COMPANY_MAP.values())
    # 方向很重要：会出事的是「标记是某个快递公司名的子串」——那样截图里只要提到该快递，
    # 就顺带命中了平台标记。反过来（标记里含快递名，如「京东自营」含「京东」）无害：
    # 提到「京东物流」并不会让「京东自营」出现在文本里。
    for marker in ocr._JD_MARKERS + ocr._TAOBAO_MARKERS:
        clashes = [c for c in couriers if marker in c]
        assert not clashes, f"平台标记 {marker!r} 是快递公司名 {clashes} 的子串：提到该快递就会误判平台"


def test_xianyu_order_shipped_by_jd_courier_is_not_rejected():
    """闲鱼订单用京东快递发货，不许被判成「拿错截图」。"""
    page = (row(50, "等待收货")
            + row(200, "成交价", "¥500.00")
            + row(300, "订单编号", "2612345678901234")
            + row(400, "京东物流", "JD0123456789"))
    out = ocr.parse_order_fields(page)
    assert out["express_company"] == "京东物流"
    full = "".join(t[1] for t in page)
    assert ocr._detect_other_platform(full) is None, "京东快递被当成了京东平台标记"
