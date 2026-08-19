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
    assert ocr._detect_purchase_status("交易关闭", False) is None       # 终态不写，见下面那条
    assert ocr._detect_purchase_status("等待卖家发货", False) == "待发货"
    assert ocr._detect_purchase_status("卖家已发货", False) == "待收货"
    assert ocr._detect_purchase_status("随便什么", True) == "待收货"    # 有快递号即已发货
    assert ocr._detect_purchase_status("随便什么", False) == "待发货"


def test_detect_status_values_are_valid_enum():
    from app.models import PurchaseStatus
    valid = {s.value for s in PurchaseStatus} | {None}   # None = 终态，刻意不写
    for text in ("交易成功", "交易关闭", "等待卖家发货", "卖家已发货", "无信息"):
        for has_express in (True, False):
            assert ocr._detect_purchase_status(text, has_express) in valid


@pytest.mark.parametrize("text", [
    "退款成功 买家已收到退款",
    "交易关闭",
    "商品详情 支持七天无理由退款",        # ← 一个按钮/说明文案就够
    "申请退款  联系卖家  查看物流",
])
def test_ocr_never_sets_a_terminal_status(text):
    """OCR **绝不**把订单设成终态（退款 / 交易关闭）。

    判定是在整页拼起来的 full_text 上做**无锚点子串扫描**，而规则表第一条含裸「退款」——
    页面上任何位置出现这两个字都会命中，包括「申请退款」按钮和「支持七天无理由退款」
    这类商品说明。而终态是**不可逆**的：`can_advance_purchase` 对终态一律 False，
    这张单从此再也不会被任何自动流程更新，爬虫之后抓到真实状态也推不动它。

    「猜错一个中间态」下一轮就自动纠正了；「猜错一个终态」是永久钉死。
    不对称到这个程度，就不该由子串扫描来决定——留空，人在暂存表上确认。
    """
    from app.models import PURCHASE_TERMINAL_STATUSES

    for has_express in (True, False):
        got = ocr._detect_purchase_status(text, has_express)
        assert got not in PURCHASE_TERMINAL_STATUSES, f"{text!r} 被判成了终态 {got}"


def test_terminal_is_left_blank_not_downgraded_to_a_guess():
    """命中终态时留空，**不退而求其次猜一个非终态**。

    截图真是退款单时，硬写成「待发货」是另一种错——而且那个错会被当成真值用。
    """
    assert ocr._detect_purchase_status("退款成功", False) is None
    assert ocr._detect_purchase_status("退款成功", True) is None, "有快递号也不该改判"


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


def test_below_fallback_will_not_reach_across_the_page():
    """本行取不到号时，往下兜底**最多跨 2 行**——不能一路扫到页脚。

    没有这条上限时：`快递单号 YT2222 333344` 被 OCR 断成两截（各 6 位，都不满足
    min_len=8）→ 同行无候选 → 兜底扫到页面最下方的客服电话 `13800138000`，
    而 11 位纯数字正好落在中通/韵达单号的长度区间，`Order.express_no == no`
    会精确命中，把货挂到一张毫不相干的订单上。
    """
    result = [
        *row(200, "快递单号", "YT2222", "333344"),
        *row(2400, "客服电话", "13800138000"),          # 隔了 2200px 的页脚
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["express_nos"] == []                       # 宁可不取，也不能取错
    assert f["unreadable"] == 1                          # 但必须留下「我漏了一行」的记号


def test_below_fallback_still_works_for_header_over_value_layout():
    """兜底本身不能禁掉：真实截图存在「列头在上、号在紧邻下一行」的表格版式。"""
    result = [
        *row(200, "快递单号"),
        *row(216, "SF1111111111"),                       # 下一行（row_tol≈16）
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["express_nos"] == ["SF1111111111"] and f["unreadable"] == 0


def test_unreadable_counts_rows_whose_number_could_not_be_read():
    """看得见「快递单号」标签、却没取到号 → 计入 unreadable。

    这个计数是「少挂了一单」在响应里的**唯一**出口：没有它，3 行里坏 1 行的截图
    只会得到 2 个号，前端照样弹绿色的「已关联 2 单」。
    """
    result = [
        *row(200, "快递单号", "SF1111111111"),
        *row(300, "快递单号", "坏掉的一行"),            # 取不到
        *row(400, "快递单号", "YT2222222222"),
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["express_nos"] == ["SF1111111111", "YT2222222222"]
    assert f["unreadable"] == 1


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
    京东快递发货时，同一次识别会既给出 express_company='京东物流'、又判成「京东的截图」
    ——两个结论直接打架，而用户会被问一句莫名其妙的「这好像不是闲鱼的截图」。
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


@pytest.mark.parametrize("text,want", [
    # 被 OCR 断成两截 → 判为读不出，**不能**把半截号交出去
    ("快递单号 SF1234 56789012", None),      # 后半截更长
    ("快递单号 SF123456 789012", None),      # 前半截更长
    # 不该误伤的
    ("快递单号 SF1111111111", "SF1111111111"),
    # ⚠️ 这一条能活下来靠的是**长度闸**（12 位 ≥ _TRACK_TYPICAL_MIN），不是「日期用连字符」——
    # `_looks_split` 对它其实返回 True（右侧正则没有右锚定，`2026` 就够了，实测）。
    # 原注释把绿的原因说错了，属于「判据被另一个原因满足」那一类。
    ("快递单号 SF1111111111 2026-08-01 已签收", "SF1111111111"),
    ("快递单号 12345678", "12345678"),        # 孤立的短号：可疑但没有断开的证据，照取
    ("快递单号 4312345678901", "4312345678901"),
    ("收件地址 ATPTSTKH", None),              # 纯字母串
    # **长半截恰好 10~11 位**：原先 `_TRACK_TYPICAL_MIN = 10` 让这一档整个漏过去，
    # 而 `shipment.py` 的闸又恰好是 `len(no) < 10` ⇒ 两道闸都过 ⇒ 精确匹配 + 原子挂靠。
    # 阈值改成有依据的 12（= 注释里列出的主流快递号真实下界）之后才挡得住。
    ("快递单号 7512345678 9012", None),       # 纯数字，长半截 10
    ("快递单号 SF12345678 9012", None),       # 含字母，长半截 10
    ("国际单号 EB86162438 6CN", None),        # EMS 被断开，长半截 10
    ("快递单号 43123456789 012", None),       # 长半截 11
    # **反面**：孤立的短号仍照取（判据是「偏短 **且** 紧邻另一段数字」，不是只看长度）
    ("快递单号 7512345678", "7512345678"),
    ("快递单号 SF1111111111 1件", "SF1111111111"),   # 12 位真号，旁边有数字也不该误伤
])
def test_split_tracking_number_is_reported_as_unreadable(text, want):
    """半截快递号**不许**交出去——它会被 `Order.express_no == no` 拿去精确匹配并原子挂靠。

    匹配不上还好（只漏一单）；万一撞上别人的单号，就是把货挂到一张无关订单上，
    而 `version` 已经 +1、不可撤销。口径与 `_same_row_value` 的兜底上限一致：
    **宁可不取，也不能取错**——取不到会落进 `unreadable` 计数，用户看得见。

    判据要求「偏短 **且** 紧邻另一段数字」，所以孤立的 8 位号不受影响。
    """
    assert ocr._extract_tracking(text) == want


def test_split_tracking_row_lands_in_unreadable():
    """端到端：断号那一行进 unreadable，不会悄悄少一单也不会取到错号。"""
    result = [
        *row(200, "快递单号", "SF1111111111"),
        *row(300, "快递单号 YT2222 333344"),          # 同一个框里被断成两截
        *row(2400, "客服电话", "13800138000"),
    ]
    f = ocr.parse_shipment_fields(result)
    assert f["express_nos"] == ["SF1111111111"]
    assert f["unreadable"] == 1


# --- recognize_order 的整条链路（打桩引擎，不需要 rapidocr/pillow）------------

def _stub_recognize(monkeypatch, texts):
    """把引擎与解码都打桩，只跑 recognize_order 自己的控制流。"""
    from app.services import ocr as m

    monkeypatch.setattr(m, "_get_engine", lambda: object())
    monkeypatch.setattr(m, "_decode_image", lambda b: "FAKE_ARRAY")
    monkeypatch.setattr(m, "_run_engine", lambda eng, arr: [tok(t) for t in texts])
    monkeypatch.setattr(m, "_truck_present", lambda arr: False)


def test_recognize_order_warns_on_other_platform_without_crashing(monkeypatch):
    """含京东/淘宝强标记的截图走「拒识」分支——这条分支要用解码后的数组跑卡车模板匹配。

    抽 `_decode_image` 出来时把局部变量 `arr` 一并消掉过，于是 60 行之外那句
    `_truck_present(arr)` 静静变成 NameError：上传京东截图必崩 500。
    而闲鱼截图（占绝大多数）走的是 else 分支，本地怎么点都试不出来——
    所以这条链路必须有测试，不能只测纯解析层。
    """
    _stub_recognize(monkeypatch, ["京东自营 官方旗舰店", "订单编号 1234567890123456"])
    f = ocr.recognize_order(b"fake")
    # 从「拒识」改成了「警示」：平台如实标出来（不再置空），结果照常给，
    # 只是附一句提醒，由前端问一次「确定继续吗」。
    # 置空 platform 的老做法有个副作用：用户确认继续之后建出来的是一张**没有来源**的单，
    # 而我们明明认出来了。
    assert f["platform"] == "京东"
    assert "京东" in f["platform_warning"]
    assert f["order_no"] == "1234567890123456", "警示归警示，字段该解析的还得解析出来"


def test_recognize_order_accepts_xianyu(monkeypatch):
    """反面：闲鱼截图不该带任何警示（否则每张图都要确认一次，等于把功能废了）。"""
    _stub_recognize(monkeypatch, ["闲鱼", "订单编号 1234567890123456", "成交价 ¥88.00"])
    f = ocr.recognize_order(b"fake")
    assert f["platform"] == "闲鱼" and f["platform_warning"] is None
    assert f["order_no"] == "1234567890123456"


def test_recognize_order_passes_the_decoded_array_to_the_truck_matcher(monkeypatch):
    """卡车模板匹配拿到的必须是**解码后的数组**，不是别的东西。"""
    from app.services import ocr as m

    seen = []
    monkeypatch.setattr(m, "_get_engine", lambda: object())
    monkeypatch.setattr(m, "_decode_image", lambda b: "DECODED")
    monkeypatch.setattr(m, "_run_engine", lambda eng, arr: [tok("天猫旗舰店")])
    monkeypatch.setattr(m, "_truck_present", lambda arr: (seen.append(arr), False)[1])
    ocr.recognize_order(b"fake")
    assert seen == ["DECODED"], f"_truck_present 收到的不是解码结果：{seen}"


# --- 拿错平台：提醒而不是拒收 ----------------------------------------------------

def test_ocr_endpoint_tells_the_frontend_whether_a_plugin_covers_that_platform(
        client, monkeypatch):
    """认出不是闲鱼时，端点要顺带回答「这台机器上有没有插件在管这个平台」。

    提示词分两种——「已经装了淘宝插件，用它更准，仍要 OCR 吗」与
    「没有插件在管，OCR 是唯一的路」——该说哪一句取决于用户装了什么、配了哪些账号。
    这件事**只有后端知道**；让前端自己去比对就得在前端写一份平台↔插件的对应关系，
    而 `test_frontend_does_not_hardcode_any_plugin_id` 正是不许那么做。
    """
    from app.routers import orders as mod

    async def fake_ocr(file, recognizer):
        return {"platform": "淘宝", "platform_warning": "这看起来是淘宝的截图…",
                "order_no": "TB-1"}

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    got = client.post("/api/orders/ocr",
                      files={"file": ("x.png", b"\x89PNG", "image/png")}).json()
    assert got["platform"] == "淘宝", "平台被置空了——确认继续后会建出一张没有来源的单"
    assert "platform_plugin" in got, "没告诉前端有没有插件在管，提示词只能靠猜"


def test_platform_provider_reads_account_platforms_not_plugin_ids(session, monkeypatch):
    """「谁在管这个平台」的判据是**账号上配的 platform**，不是插件 id。

    核心不认识任何具体插件（test_core_does_not_hardcode_any_plugin_id 钉着这条）；
    账号的平台本来就是用户添加账号时选的「这个号抓的是哪个平台」。
    """
    import json

    from app.models import PluginConfig
    from app.routers import plugins as mod

    fake = {"_m": type("M", (), {"id": "demo", "name": "演示插件"})(), "_dir": None}
    monkeypatch.setattr(mod, "discover", lambda: [fake])

    # 账号存在 params_json 的 `accounts` 键里（不是单独一列）——
    # 这一点写错的话，platform_provider 恒返回空串，而它是**沉默**的：
    # 提示词会永远走「没有插件在管」那一支，用户装没装插件都一样。
    cfg = PluginConfig(plugin_id="demo", enabled=True, params_json=json.dumps(
        {"accounts": [{"name": "a", "platform": "淘宝", "enabled": True}]}))
    session.add(cfg)
    session.commit()
    try:
        assert mod.platform_provider(session, "淘宝") == "演示插件"
        assert mod.platform_provider(session, "京东") == "", "不该给一个没人管的平台报插件名"
        assert mod.platform_provider(session, "") == ""
        # 插件停用了就不算「有人在管」——停用状态下它一次都不会跑
        cfg.enabled = False
        session.add(cfg)
        session.commit()
        assert mod.platform_provider(session, "淘宝") == "", "插件已停用却仍报它在管这个平台"
    finally:
        session.delete(session.get(PluginConfig, "demo"))
        session.commit()


@pytest.mark.parametrize("tokens,want_platform,want_warn", [
    # ① 明确认出别的平台
    (["京东自营 官方旗舰店", "订单编号 1234567890123456"], "京东", True),
    # ② 有闲鱼线索 → 闲鱼，不警示
    (["闲鱼", "订单编号 1234567890123456"], "闲鱼", False),
    # ③ 两边证据都没有 → **承认不知道**，platform 留空
    (["订单编号 1234567890123456", "签收时间 2026-03-01"], None, True),
])
def test_platform_has_three_outcomes_including_dont_know(
        monkeypatch, tokens, want_platform, want_warn):
    """**不带 hint 时**平台判别有三个出口，不是两个。（用户指定来源那条第四路见下面几条。）

    原先只有「别的平台」和「闲鱼」：没有任何证据时无条件写死 platform="闲鱼"。
    于是一张淘宝截图只要没出现 京豆/白条/天猫/旺旺/官方旗舰店 这几个词，
    就会被**信誓旦旦地**标成闲鱼——用户在列表里看到「闲鱼」，没有任何理由去怀疑。
    而来源是要进账本的，还参与订单唯一键（order_no + COALESCE(platform,'')）：
    标错来源意味着将来插件抓回同一笔单会新建一行，同一笔交易变成两条。

    「猜错了还一脸笃定」比「说不知道」贵得多——后者用户点一下就改了，
    而留空是被支持的状态（唯一索引走 COALESCE，同订单号照样去重）。
    """
    _stub_recognize(monkeypatch, tokens)
    f = ocr.recognize_order(b"fake")
    assert f["platform"] == want_platform
    assert bool(f["platform_warning"]) is want_warn
    # 无论哪一支，字段该解析的都得解析出来——警示归警示，不是拒识
    assert f["order_no"] == "1234567890123456"


def test_ocr_writes_to_staging_not_straight_into_the_ledger():
    """OCR 的识别结果必须先落**暂存**，不许直接写账本。

    这是这套东西里唯一一条能一句话说完的规则：**机器认的一律先落暂存，人点导入才进账本。**
    插件那边早就是这样了（权限里刻意不给 staging:promote，就是为了让卡片上
    「插件只管抓取，抓到的单必须经人工确认才进账本」这句话是真的）；
    而 OCR 是两个生产者里**更不可靠**的那个——来源会标错、非闲鱼版式上成交价和商品名
    直接认不出来——却曾经绕过这道关卡直接写正式账本。

    那条「拿错平台要不要继续」的确认框也随之取消了：整张暂存表就是复核界面，
    在那儿改比在弹窗里选自然得多。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src" / "views"
    staging = (root / "Staging" / "index.vue").read_text(encoding="utf-8")
    orders = (root / "Orders" / "index.vue").read_text(encoding="utf-8")

    assert "ordersApi.ocr(" in staging, "暂存页没有 OCR 入口"
    assert "stagingApi.create" in staging or "addRow(" in staging, "暂存页的 OCR 没有写入口"
    # 订单页不许再有任何 OCR 通路
    for gone in ("OcrButton", "ordersApi.ocr(", "processOcr", "enqueueOcr"):
        assert gone not in orders, f"订单页还留着 OCR 通路：{gone}（识别结果应当先落暂存）"
    # 暂存页的落点必须是暂存，不能是账本
    assert "ordersApi.create" not in staging, "暂存页的 OCR 直接建了账本单，绕过了人工确认"


# --- 快递公司：宁可留空，不许从装饰词猜出来 --------------------------------------

@pytest.mark.parametrize("page_text", [
    "顺丰包邮 全新未拆",          # 商品标题里的「顺丰」
    "京东自营 官方旗舰店",         # COMPANY_MAP 里有「京东」→ 京东物流
    "cute items for systems",     # 英文标题里的裸子串 ems → EMS
    "菜鸟驿站代收",               # 实际承运可能是别家
])
def test_company_is_not_invented_without_a_tracking_number(page_text):
    """页面上没有物流行时，**不许**只凭一个公司词就填出快递公司。

    原先兜底那一支写着「只认出公司、没有单号：仍然报公司」，没有任何锚点约束：
    商品标题里的「顺丰包邮」、英文标题里的 it`ems`/syst`ems`、京东截图上的「京东自营」
    都会凭空造出一个 express_company——看起来完全合理，实际是从装饰词猜的，
    而它会落进「快递公司」列并参与标签归组。

    没有快递号就是没认出物流信息。留空比编一个强——留空看得出来，编的看不出来。
    """
    page = row(50, page_text) + row(120, "订单编号", "1234567890123456")
    out = ocr.parse_order_fields(page)
    assert out["express_no"] is None, "用例前提不成立：这一页不该有快递号"
    assert out["express_company"] is None, f"从 {page_text!r} 里猜出了快递公司"


def test_company_still_comes_along_when_there_is_a_real_tracking_row():
    """反面：有真的物流行时，公司名照常取——这道闸不是把功能关掉。"""
    page = row(50, "快递单号", "顺丰速运 SF1234567890123")
    out = ocr.parse_order_fields(page)
    assert out["express_no"] == "SF1234567890123"
    assert out["express_company"] == "顺丰速运"


def test_nearest_company_does_not_reach_across_the_page():
    """标签行不含公司名时，只在**附近几行**里找，不许从页面另一头捞。

    第二段原先是全页按 y 距离排序取最近的一个、没有距离上限：
    一张只有单号没有公司名的物流行，会把几百像素外商品标题里的「顺丰包邮」认成承运商。
    """
    page = (row(50, "顺丰包邮 全新未拆")                      # 页面顶部的商品标题
            + row(900, "快递单号", "78912345678901"))         # 很远的地方才是真物流行
    out = ocr.parse_order_fields(page)
    assert out["express_no"] == "78912345678901"
    assert out["express_company"] is None, "从页面另一头把标题里的公司词捞回来了"


# --- 锚点：取不到值要继续找下一个，不能在第一个上 break ---------------------------

@pytest.mark.parametrize("field,head,value_row,want", [
    ("order_no", "订单号", ("订单编号", "1234567890123456"), "1234567890123456"),
    ("order_date", "下单时间", ("下单时间", "2026-03-01"), "2026-03-01"),
])
def test_a_bare_label_row_does_not_swallow_the_anchor(field, head, value_row, want):
    """页面上第一个含该关键词的框是**列头/筛选栏/分组标题**（同行没有值）时，
    必须继续找下一个，不能在它身上 break。

    在那儿 break 的话真正那一行永远轮不到，字段静默为空。
    集运侧早就用 `_first` 修过这个坑并写了注释，商品订单侧三段却一直留着——
    现在两边共用 `_first_anchor`。
    """
    page = row(40, head) + row(200, *value_row)      # 第一行是光秃秃的标题，没有值
    out = ocr.parse_order_fields(page)
    assert out[field] == want, f"{field} 被空的标题行吃掉了"


def test_bare_price_label_does_not_kill_product_too():
    """成交价的锚点被空标题行吃掉时，**商品名会跟着一起丢**——
    `_parse_product` 靠「成交价上方那行挂牌价」定位，没有锚点就直接返回 None。
    所以这一段的 break 比另外两段更贵。"""
    page = (row(40, "成交价")                                  # 分组标题，同行无金额
            + row(200, "全新未拆 星星灯")
            + row(260, "挂牌价", "¥99.00")
            + row(320, "成交价", "¥88.00"))
    out = ocr.parse_order_fields(page)
    assert out["price_cny"] == "88.00", "成交价被空的标题行吃掉了"


# --- 用户在上传时指定来源 -------------------------------------------------------

@pytest.mark.parametrize("tokens,hint", [
    (["京东自营 官方旗舰店", "订单编号 1234567890123456"], "闲鱼"),   # 图上是京东，人说闲鱼
    (["闲鱼", "订单编号 1234567890123456"], "淘宝"),                  # 图上是闲鱼，人说淘宝
    (["订单编号 1234567890123456"], "拼多多"),                       # 图上什么证据都没有
])
def test_user_hint_always_wins_over_the_evidence(monkeypatch, tokens, hint):
    """用户在上传时选了来源 → **platform 一定是它**，图上的证据一律推翻不了。

    这不是「尊重用户」的客气话，是因为 platform 进的是账本活跃行唯一键
    （`ix_orders_order_no_platform_active` 含 `COALESCE(platform,'')`）。
    一张认不出的淘宝截图落成 platform=NULL，淘宝插件之后抓到同一单写「淘宝」，
    **同一笔交易变成两行**。让证据推翻用户不是气人，是直接制造重复行——
    而消掉这类重复正是「上传时先问平台」这件事的第一价值。
    """
    _stub_recognize(monkeypatch, tokens)
    assert ocr.recognize_order(b"fake", platform_hint=hint)["platform"] == hint


def test_hint_conflicting_with_the_evidence_still_says_so(monkeypatch):
    """人说闲鱼、图上却是京东 → 照样标闲鱼，但**得说一句**。

    冲突信息有价值：批量选了某个平台、里面混进一张别家的截图，
    这时候提醒一句就能让人回去核对，而不是等到账本里对不上账才发现。
    """
    _stub_recognize(monkeypatch, ["京东自营 官方旗舰店", "订单编号 1234567890123456"])
    f = ocr.recognize_order(b"fake", platform_hint="闲鱼")
    assert f["platform"] == "闲鱼"
    assert "京东" in (f["platform_warning"] or ""), f["platform_warning"]


def test_hint_of_xianyu_without_conflict_is_silent(monkeypatch):
    """人说闲鱼、图上也是闲鱼 → 一个字都不说。

    解析规则本来就是照闲鱼版式写的，这一支没有任何要提醒的。
    没有这条，「凡是带 hint 就唠叨一句」也能让上面几条全绿，而那会让每一批
    正常的闲鱼截图都无端多出一行警告——警告一旦变成常态就没人看了。
    """
    _stub_recognize(monkeypatch, ["闲鱼", "订单编号 1234567890123456"])
    assert ocr.recognize_order(b"fake", platform_hint="闲鱼")["platform_warning"] is None


def test_non_xianyu_hint_warns_that_the_rules_are_still_xianyu_shaped(monkeypatch):
    """人说淘宝/京东 → 必须**如实说清**：解析规则今天只有闲鱼一套。

    `parse_order_fields` 里零个平台分支，锚点全是写死的闲鱼字面量
    （「成交价」是闲鱼独有的，淘宝/京东叫「实付款」；而商品名寄生在成交价锚点上，
    价格丢了商品名一起丢）。所以选「淘宝」**不会**让识别变准。
    这条守卫钉住的就是「别让那个下拉框看起来像是能加载淘宝解析规则」。
    """
    _stub_recognize(monkeypatch, ["订单编号 1234567890123456"])
    w = ocr.recognize_order(b"fake", platform_hint="淘宝")["platform_warning"]
    assert w and "闲鱼" in w, w


def test_a_xianyu_screenshot_tagged_otherwise_is_not_told_it_cannot_be_parsed(monkeypatch):
    """人选了淘宝，但图上有**明确的闲鱼特征** ⇒ 解析规则恰恰是对的，别说反话。

    这一支原先不存在：非闲鱼 hint 一律发「成交价与商品名多半认不出来，请在暂存表里补」。
    而 `_stamp_platform` 自己的 docstring 写着保留检测的理由正是
    「批量选了淘宝、里面混进一张闲鱼截图，**那张的解析其实是准的**」——
    文案把这条理由否掉了。批量拖 12 张选淘宝、混 2 张闲鱼，
    汇总弹窗就写「其中 12 张不是闲鱼版式、多半认不出来」，而那 2 张是准的。

    仍然要保持 truthy（上一条守卫的理由：路由靠它查 platform_plugin、前端靠它计数）。
    """
    # 「成交价」是 _XIANYU_CUES 里的一条，同时也是解析锚点——有它就是闲鱼版式
    _stub_recognize(monkeypatch, ["订单编号 1234567890123456", "成交价 ¥100.00"])
    w = ocr.recognize_order(b"fake", platform_hint="淘宝")["platform_warning"]
    assert w, "警告不能置空，否则 platform_plugin 与 offPlatform 整条链一起失效"
    assert "认不出" not in w, f"明明是闲鱼版式却说认不出来：{w}"
    assert "闲鱼" in w and "淘宝" in w, w

    # **反面**：真的没有闲鱼特征时，那句「多半认不出来」必须还在
    _stub_recognize(monkeypatch, ["订单编号 1234567890123456"])
    w2 = ocr.recognize_order(b"fake", platform_hint="淘宝")["platform_warning"]
    assert "认不出" in w2, w2


def test_warning_stays_truthy_for_every_hint_that_is_not_xianyu(monkeypatch):
    """带 hint 时 `platform_warning` 必须保持 truthy（闲鱼无冲突那一支除外）。

    路由只有在它为真时才去查 `platform_plugin`，前端也只有在它为真时才计 offPlatform
    并在批次汇总里出那一行。把它置成 None「省事」会**静默拆掉**整条既有链路：
    插件推荐不再出现、汇总少一行，而没有任何测试直接盯着那句话。
    """
    _stub_recognize(monkeypatch, ["订单编号 1234567890123456"])
    for hint in ("淘宝", "京东", "拼多多", "其他"):
        assert ocr.recognize_order(b"fake", platform_hint=hint)["platform_warning"], hint


def test_hint_skips_the_expensive_truck_match(monkeypatch):
    """带 hint 时**不许**去跑卡通卡车模板匹配。

    `_truck_present` 是全图 8 尺度 matchTemplate，一张 1080×2400 就是秒级。
    用户已经说了这是什么平台，再花一秒去图里找卡车纯属白烧——批量拖十几张时
    省下的就是十几秒。它只是用来**猜**闲鱼的，而猜这件事已经不需要了。
    """
    called = []
    _stub_recognize(monkeypatch, ["订单编号 1234567890123456"])
    # ⚠️ 探针必须设在 `_stub_recognize` **之后**——它自己就把 `_truck_present` 打了桩，
    #    设在前面会被它当场覆盖，于是这条守卫永远不会红（第一版正是这样，
    #    破坏性验证时「带 hint 还去跑模板匹配」纹丝不动才发现）。
    monkeypatch.setattr(ocr, "_truck_present", lambda arr: called.append(1) or False)
    ocr.recognize_order(b"fake", platform_hint="淘宝")
    assert not called, "带 hint 还去跑了模板匹配"

    # 反面：不带 hint 时它**必须**被调到。没有这一半，把 `_truck_present` 整个删掉
    # 也能让上面那句绿——而它是闲鱼判别的第二根支柱。
    called.clear()
    ocr.recognize_order(b"fake")
    assert called, "不带 hint 时反而不看图了——闲鱼的兜底信号没了"


def test_no_hint_behaves_exactly_as_before(monkeypatch):
    """不传 hint ⇒ 与加这个参数之前**逐字节相同**——三条自动判别的出口一条不少。

    这是整条改动的验收条件：新参数是**加法**，不是改法。
    上面那条参数化的三态测试是主证据，这里补的是「单参调用仍然合法」——
    默认值一旦被拿掉，是 TypeError 而不是行为变化，红得早、也红得明白。
    """
    _stub_recognize(monkeypatch, ["闲鱼", "订单编号 1234567890123456"])
    assert ocr.recognize_order(b"fake")["platform"] == "闲鱼"


def test_the_route_actually_threads_the_hint_into_the_recogniser(client, monkeypatch):
    """路由收下的 `platform_hint` 必须**真的传进识别函数**。

    这是整条链上最安静的一处失败：对话框弹了、用户选了、图传上去了、HTTP 200、
    批次汇总照常显示——而那句选择在路由里蒸发，识别照旧靠猜。没有任何东西会报错。
    上面那些测试全都直接调 `recognize_order`，绕过了路由，**一条也盖不住这里**
    （破坏性验证时把 `functools.partial(...)` 换回裸 `recognize_order`，全绿）。
    """
    from app.routers import orders as mod
    from app.services import ocr as ocr_mod

    seen = {}
    def spy(image_bytes, platform_hint=None):
        seen["hint"] = platform_hint
        return {"order_no": "X"}

    monkeypatch.setattr(ocr_mod, "recognize_order", spy)

    async def fake_ocr(file, recognizer):
        return recognizer(b"fake")     # 真的把路由造的那个 recognizer 调起来

    monkeypatch.setattr(mod, "run_ocr", fake_ocr)
    client.post("/api/orders/ocr", data={"platform_hint": "淘宝"},
                files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert seen.get("hint") == "淘宝", f"来源在路由里蒸发了：{seen}"

    seen.clear()
    client.post("/api/orders/ocr", files={"file": ("x.png", b"\x89PNG", "image/png")})
    assert seen.get("hint") is None, "没选来源却凭空传了一个进去"


# --- 闲鱼卡车模板匹配：此前**零测试**覆盖真实匹配 -------------------------------

def _truck_canvas(seed, w=1080, h=2400, paste=None):
    """造一张「像截图」的灰度图：浅底 + 横向文本条。paste=(尺度) 时贴一只卡车进去。

    不用纯白：纯白上任何模板的相关分都是 0，测不出噪声底——而噪声底正是这里的要害。
    固定 seed 重建，保证同一条断言每次跑的是同一张图。
    """
    import cv2
    import numpy as np

    rng = np.random.default_rng(seed)
    g = np.full((h, w), 245, np.uint8)
    for y in range(0, h, 37):
        g[y:y + 2, 20:w - 20] = rng.integers(60, 200)
    for _ in range(h // 20):
        y, x = rng.integers(0, h - 12), rng.integers(0, max(1, w - 90))
        g[y:y + 11, x:x + 80] = rng.integers(30, 120)
    if paste:
        ref = ocr._load_truck_ref()
        t = cv2.resize(ref, (int(ref.shape[1] * paste), int(ref.shape[0] * paste)),
                       interpolation=cv2.INTER_AREA)
        g[100:100 + t.shape[0], 50:50 + t.shape[1]] = t
    return g


@pytest.mark.parametrize("scale", [1.0, 0.6, 0.4, 0.25])
def test_truck_is_found_at_every_declared_scale(scale):
    """`_TRUCK_SCALES` 里声明的每个尺度都必须真能认出卡车。

    这条链此前**一条测试都没有**：`_truck_score` 是「截图是不是闲鱼」的第二根支柱
    （文案线索答不上来时才问它），而它的阈值、尺度表、下界三个常数从来没被任何东西钉住。
    改动它们（比如为了提速缩图）会不会把真卡车漏掉，以前只能靠肉眼。
    """
    pytest.importorskip("cv2")
    g = _truck_canvas(7, paste=scale)
    got = ocr._truck_score(g)
    assert got >= ocr._TRUCK_MATCH_THRESHOLD, \
        f"尺度 {scale} 的卡车没认出来：{got:.3f} < {ocr._TRUCK_MATCH_THRESHOLD}"


def test_a_screenshot_without_a_truck_stays_below_the_threshold():
    """反面：没有卡车的截图不许越过阈值。

    **假阳比慢贵得多**：`_stamp_platform` 里卡车命中会**压过**明确的淘宝/京东标记，
    把 platform 定成「闲鱼」且 `platform_warning=None`（自信地错、且不给任何提示），
    随后路由按 warning 判是否推荐插件 → 跳过；而「闲鱼」还会写进
    `COALESCE(platform,'')` 唯一键，插件之后抓同一单就变成重复行。
    """
    pytest.importorskip("cv2")
    for seed in (7, 11, 42):
        got = ocr._truck_score(_truck_canvas(seed))
        assert got < ocr._TRUCK_MATCH_THRESHOLD, f"seed={seed} 无卡车却判命中：{got:.3f}"
    # 余量也要钉住，不然阈值被人调回去时这条依然绿。
    # `_truck_canvas` 造的是**等间距横条**——负样本里最凶的一类（周期结构与模板共振，
    # 实测 0.555/0.579/0.608，而类文本行只有 0.550、卡片色块 0.469、纯噪声 0.07）。
    worst = max(ocr._truck_score(_truck_canvas(s)) for s in (7, 11, 42))
    assert ocr._TRUCK_MATCH_THRESHOLD - worst >= 0.15, \
        f"阈值 {ocr._TRUCK_MATCH_THRESHOLD} 距最凶的负样本只剩 {ocr._TRUCK_MATCH_THRESHOLD - worst:.3f}"


@pytest.mark.slow
def test_the_noise_floor_on_a_huge_screenshot_is_measured_not_assumed():
    """**超大图上的噪声底几乎贴着阈值**——这条把那个事实钉下来，别再靠猜。

    `TM_CCOEFF_NORMED` 取的是全图窗口的**最大**相关分，窗口数随面积线性涨，
    于是「最大值」的噪声底也跟着涨。实测 2000×20000（= `MAX_OCR_PIXELS` 上限）的
    合成负样本：**0.591 ~ 0.609**，而阈值是 0.60——余量只有 0.01 量级，已经有一个种子越线。

    这直接否掉了一类「为了提速先把图缩小再匹配」的改法：同图 A/B 实测（三个种子一致）
    缩到 4 Mpx 后耗时 17.7s → 1.5s，**但负样本分从 0.591~0.609 涨到 0.656~0.661**——
    图缩了模板也跟着缩，小模板更容易匹配噪声。快了 12 倍，代价是把仅剩的余量吃光。
    要提速得另想办法（提高阈值、只在没有文案线索时才跑、或换特征而不是模板匹配）。

    标 slow：单次约 18 秒。默认不跑，改动那三个常数时手动跑
    `pytest -m slow tests/test_ocr_parse.py -k noise_floor`。
    """
    pytest.importorskip("cv2")
    got = ocr._truck_score(_truck_canvas(7, w=2000, h=20000))
    assert got < 0.75, f"超大图噪声底已经高到 {got:.3f}，模板匹配这条路要重新设计"


def test_the_smallest_declared_scale_stays_above_the_sanity_bound():
    """尺度表里最小的那一档，缩出来的模板必须还在「太小无意义」那道下界之上。

    现状：最小尺度 0.25 × 参考图 317×243 = 79×60，而下界是 24×18——**那道下界目前不可达**，
    是给将来加更小尺度时留的。这条把两者的关系钉住：谁往 `_TRUCK_SCALES` 里加一档
    小到会被下界砍掉的尺度，就会在这里被告知「加了也不会跑」，
    而不是让它静默地不生效（那种失败方式在这个仓库已经出现过很多次）。

    **不要**为了让小尺度跑起来而去调低下界：模板越小越容易匹配噪声，
    实测负样本噪声底会随之抬高，而阈值那 0.19 的余量经不起这么花（见上一条的说明）。
    """
    rh, rw = ocr._load_truck_ref().shape[:2]
    s = min(ocr._TRUCK_SCALES)
    w, h = int(rw * s), int(rh * s)
    assert w >= 24 and h >= 18, \
        f"最小尺度 {s} 缩出 {w}×{h}，会被 _truck_score 里那道 24×18 的下界直接跳过——加了等于没加"


# --- 内存不足不许被说成「图片坏了」 ---------------------------------------------
#
# `MemoryError` 是 `Exception` 的子类，会先被解码/推理那两处的 `except Exception` 接住、
# 转成 `ValueError` → 路由映射成 400「图片无法解析：」——而 `str(MemoryError())` 是**空串**，
# 用户拿到的是一句以冒号结尾、后面什么都没有的话，含义还是「你这张图坏了」。
# 路由里那个带 `Retry-After` 的 503「服务器内存不足，请稍后重试」因此**不可达**，
# 日志里也不会留下任何痕迹——而解码与推理恰恰是仅有的两个会 OOM 的位置。
#
# 拆成两条：推理那一半不依赖任何可选包，任何环境下都要跑；
# 解码那一半要真有 pillow 才测得了（OCR 依赖是可选的，本仓 venv 里常常没装）。
# 合成一条的话，没装 pillow 的环境会把不需要它的那一半一起跳过。

def test_engine_out_of_memory_is_not_disguised_as_a_broken_image():
    class _Oom:
        def __call__(self, arr):
            raise MemoryError()

    with pytest.raises(MemoryError):
        ocr._run_engine(_Oom(), None)

    # **反面**：别的异常仍要转成 ValueError（路由映射成 400「这张图有问题」）。
    class _Bad:
        def __call__(self, arr):
            raise RuntimeError("畸形图")

    with pytest.raises(ValueError):
        ocr._run_engine(_Bad(), None)


def test_decode_out_of_memory_is_not_disguised_as_a_broken_image(monkeypatch):
    Image = pytest.importorskip("PIL.Image", reason="解码分支需要 pillow")

    def _oom(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(Image, "open", _oom)
    with pytest.raises(MemoryError):
        ocr._decode_image(b"whatever")


# --- 日期：模块声明了哪些破折号，就得认哪些 -------------------------------------

def test_dates_survive_every_dash_the_module_declares():
    """`_extract_date` 原先在字符类里**自己又抄了一份**破折号表（`[-/.–—]`），
    只覆盖 `_DASHES` 七个里的两个。漏掉的 `－`（U+FF0D 全角连字符）恰恰是中文界面
    OCR 最常吐出来的那个 ⇒ `2026－07－19` 返回 None ⇒ 下单时间/订单时间**静默为空**，
    用户不知道为什么这张图的日期没认出来。

    这条测试**从 `_DASHES` 自己生成用例**，不再抄第三份——加一个破折号就自动被覆盖。
    """
    for d in ocr._DASHES:
        got = ocr._extract_date(f"下单时间 2026{d}07{d}19")
        assert got == "2026-07-19", f"不认 {d!r}（U+{ord(d):04X}）：{got}"
    for sep in "-/.":
        assert ocr._extract_date(f"下单时间 2026{sep}07{sep}19") == "2026-07-19", sep
    # **反面**：日历上不存在的日期仍要挡掉（别把判据改成「凑够三段数字就行」）
    assert ocr._extract_date("2026-02-31") is None
    assert ocr._extract_date("没有日期") is None


def test_truck_template_is_not_marked_loaded_until_it_actually_is(monkeypatch):
    """`_truck_ref_loaded` 原先是**加载之前**就置 True 的：并发的另一路请求会看到
    「已加载 + _truck_ref is None」⇒ 那一张的卡车信号凭空消失，而它恰恰是文案兜不住时
    唯一的闲鱼证据（结果是无线索的闲鱼截图被判成「认不出是哪个平台」）。
    """
    cv2 = pytest.importorskip("cv2", reason="卡车模板要 opencv")

    seen = {}
    real = cv2.imdecode

    def spy(*a, **k):
        seen["flag_during_load"] = ocr._truck_ref_loaded
        return real(*a, **k)

    monkeypatch.setattr(cv2, "imdecode", spy)
    monkeypatch.setattr(ocr, "_truck_ref_loaded", False)
    monkeypatch.setattr(ocr, "_truck_ref", None)

    ocr._load_truck_ref()
    assert seen.get("flag_during_load") is False, \
        "加载还没做完就置了「已加载」——并发的另一路会拿到 None"
    assert ocr._truck_ref_loaded is True, "加载完了却没置位，每次都要重读一遍文件"


# --- 向下兜底只认「光秃秃的值」 -------------------------------------------------

def test_a_bare_label_does_not_steal_the_neighbouring_fields_value():
    """页面上只有一个光秃秃的「订单号」列头（列头/筛选栏/灰色分组标题都长这样），
    下一行是页脚的 `客服电话 13800138000` ⇒ 向下兜底原先把手机号取成订单号。

    而 `order_no` 是暂存去重键、也是账本活跃唯一键的一半：同一批里两张截图
    兜底到同一个页脚号码，第二张会被**并进第一行**，两笔交易塌成一条。
    `_BELOW_ROWS` 那道距离上限挡的是「跨半页抓」，挡不住「紧邻下一行是别人的字段」。
    """
    res = [tok("订单号", x=100, y=40), tok("客服电话 13800138000", x=100, y=70)]
    assert ocr.parse_order_fields(res)["order_no"] is None

    # **反面**：真正该支持的表格版式（列头在上、值在下一行）必须照常取到，
    # 否则这道闸就是把「列头在上」那种版式整页打空。
    res2 = [tok("订单号", x=100, y=40), tok("1234567890123456", x=100, y=70)]
    assert ocr.parse_order_fields(res2)["order_no"] == "1234567890123456"

    # 同行取值不受影响（这条闸只作用在向下兜底那一支）
    res3 = [tok("订单号", x=100, y=40), tok("客服电话 13800138000", x=100, y=400),
            tok("9876543210987654", x=340, y=40)]
    assert ocr.parse_order_fields(res3)["order_no"] == "9876543210987654"


# --- 「一个列头 + 下面一列号」的内含快递页 -------------------------------------

def test_a_column_of_express_numbers_under_one_header_is_read_in_full():
    """内含快递页存在「一个列头『快递单号』+ 下面 N 行号」的表格版式
    （`_same_row_value` 的 `_BELOW_ROWS` 兜底就是为它留的）。

    但取值只取**最近的一个**，而那个循环又只遍历「文本里含『快递单号』的框」
    ⇒ 三行号只出一个，剩下两个**连看都没看**，且 `unreadable` 仍是 0
    ⇒ 前端弹出的是纯绿色的「已关联 1 单」，用户没有任何线索知道少了两单。
    而 `unreadable` 在 schemas 里被写成「少挂一单在响应里的唯一出口」——
    它在这个版式下恰好失效。
    """
    res = [tok("快递单号", x=100, y=200),
           tok("SF1111111111", x=100, y=226),
           tok("YT2222222222", x=100, y=252),
           tok("ZT3333333333", x=100, y=278)]
    f = ocr.parse_shipment_fields(res)
    assert f["express_nos"] == ["SF1111111111", "YT2222222222", "ZT3333333333"], f
    assert f["unreadable"] == 0

    # **反面一**：列到「下一个字段的标签」为止，不许越界一路抓下去。
    res2 = [tok("快递单号", x=100, y=200),
            tok("SF1111111111", x=100, y=226),
            tok("客服电话 13800138000", x=100, y=252),
            tok("YT9999999999", x=100, y=278)]
    assert ocr.parse_shipment_fields(res2)["express_nos"] == ["SF1111111111"]

    # **反面二**：逐行自带标签那种版式不受影响（锚点自己有值，走不到列头那一支）
    res3 = [tok("快递单号 SF1111111111", x=100, y=200),
            tok("快递单号 YT2222222222", x=100, y=226)]
    assert ocr.parse_shipment_fields(res3)["express_nos"] == ["SF1111111111", "YT2222222222"]

    # **反面三**：隔得远的那些不算一列（行距超过 _BELOW_ROWS 就停）
    res4 = [tok("快递单号", x=100, y=200),
            tok("SF1111111111", x=100, y=226),
            tok("YT2222222222", x=100, y=900)]
    assert ocr.parse_shipment_fields(res4)["express_nos"] == ["SF1111111111"]


def test_a_shipping_fee_row_is_not_mistaken_for_the_product_name():
    """商品名取的是「成交价上方最近的带 ¥ 行」——纯几何判据。
    标题与成交价之间夹一行运费时，取到的是**「运费」**：

        全新未拆 星星灯 手办   ¥520.00
        运费                  ¥12.00      ← 原先这一行被当成商品标题
        成交价                ¥500.00

    而 `product` 会被前端写成暂存行的商品名，并成为 `build_items` 的兜底物品名。
    """
    res = [tok("全新未拆 星星灯 手办", x=100, y=100), tok("¥520.00", x=400, y=100),
           tok("运费", x=100, y=140), tok("¥12.00", x=400, y=140),
           tok("成交价", x=100, y=180), tok("¥500.00", x=400, y=180)]
    got = ocr.parse_order_fields(res)
    assert got["product"] == "全新未拆 星星灯 手办", got["product"]
    assert got["price_cny"] == "500.00"

    # **反面一**：没有那一行时行为不变（别把闸写成「永远往上多跳一行」）
    res2 = [tok("全新未拆 星星灯 手办", x=100, y=100), tok("¥520.00", x=400, y=100),
            tok("成交价", x=100, y=180), tok("¥500.00", x=400, y=180)]
    assert ocr.parse_order_fields(res2)["product"] == "全新未拆 星星灯 手办"

    # **反面二**：商品名里**含**这些词时不许被误伤（判据是整段中文都是标签才跳过）
    res3 = [tok("运费险专用测试品", x=100, y=100), tok("¥520.00", x=400, y=100),
            tok("成交价", x=100, y=180), tok("¥500.00", x=400, y=180)]
    assert ocr.parse_order_fields(res3)["product"] == "运费险专用测试品"
