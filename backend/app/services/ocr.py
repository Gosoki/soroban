"""截图 OCR：从订单详情图里抽字段。两类截图共用同一引擎与解析原语——
- 商品订单（闲鱼）：快递公司、快递号、订单号、成交价 → recognize_order
- 集运订单：国际单号、成品包裹号、订单时间、内含快递号 → recognize_shipment

用 RapidOCR（onnxruntime，离线中文模型，pip 装、无系统二进制依赖）。引擎首次加载
模型较慢，故做进程内单例懒加载。解析靠「标签 + 几何同行关联」：先在同一文字框里
找「标签 + 值」，找不到再在同一行（y 接近）里找最近的数字/金额框。
"""

from __future__ import annotations

import datetime as dt
import io
import re
import threading
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..models import PURCHASE_TERMINAL_STATUSES, PurchaseStatus   # 状态与终态集合的唯一真相，避免与后端漂移

# 常见快递公司：关键词 → 规范全称。按「越具体越靠前」不重要（子串命中即可），
# 但保证能覆盖淘宝寄件常见家；命中后统一输出规范名，便于「快递公司」列做标签归组。
COMPANY_MAP = {
    "顺丰": "顺丰速运",
    "申通": "申通快递",
    "圆通": "圆通速递",
    "中通": "中通快递",
    "韵达": "韵达快递",
    "极兔": "极兔速递",
    "百世": "百世快递",
    "汇通": "百世快递",
    "天天": "天天快递",
    "德邦": "德邦快递",
    "京东": "京东物流",
    "宅急送": "宅急送",
    "丰网": "丰网速运",
    "菜鸟": "菜鸟裹裹",
    "邮政": "邮政快递",
    "EMS": "EMS",
    "ems": "EMS",
}

# OCR 只用于闲鱼——淘宝/京东走爬虫抓取。故 OCR 产出的来源恒为「闲鱼」；
# 仅当截图出现明确的淘宝/京东强标记且无任何闲鱼信号时，判为「拿错截图」并拒识、提示改用爬虫。
# 「成交价」是闲鱼独有的价格标签（淘宝/京东都叫「实付款」），而且本模块已经拿它当解析锚点用
# （见 parse_order_fields 的成交价那段）——复用既有锚点，不引入新的魔法字符串。
_XIANYU_CUES = ("蚂蚁森林能量", "开箱视频", "担保账户", "担保交易", "闲鱼", "收货前拍摄",
                "芝麻信用", "成交价")
# 强平台标记：出现且无闲鱼信号才判为该平台。刻意不含「淘宝/花呗」等宽泛词——闲鱼隶属淘宝，
# 其页面也可能出现「淘宝」字样，用宽泛词会误伤闲鱼截图。
# 同理**必须与 COMPANY_MAP 的快递公司名互不相交**：裸「京东」既是快递公司（京东物流）
# 又当平台标记时，「闲鱼卖家用京东快递发货」的截图会被同一次识别既解析出
# express_company='京东物流'、又打上「疑似京东订单截图」的警示——同一次识别两个结论打架。
# 「自营」同样太宽（「原厂自营」之类的商品描述会命中），收紧成「京东自营」。
# 这条不相交性由 test_platform_markers_dont_collide_with_couriers 守着。
_JD_MARKERS = ("京东自营", "京东超市", "京豆", "白条", "PLUS会员", "京享值")
_TAOBAO_MARKERS = ("天猫", "旺旺", "官方旗舰店")

# 闲鱼物流卡通卡车模板：作为文案兜不住时的补充信号（有卡车 → 闲鱼）。多尺度模板匹配，
# 相关分 ≥ 阈值即判定命中。参考图存在 services/xianyu_truck.png。
_TRUCK_REF_PATH = Path(__file__).with_name("xianyu_truck.png")
_TRUCK_MATCH_THRESHOLD = 0.60
_TRUCK_SCALES = (0.25, 0.32, 0.4, 0.5, 0.6, 0.72, 0.85, 1.0)
_truck_ref = None            # 缓存的灰度模板
_truck_ref_loaded = False

_engine = None
_engine_lock = threading.Lock()
# 识别在线程池里跑（见路由），多请求可并发到达；RapidOCR 引擎非保证可重入，
# 故用锁串行化实际推理——事件循环不被阻塞、前端可持续上传，推理逐张完成。
_infer_lock = threading.Lock()


class OcrUnavailable(RuntimeError):
    """RapidOCR 未安装/加载失败——供路由转成友好的 503。"""


def _get_engine():
    """懒加载并缓存 RapidOCR 引擎（首次约数百 ms 加载模型）。

    构造失败与「依赖没装」同等对待、一律转 OcrUnavailable（路由映射成 503 + 可读文案）。
    RapidOCR() 会按路径加载 ONNX 模型与 config.yaml——打包版尤其容易缺文件（spec 漏收、
    upx 压坏、杀软隔离），抛的是 FileNotFoundError / onnxruntime.Fail / yaml 错误。
    这些原先落在 try 之外，用户点一下「识别截图」只会得到一个裸 500。
    失败后保持 _engine 为 None，下次请求可以重试（装好依赖/补上文件即恢复，无需重启）。"""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as e:  # 依赖未装
                raise OcrUnavailable(
                    "OCR 依赖未安装，请在 backend 下执行：pip install rapidocr_onnxruntime"
                ) from e
            try:
                _engine = RapidOCR()
            except Exception as e:    # noqa: BLE001  模型文件缺失/损坏/onnxruntime 初始化失败
                raise OcrUnavailable(f"OCR 引擎加载失败（模型文件缺失或损坏）：{e}") from e
    return _engine


def _longest_digit_run(text: str, min_len: int = 1) -> Optional[str]:
    """取文本里最长的一段连续数字（长度 ≥ min_len），否则 None。"""
    runs = re.findall(r"\d+", text or "")
    if not runs:
        return None
    best = max(runs, key=len)
    return best if len(best) >= min_len else None


# 主流快递单号的长度下界：顺丰 12/15、圆通/中通/申通 12、韵达 13、EMS 13、京东 JD+13。
# 短于它的「号」本身可疑，但只有在**旁边还紧邻着另一段数字**时才判定为被 OCR 断开——
# 孤立的短号照常取，见 _looks_split。
_TRACK_TYPICAL_MIN = 10


def _looks_split(text: str, best: str) -> bool:
    """`best` 左右紧邻（只隔一个空格）还有另一段含数字的字母数字串。

    这是「这一行的号被 OCR 断成了两截」的信号。判据要求**紧邻且只隔一个空格**，
    所以同行的日期（`2026-08-01` 会被切成三段，但分隔符是连字符）、
    中文标签（不进 alnum run）都不会误触发。
    """
    m = re.search(rf"(?<![A-Za-z0-9]){re.escape(best)}(?![A-Za-z0-9])", text)
    if not m:
        return False
    return bool(re.search(r"[A-Za-z0-9]*\d[A-Za-z0-9]* $", text[:m.start()])
                or re.match(r"^ [A-Za-z0-9]*\d[A-Za-z0-9]*", text[m.end():]))


def _extract_tracking(text: str, min_len: int = 8) -> Optional[str]:
    """快递单号：可能带字母前缀（顺丰 SF、京东 JD 等），取「字母数字混合、含≥6 位数字」的
    最长串并统一大写；纯数字单号照常命中。要求含足够数字 → 排除纯字母串（如地址 ATPTSTKH）。

    **取到的号明显偏短、而旁边紧挨着另一段数字 → 返回 None（判为读不出）。**
    OCR 会把一个长号断成两截，而这里原本只是「取最长的那一段」：
        `快递单号 SF1234 56789012` → `56789012`   ← 后半截
        `快递单号 SF123456 789012` → `SF123456`   ← 前半截
    半截号会被 `shipment.py` 的 `Order.express_no == no` 拿去**精确匹配**并原子挂靠——
    匹配不上还好（只是漏一单），万一撞上别人的单号就是把货挂到无关订单上。
    口径与 `_same_row_value` 的兜底上限一致：**宁可不取，也不能取错**。
    返回 None 之后这一行会落进 `parse_shipment_fields` 的 `unreadable` 计数，
    用户会看到「另有 N 行未能读出单号，请核对」，而不是一个悄悄取错的号。
    """
    runs = re.findall(r"[A-Za-z0-9]+", text or "")
    cands = [r for r in runs if len(r) >= min_len and sum(c.isdigit() for c in r) >= 6]
    if not cands:
        return None
    best = max(cands, key=len)
    if len(best) < _TRACK_TYPICAL_MIN and _looks_split(text or "", best):
        return None
    return best.upper()


# OCR 常把 ASCII 连字符识别成各种全角/排版破折号，统一归一后再做正则匹配。
_DASHES = "–—−‒﹘﹣－"
_DASH_RE = re.compile(f"[{_DASHES}]")
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _norm_dashes(text: str) -> str:
    return _DASH_RE.sub("-", text or "")


def _extract_package_no(text: str, min_len: int = 6) -> Optional[str]:
    """成品包裹号：形如 2304513-1，**带连字符**（_extract_tracking 的 [A-Za-z0-9]+ 会把它切成
    两段，故单独一个提取器）。归一破折号后取「字母数字＋连字符」最长串，要求含 ≥6 位数字。
    显式排除 YYYY-MM-DD —— 页面上「订单时间」也满足数字条件，不排掉会被同行/下方兜底误取。"""
    runs = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", _norm_dashes(text))
    cands = [r for r in runs
             if len(r) >= min_len and sum(c.isdigit() for c in r) >= 6
             and not _DATE_LIKE_RE.match(r)]
    return max(cands, key=len).upper() if cands else None


def _to_tokens(ocr_result) -> list[dict]:
    """把 RapidOCR 结果 [[box, text, score], ...] 归一成带几何信息的 token 列表。"""
    tokens = []
    for item in ocr_result or []:
        box, text = item[0], item[1]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        tokens.append({
            "text": text or "",
            "cx": sum(xs) / len(xs),
            "cy": sum(ys) / len(ys),
            "x0": min(xs),
            "y0": min(ys),
            "y1": max(ys),
            "h": max(ys) - min(ys),
            "digits": _longest_digit_run(text or ""),      # 订单号等纯数字用
            "tracking": _extract_tracking(text or ""),     # 快递单号用（保留字母前缀）
            "package": _extract_package_no(text or ""),    # 成品包裹号用（带连字符）
        })
    return tokens


def _first_anchor(tokens: list[dict], match, pick):
    """逐个候选锚点尝试，**直到真的取到值**；返回 (锚点框, 值)，一个都取不到给 (None, None)。

    为什么不能「命中第一个含关键词的框就 break」：同一个词在页面上往往出现多次——
    列头「订单号」、筛选栏「按订单号搜索」、灰色分组标题，这些框同行都没有值。
    在它们身上 break，真正那一行**永远轮不到**，字段静默为空。

    这个坑集运侧早就踩过并修了（见 parse_shipment_fields 里原来的注释），
    但商品订单侧的订单号 / 成交价 / 下单时间三段一直是「命中即 break」。
    提到模块级共用，就不会再出现「一边修好了、另一边还留着」。

    match: (token) -> bool      哪些框算候选锚点
    pick:  (token) -> value|None 从锚点取值，取不到返回 None
    """
    for t in tokens:
        if match(t) and (v := pick(t)) is not None:
            return t, v
    return None, None


def _match_company(text: str) -> Optional[str]:
    for kw, canonical in COMPANY_MAP.items():
        if kw in text:
            return canonical
    return None


_COMPANY_NEAR_ROWS = 2      # 「就近找公司名」最多跨几行（× row_tol）


def _nearest_company(anchor: dict, tokens: list[dict], row_tol: float) -> Optional[str]:
    """标签行本身不含公司名时，在它同行、再不行就**附近几行**里找一个。

    只在**已经由真标签定位到快递号**之后调用——此时公司名只是补充信息，取错的代价远小于
    「整页第一个含公司词的框」那种把商品标题当锚点的做法。

    ⚠️ 第二段原先是**全页**按 y 距离排序取最近的一个，没有距离上限：
    快递行不含公司名时，它会从页面另一头把商品标题里的「顺丰包邮」捞回来，
    而结果看起来跟真的一样。加 `_COMPANY_NEAR_ROWS` 行的上限——超出这个范围的
    「最近」已经不构成同一条信息，宁可留空。
    """
    def near(t):
        return t is not anchor and abs(t["cy"] - anchor["cy"]) <= row_tol * _COMPANY_NEAR_ROWS

    same_row = [t for t in tokens
                if t is not anchor and abs(t["cy"] - anchor["cy"]) <= row_tol]
    for t in sorted(same_row, key=lambda t: abs(t["cy"] - anchor["cy"])):
        if (c := _match_company(t["text"])):
            return c
    for t in sorted([t for t in tokens if near(t)], key=lambda t: abs(t["cy"] - anchor["cy"])):
        if (c := _match_company(t["text"])):
            return c
    return None


_BELOW_ROWS = 2      # 「往下兜底」最多跨几行（× row_tol）；见 _same_row_value


def _same_row_value(anchor: dict, tokens: list[dict], row_tol: float,
                    min_len: int, key: str = "digits", allow_below: bool = True) -> Optional[str]:
    """在与 anchor 同一行（y 接近）里找 key 值（digits/tracking）最长的框，优先取右侧的。
    找不到同行则退回到 anchor 下方最近的框。

    `allow_below=False` 用于**内容词锚点**（如快递公司名）——真标签（「订单编号」「快递单号」）
    只会出现在它的值旁边，往下兜底是安全的；而公司名可能出现在页面任何地方（商品标题里的
    「顺丰包邮」），往下兜底会一路抓到页面下方的订单号，把它当成快递号。

    但「往下兜底是安全的」只在**紧邻下一行**成立，所以兜底带 `_BELOW_ROWS` 行的距离上限。
    没有上限时：本行的号被 OCR 断成两截（`快递单号 YT2222 333344`，两段都不满足 min_len）
    → 候选为空 → 一路向下扫**整页**，抓到页脚的客服电话当快递单号。实测能跨 2200px
    抓到 11 位手机号，而 11 位纯数字正好落在中通/韵达单号的长度区间里，
    `shipment.py` 那句 `Order.express_no == no` 会精确命中并把货挂到别人的订单上。
    上限取 2 行而不是干脆禁用兜底：真实的「内含快递」页存在「列头在上、号在下一行」的
    表格版式，禁用会把那种版式整页打空。"""
    cands = [t for t in tokens if t is not anchor and t[key]
             and len(t[key]) >= min_len]
    same_row = [t for t in cands if abs(t["cy"] - anchor["cy"]) <= row_tol]
    if same_row:
        right = [t for t in same_row if t["x0"] >= anchor["x0"]]
        pick = min(right or same_row, key=lambda t: abs(t["cy"] - anchor["cy"]))
        return pick[key]
    if not allow_below:
        return None
    below = [t for t in cands
             if 0 < t["cy"] - anchor["cy"] <= row_tol * _BELOW_ROWS]
    if below:
        return min(below, key=lambda t: t["cy"] - anchor["cy"])[key]
    return None


def _extract_date(text: str) -> Optional[str]:
    """从文本里抽出 YYYY-MM-DD（兼容 - / . 及全角连字符 – —），无则 None。"""
    m = re.search(r"(\d{4})\s*[-/.–—]\s*(\d{1,2})\s*[-/.–—]\s*(\d{1,2})", text or "")
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()   # 用真实日历校验，挡掉 2-31/4-31 等不存在的日期
    except ValueError:
        return None


def _parse_order_date(anchor: dict, tokens: list[dict], row_tol: float) -> Optional[str]:
    """下单时间：锚点框自身或同一行里取日期（避开付款/发货时间——它们各自有独立标签行）。"""
    if (d := _extract_date(anchor["text"])):
        return d
    same_row = [t for t in tokens if t is not anchor and abs(t["cy"] - anchor["cy"]) <= row_tol]
    same_row.sort(key=lambda t: t["x0"])
    for t in same_row:
        if (d := _extract_date(t["text"])):
            return d
    return None


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in (text or ""))


def _parse_product(tokens: list[dict], row_tol: float, price_anchor: Optional[dict]) -> Optional[str]:
    """商品名称：成交价上方最近的「挂牌价」所在行的中文文本即商品标题。
    （闲鱼订单页：商品图右侧一行 = 标题 + 单件挂牌价，位于「成交价」上方。）"""
    if price_anchor is None:
        return None

    def amount(text: str):
        return re.search(r"[¥￥]\s*[0-9]", text or "")

    above = [t for t in tokens if amount(t["text"]) and t["cy"] < price_anchor["cy"] - row_tol]
    if not above:
        return None
    listing = max(above, key=lambda t: t["cy"])   # 最靠近成交价的上方价 = 挂牌价行
    same_row = [t for t in tokens if t is not listing
                and abs(t["cy"] - listing["cy"]) <= row_tol and _has_cjk(t["text"])]
    if same_row:
        title = max(same_row, key=lambda t: len(t["text"]))["text"]
    else:   # 标题可能与价格合并在同一框：去掉价格片段后取剩余文本
        title = re.sub(r"[¥￥]\s*[0-9][0-9.,]*", "", listing["text"])
    title = title.strip()
    return title or None


def _parse_price(anchor: dict, tokens: list[dict], row_tol: float) -> Optional[str]:
    """成交价：同一行里找带 ¥/￥ 或形如 123.45 的金额，返回数字字符串。"""
    def amount(text: str) -> Optional[str]:
        # 允许千分位逗号（如 ¥1,234.50），否则会在逗号处截断只取到 "1"；捕获后去掉逗号
        m = re.search(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text)
        if not m:
            m = re.search(r"([0-9][0-9,]*\.[0-9]{2})", text)   # 无货币符号时认小数金额
        return m.group(1).replace(",", "") if m else None

    if (a := amount(anchor["text"])):        # 标签框自身就含金额
        return a
    same_row = [t for t in tokens if t is not anchor
                and abs(t["cy"] - anchor["cy"]) <= row_tol]
    same_row.sort(key=lambda t: t["x0"])
    for t in same_row:
        if (a := amount(t["text"])):
            return a
    return None


def _is_xianyu(full_text: str) -> bool:
    """文案里是否出现闲鱼特征线索。"""
    return any(c in full_text for c in _XIANYU_CUES)


def _detect_other_platform(full_text: str) -> Optional[str]:
    """是否出现明确的淘宝/京东强标记（用于拒识非闲鱼截图）；都没有返回 None。"""
    if any(m in full_text for m in _JD_MARKERS):
        return "京东"
    if any(m in full_text for m in _TAOBAO_MARKERS):
        return "淘宝"
    return None


_EXPRESS = "<有快递号>"   # 伪关键词：这一条不看文案，看有没有识别出快递单号

# 判交易状态的**有序**规则表：从上往下，第一条命中即返回。顺序本身是语义的一部分：
#
#  1. 终态排在最前，且**必须排在 _EXPRESS 之前**：退款单同样带快递号，
#     先判快递就会把退款单识别成「待收货」——这不是理论问题，之前正是这么错的。
#  2. 「交易成功」是闲鱼页面上的字样，就是**国内快递签收**这一刻 → 已签收。
#     国际段送达是集运单的「已送达」，两者不同名、不同表。
#
# ⚠️ 加新状态时注意**子串吞并**：靠前的关键词是靠后关键词的子串时，靠后那条永远轮不到。
# 现在最危险的是裸「退款」——将来做退货流程加「退款中」，它会被这条吞成终态「退款」，
# 而终态之后 `can_advance_purchase` 对该单一律 False，等于这张单从此再也不会被自动更新。
# `tests/test_ocr_parse.py` 的前缀安全守卫会在那一刻先红。
_STATUS_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("退款成功", "已退款", "退货退款", "退款"), PurchaseStatus.refunded.value),
    (("交易关闭", "已关闭"), PurchaseStatus.cancelled.value),
    (("交易成功", "交易完成"), PurchaseStatus.signed.value),
    ((_EXPRESS,), PurchaseStatus.shipped.value),
    (("待确认收货", "卖家已发货", "待收货", "确认收货"), PurchaseStatus.shipped.value),
    (("等待卖家发货", "等待发货", "待卖家发货", "待发货"), PurchaseStatus.paid.value),
)


def _detect_purchase_status(full_text: str, has_express: bool) -> Optional[str]:
    """判交易状态：按 `_STATUS_RULES` 自上而下取第一条命中；都不命中兜底「待发货」。

    ⚠️ **命中终态（退款 / 交易关闭）时返回 None，不写这个字段。**

    为什么：判定是在整页拼起来的 `full_text` 上做**无锚点子串扫描**，而规则表第一条
    含裸「退款」——页面上任何位置出现这两个字（「申请退款」「退款/售后」这类按钮、
    甚至商品标题里的「支持七天退款」）都会命中。而终态是**不可逆**的：
    `can_advance_purchase` 对终态一律返回 False，这张单从此再也不会被任何自动流程更新，
    爬虫之后抓到真实状态也推不动它。

    「猜错一个中间态」代价是下一轮自动纠正；「猜错一个终态」代价是这张单永久钉死。
    两者不对称到这个程度，就不该由一个子串扫描来决定。返回 None 之后字段留空，
    由人在暂存表上确认——这正是「机器认的先落暂存」那条规则要解决的事。

    刻意**不**退而求其次去猜一个非终态：截图真是退款单时，硬写成「待发货」是另一种错。
    """
    for patterns, status in _STATUS_RULES:
        for p in patterns:
            if (has_express if p is _EXPRESS else p in full_text):
                return None if status in PURCHASE_TERMINAL_STATUSES else status
    return PurchaseStatus.paid.value               # 兜底：待发货


def _load_truck_ref():
    """懒加载并缓存灰度卡车模板；文件缺失/解码失败返回 None（该信号自动禁用）。"""
    global _truck_ref, _truck_ref_loaded
    if _truck_ref_loaded:
        return _truck_ref
    _truck_ref_loaded = True
    try:
        import cv2
        import numpy as np

        # 用 imdecode 而非 imread：避开 Windows 下 imread 对非 ASCII 路径的读取问题
        data = _TRUCK_REF_PATH.read_bytes()
        _truck_ref = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    except Exception:
        _truck_ref = None
    return _truck_ref


def _truck_score(gray) -> float:
    """多尺度模板匹配卡车，返回最高归一化相关分（0~1）；无参考图/异常返回 0。"""
    try:
        import cv2

        ref = _load_truck_ref()
        if ref is None or gray is None:
            return 0.0
        ih, iw = gray.shape[:2]
        rh, rw = ref.shape[:2]
        best = 0.0
        for s in _TRUCK_SCALES:
            w, h = int(rw * s), int(rh * s)
            if w < 24 or h < 18 or w > iw or h > ih:   # 太小无意义 / 比截图还大则跳过
                continue
            tmpl = cv2.resize(ref, (w, h), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, _ = cv2.minMaxLoc(res)
            best = max(best, float(mx))
        return best
    except Exception:
        return 0.0


def _truck_present(rgb_arr) -> bool:
    """截图里是否出现闲鱼卡车（相关分达阈值）。"""
    try:
        import cv2

        gray = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)
        return _truck_score(gray) >= _TRUCK_MATCH_THRESHOLD
    except Exception:
        return False


def parse_order_fields(ocr_result) -> dict:
    """从 OCR 结果里抽取 {platform, express_company, express_no, order_no, price_cny}（缺省 None）。"""
    tokens = _to_tokens(ocr_result)
    # platform 由 recognize_order 统一判定（需结合卡车图像），这里只抽文本字段
    out = {"platform": None, "express_company": None, "express_no": None,
           "order_no": None, "price_cny": None, "order_date": None, "product": None}
    if not tokens:
        return out

    heights = [t["h"] for t in tokens if t["h"] > 0]
    row_tol = (sum(heights) / len(heights) * 0.8) if heights else 12.0

    # 快递公司 + 快递号。两段式，**先真标签、后公司名**：
    #   「顺丰/天天/菜鸟/京东」这些词会出现在商品标题里（「顺丰包邮」），英文标题还会因
    #   items / systems 里的裸子串 ems 命中。原先是「整页第一个含公司关键词的框即锚点、命中即
    #   break」，于是标题框抢走锚点、真正的快递行再也轮不到，而 _same_row_value 的向下兜底
    #   又没有距离上限——express_no 被抓成页面下方的**订单号**（两者完全相同），
    #   还连带把「等待卖家发货」判成「待收货」。
    for kw in ("快递单号", "运单号", "物流单号"):
        for t in tokens:
            if kw not in t["text"]:
                continue
            no = _extract_tracking(t["text"], 8) or _same_row_value(
                t, tokens, row_tol, 8, key="tracking")
            if no:
                out["express_no"] = no
                out["express_company"] = _match_company(t["text"]) or _nearest_company(t, tokens, row_tol)
                break
        if out["express_no"]:
            break
    if not out["express_no"]:
        # 退回公司名锚点：逐个尝试，取不到值就继续找下一个（不再命中即 break），
        # 且只认同行——公司名不是标签，往下兜底就是上面那个 bug 的成因。
        for t in tokens:
            canonical = _match_company(t["text"])
            if not canonical:
                continue
            no = _extract_tracking(t["text"], 8) or _same_row_value(
                t, tokens, row_tol, 8, key="tracking", allow_below=False)
            if no:
                out["express_company"], out["express_no"] = canonical, no
                break
            # **刻意不再「只认出公司名就报公司」。** 那一支没有锚点约束：
            # 页面上根本没有物流行时，商品标题里的「顺丰包邮」、英文标题里的
            # it`ems`/syst`ems`、京东截图上的「京东自营」（COMPANY_MAP 里有「京东」）
            # 都会凭空填出一个快递公司——看起来完全合理，实际是从装饰词猜的，
            # 而它会落进「快递公司」列并参与标签归组。
            # 没有快递号就是没认出物流信息，留空比编一个强。

    # 订单号：锚点为含「订单编号/订单号」的框；值为本框或同行最长数字（多为 15~20 位）。
    # 用 _first_anchor 而不是「命中即 break」：页面上第一个含「订单号」的框很可能是
    # 列头/筛选栏/灰色分组标题，它们同行没有值——在那儿 break，真正那一行永远轮不到。
    _, out["order_no"] = _first_anchor(
        tokens,
        lambda t: "订单编号" in t["text"] or "订单号" in t["text"],
        lambda t: _longest_digit_run(t["text"], 10) or _same_row_value(t, tokens, row_tol, 10),
    )

    # 成交价：锚点为含「成交价」的框；同行取 ¥ 金额。同样逐个锚点试到取到值为止。
    # ⚠️ 商品名寄生在这个锚点上（_parse_product 靠「成交价上方那行挂牌价」定位），
    # 所以锚点选错不只丢金额，连商品名一起丢。
    price_anchor, price = _first_anchor(
        tokens,
        lambda t: "成交价" in t["text"],
        lambda t: _parse_price(t, tokens, row_tol),
    )
    if price is not None:
        try:
            out["price_cny"] = str(Decimal(price))
        except Exception:
            out["price_cny"] = price

    # 商品名称：成交价上方挂牌价所在行的中文标题
    out["product"] = _parse_product(tokens, row_tol, price_anchor)

    # 下单时间：锚点为含「下单时间」的框；同行取日期（区别于付款/发货时间）
    _, out["order_date"] = _first_anchor(
        tokens,
        lambda t: "下单时间" in t["text"],
        lambda t: _parse_order_date(t, tokens, row_tol),
    )

    return out


MAX_OCR_PIXELS = 40_000_000      # 4000 万像素 ≈ 8000×5000，覆盖 8K 截图与常规扫描件


def _decode_image(image_bytes: bytes):
    """字节 → RGB 数组，**带像素闸**。抛 ValueError（路由映射成 400）。

    为什么必须卡像素而不只卡字节：`routers/common.MAX_OCR_BYTES` 卡的是**压缩后**大小，
    而 PNG 对纯色/渐变的压缩比能到几千倍。实测 189KB 的 13000×13000 PNG 解出来是
    507MB 的 uint8 数组，加上引擎前处理的副本峰值 1.1GB——一张图就能把这台机器
    （可用内存 1.5GB 上下）打到 MemoryError 或被 OOM killer 带走整个 uvicorn。

    闸放在这里而不是路由层：三条上传路由共用同一段解码，改一处全覆盖；
    而且 `Image.open` 是惰性的，读 `.size` 不解码像素，判断本身零成本。

    超限**直接拒绝**而不是降采样：降采样会掉小字体识别率，而识别错的快递单号
    比识别不出来贵得多——错号会去精确匹配并挂靠到别人的订单上。
    """
    from PIL import Image
    import numpy as np

    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
    except Exception as e:
        raise ValueError(f"图片无法解析：{e}") from e
    if w * h > MAX_OCR_PIXELS:
        raise ValueError(
            f"图片分辨率过大（{w}×{h}，上限 {MAX_OCR_PIXELS // 1_000_000} 百万像素）。"
            "请裁掉无关区域或截短后重试"
        )
    try:
        return np.array(img.convert("RGB"))
    except Exception as e:
        raise ValueError(f"图片无法解析：{e}") from e


def _run_engine(engine, arr):
    """串行化引擎推理（RapidOCR 非保证可重入），异常转 ValueError → 路由映射成 400。"""
    with _infer_lock:
        try:
            result, _ = engine(arr)
        except Exception as e:        # noqa: BLE001  极端尺寸/畸形图让引擎前处理内部报错
            raise ValueError(f"图片无法识别（OCR 引擎处理失败）：{e}") from e
    return result


# 用户在上传时能选的来源。**中文显示名，不是 slug**：同一个字符串要同时对上三处——
# 账本活跃行唯一键里的 COALESCE(platform,'')（models/order/order.py）、
# platform_provider 里的 a["platform"] == platform（routers/plugins.py）、
# 前端的平台色表（constants.js 的 ORDER_SOURCES）。
# 插件清单里那个 `platform = "taobao"` 在这条链上没有任何消费者，别混进来。
PLATFORM_CHOICES = ("闲鱼", "淘宝", "京东", "拼多多", "其他")


def _stamp_platform(fields: dict, full_text: str, arr, hint: Optional[str] = None) -> None:
    """判「这张截图是哪个平台的」，写进 `fields` 的 platform / platform_warning。

    `hint` 是用户在上传时自己选的来源。**它永远赢，绝不被图上的证据推翻。**
    理由不是「尊重用户」这种客气话，是 platform 进的是账本活跃行的唯一键
    （`ix_orders_order_no_platform_active` 含 `COALESCE(platform,'')`）：一张认不出的
    淘宝截图落成 platform=NULL，淘宝插件之后抓到同一单写「淘宝」，**同一笔交易变成两行**。
    让证据推翻用户不是气人，是直接制造重复行。

    有 hint 时**仍然跑检测**，只是检测结果降级成一句提醒。两个原因：
      · `platform_warning` 必须保持 truthy，否则路由不去查 platform_plugin、
        前端的 offPlatform 统计与批次汇总整条链一起失效——「跳过检测」会静默拆掉现有功能；
      · 冲突信息本身有用：批量选了淘宝、里面混进一张闲鱼截图，那张的解析其实是准的。
    只跳过**贵的**那一半（卡通卡车模板匹配），文案线索是纯子串扫描、微秒级，白跑不亏。

    OCR 的解析规则是照闲鱼的版式写的，所以来源缺省判「闲鱼」。仅当截图有明确淘宝/京东
    强标记、且无任何闲鱼信号（文案线索或卡通卡车）时，判为**不是闲鱼**。

    ⚠️ 这一支从「拒识」改成了「**警示**」：以前置空 platform 并回一个 reject_reason，
    前端据此**不建单**。但那等于替用户做了决定——他可能就是想手工补一张淘宝的单
    （插件没装、或那一单插件抓不到）。现在照常给出识别结果、并把平台如实标成淘宝/京东，
    只是附一句 `platform_warning`，由前端问一句「确定继续吗」。
    字段名也跟着改了：`reject_reason` 是「我拒绝了」，而现在没有拒绝任何东西。

    **`arr`（解码后的图像）是显式参数，这一点是刻意的。**
    它原先是调用方的一个局部变量，被这段代码隔着几十行引用；
    一次「把解码抽成函数」的重构顺手消掉了那个名字，于是这里静静变成 NameError——
    而它只在冷分支（含京东/淘宝标记的截图）触发，闲鱼截图一切正常，本地试不出来。
    做成参数之后，同样的疏忽会在**调用点**变成 TypeError，立刻炸、人人可见。

    ⚠️ **卡通卡车匹配很贵，短路必须留着。** `_truck_present` 是全图 8 尺度 matchTemplate，
    一张 1080×2400 就是 1 秒量级、长截图更久。所以每一支里都必须先问便宜的文案线索
    （`_is_xianyu`），只有它答不上来才去看图。这里最初就是短路写法，
    是一次「改成拒识语义」的重构顺手提成变量才把短路弄丢的——别再提。
    """
    other = _detect_other_platform(full_text)
    if hint:
        # 用户说了算。检测结果只用来补一句「但图上看着像别家」，不改 platform。
        # ⚠️ 这里**故意不调 `_truck_present`**：它是全图 8 尺度 matchTemplate，
        #    批量拖十几张时省下的就是十几秒。跳过只能写在这一支里——做成无条件短路
        #    会让「闲鱼卡车必须被看一眼」那条守卫变红，而那条守卫是有道理的。
        seen = None if (_is_xianyu(full_text) or other == hint) else other
        fields["platform"] = hint
        if hint == "闲鱼":
            # 解析规则本来就是照闲鱼版式写的，不冲突就一个字都不说。
            fields["platform_warning"] = (
                f"你标了闲鱼，但截图上看到的是{seen}的特征，请复核这张图有没有传错" if seen else None)
        else:
            fields["platform_warning"] = (
                f"你标了{hint}；OCR 的解析规则是照闲鱼的版式写的，"
                f"成交价与商品名多半认不出来，请在暂存表里补"
                + (f"。而截图上看到的是{seen}的特征" if seen else ""))
        return

    if other:
        # 有别的平台的强标记：除非同时有闲鱼信号（闲鱼卖家发京东快递之类），否则就是它。
        if _is_xianyu(full_text) or _truck_present(arr):
            fields["platform"], fields["platform_warning"] = "闲鱼", None
        else:
            fields["platform"] = other
            fields["platform_warning"] = (
                f"这看起来是{other}的截图，而 OCR 的解析规则是照闲鱼的版式写的，"
                f"金额/单号可能认错或认不全")
        return

    # 没有任何别的平台标记。**这里不再无条件判「闲鱼」。**
    #
    # 原先这一支直接写死 platform="闲鱼"：于是一张淘宝截图只要没出现
    # 京豆/白条/天猫/旺旺/官方旗舰店 这几个词，就会被**信誓旦旦地**标成闲鱼——
    # 用户在列表里看到「闲鱼」，没有任何理由去怀疑它，而来源是要进账本的。
    # 「猜错了还一脸笃定」比「说不知道」贵得多：后者用户点一下就改了。
    if _is_xianyu(full_text) or _truck_present(arr):
        fields["platform"], fields["platform_warning"] = "闲鱼", None
    else:
        # 两边都没有证据 → 留空，由用户来定。空来源是**被支持的状态**：
        # 唯一索引走 COALESCE(platform,'')，同订单号照样去重（见 models/order/order.py）。
        fields["platform"] = None
        fields["platform_warning"] = "认不出这是哪个平台的截图（没有闲鱼、也没有淘宝/京东的特征）"


def recognize_order(image_bytes: bytes, platform_hint: Optional[str] = None) -> dict:
    """对上传的截图跑 OCR 并解析出订单字段。抛 OcrUnavailable 表示引擎不可用。

    `platform_hint` 缺省 None ⇒ 行为与加这个参数之前**逐字节相同**（自动判别那三条出口）。
    默认值不是可选的：好几处测试用单参 `recognize_order(b"...")` 调它。"""
    engine = _get_engine()
    arr = _decode_image(image_bytes)
    result = _run_engine(engine, arr)
    fields = parse_order_fields(result)

    full = "\n".join(item[1] for item in (result or []))
    _stamp_platform(fields, full, arr, platform_hint)
    # 交易状态：有快递单号（已发货）→ 待收货，否则待发货（另按头部状态语细分终态）
    fields["purchase_status"] = _detect_purchase_status(full, bool(fields.get("express_no")))
    # 注：旧版在「交易成功」时会清空快递公司/快递号（认为已完成的单不需要物流信息）。现在快递号
    # 是集运「内含快递」联动的匹配键——清掉就再也匹配不上该订单，故保留。误取风险本就很低：
    # 快递号必须以 COMPANY_MAP 命中的快递公司名为锚点、且同行有「≥8 位含≥6 个数字」的串才会被抓。
    # 附带完整识别文本，便于前端在缺字段时给用户核对/手动补
    fields["raw_text"] = full
    return fields


# --- 集运订单截图 -------------------------------------------------------------
# 集运平台「支付详情」页有两个 Tab，各自一张截图，需分别识别后合并到同一张集运单：
#   成品包裹：国际单号 EB861624386CN / 成品包裹号 2304513-1 / 渠道 / 订单时间
#   内含快递：本包裹装了哪几个国内快递单号（用来联动商品订单）
# 与商品订单 OCR 复用同一引擎、同一套「标签锚点 + 几何同行关联」原语。

# 「渠道」取国际单号上方最近的中文行；这些是页面固定文案，不是渠道名，需排除。
_SHIPMENT_CHROME = ("计费详情", "打包成", "客服留言", "支付详情",
                    "成品包裹", "内含快递", "打包要求", "尺寸查看", "订单时间")


def _nearest_cjk_above(anchor: dict, tokens: list[dict], row_tol: float) -> Optional[str]:
    """取 anchor 上方最近的一行中文文本（跳过页面固定文案），用于抓「渠道」这类无标签的标题行。"""
    above = [t for t in tokens
             if t["cy"] < anchor["cy"] - row_tol
             and _has_cjk(t["text"])
             and not any(w in t["text"] for w in _SHIPMENT_CHROME)]
    if not above:
        return None
    return max(above, key=lambda t: t["cy"])["text"].strip() or None


def parse_shipment_fields(ocr_result) -> dict:
    """从集运截图的 OCR 结果里抽取集运单字段。两个 Tab 共用本函数，靠 kind 区分识别到的是哪张。"""
    tokens = _to_tokens(ocr_result)
    out = {"kind": "unknown", "shipment_no": None, "intl_tracking_no": None,
           "date": None, "channel": None, "express_nos": [], "unreadable": 0}
    if not tokens:
        return out

    heights = [t["h"] for t in tokens if t["h"] > 0]
    row_tol = (sum(heights) / len(heights) * 0.8) if heights else 12.0

    # 逐个锚点尝试直到取到值（理由见模块级的 _first_anchor）。这里原先是本函数内的一个
    # 局部 _first——而商品订单那侧一直是「命中即 break」，同一个坑修了一半。现已共用。
    def _first(keyword: str, pick):
        return _first_anchor(tokens, lambda t: keyword in t["text"], pick)

    # 国际单号：值为本框或同行的 tracking（EB861624386CN 含 9 位数字，min_len=8 可命中）
    anchor, value = _first(
        "国际单号",
        lambda t: _extract_tracking(t["text"], 8) or _same_row_value(t, tokens, row_tol, 8, key="tracking"),
    )
    if anchor is not None:
        out["intl_tracking_no"] = value
        out["channel"] = _nearest_cjk_above(anchor, tokens, row_tol)

    # 成品包裹号：值带连字符（2304513-1），走 package 提取器
    _, out["shipment_no"] = _first(
        "包裹号",
        lambda t: _extract_package_no(t["text"]) or _same_row_value(t, tokens, row_tol, 6, key="package"),
    )

    # 订单时间：只取日期部分（页面是 YYYY-MM-DD HH:MM:SS）
    _, out["date"] = _first("订单时间", lambda t: _parse_order_date(t, tokens, row_tol))

    # 内含快递：**所有**含「快递单号」的框都是锚点（不是取首个），逐行取号后去重保序。
    #
    # `unreadable` 是「看见了锚点、却没能取到号」的行数。没有它的时候这件事**零出口**：
    # 3 行快递里坏了 1 行 → 响应里只有 2 个号 → 前端照样弹绿色的「已关联 2 单」，
    # 用户没有任何线索知道第 3 单被丢了，也无从把遗留的未挂靠单对回是哪张截图。
    # 注意：重号（两行取到同一个号，_same_row_value 跨行兜底时会发生）不计入 unreadable，
    # 它是「取到了但重复」，与「取不到」是两回事——但它同样意味着少挂一单，故一并计数。
    seen = set()
    for t in tokens:
        if "快递单号" not in t["text"]:
            continue
        no = _extract_tracking(t["text"], 8) or _same_row_value(t, tokens, row_tol, 8, key="tracking")
        if no and no not in seen:
            seen.add(no)
            out["express_nos"].append(no)
        else:
            out["unreadable"] += 1

    # kind：成品包裹页的标识字段优先；只有快递号列表则是内含快递页
    if out["intl_tracking_no"] or out["shipment_no"]:
        out["kind"] = "package"
    elif out["express_nos"]:
        out["kind"] = "express_list"
    return out


def recognize_shipment(image_bytes: bytes) -> dict:
    """对上传的集运截图跑 OCR 并解析出集运单字段。抛 OcrUnavailable 表示引擎不可用。
    不做平台判定/卡车模板匹配——那套是闲鱼商品订单专用的。"""
    engine = _get_engine()
    result = _run_engine(engine, _decode_image(image_bytes))
    fields = parse_shipment_fields(result)
    fields["raw_text"] = "\n".join(item[1] for item in (result or []))
    return fields
