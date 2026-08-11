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
    ("快递单号 SF1111111111 2026-08-01 已签收", "SF1111111111"),   # 日期用连字符，不是断号
    ("快递单号 12345678", "12345678"),        # 孤立的短号：可疑但没有断开的证据，照取
    ("快递单号 4312345678901", "4312345678901"),
    ("收件地址 ATPTSTKH", None),              # 纯字母串
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
    """平台判别有**三个**出口，不是两个。

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
