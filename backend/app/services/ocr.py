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

from ..models import PurchaseStatus   # 状态枚举值的唯一真相，避免与后端漂移

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
# express_company='京东物流'、又打上 reject_reason='疑似京东订单截图'，前端据此不建单。
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


def _extract_tracking(text: str, min_len: int = 8) -> Optional[str]:
    """快递单号：可能带字母前缀（顺丰 SF、京东 JD 等），取「字母数字混合、含≥6 位数字」的
    最长串并统一大写；纯数字单号照常命中。要求含足够数字 → 排除纯字母串（如地址 ATPTSTKH）。"""
    runs = re.findall(r"[A-Za-z0-9]+", text or "")
    cands = [r for r in runs if len(r) >= min_len and sum(c.isdigit() for c in r) >= 6]
    return max(cands, key=len).upper() if cands else None


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


def _match_company(text: str) -> Optional[str]:
    for kw, canonical in COMPANY_MAP.items():
        if kw in text:
            return canonical
    return None


def _nearest_company(anchor: dict, tokens: list[dict], row_tol: float) -> Optional[str]:
    """标签行本身不含公司名时，在它同行、再不行就上下最近处找一个。

    只在**已经由真标签定位到快递号**之后调用——此时公司名只是补充信息，取错的代价远小于
    「整页第一个含公司词的框」那种把商品标题当锚点的做法。"""
    same_row = [t for t in tokens
                if t is not anchor and abs(t["cy"] - anchor["cy"]) <= row_tol]
    for t in sorted(same_row, key=lambda t: abs(t["cy"] - anchor["cy"])):
        if (c := _match_company(t["text"])):
            return c
    for t in sorted(tokens, key=lambda t: abs(t["cy"] - anchor["cy"])):
        if t is not anchor and (c := _match_company(t["text"])):
            return c
    return None


def _same_row_value(anchor: dict, tokens: list[dict], row_tol: float,
                    min_len: int, key: str = "digits", allow_below: bool = True) -> Optional[str]:
    """在与 anchor 同一行（y 接近）里找 key 值（digits/tracking）最长的框，优先取右侧的。
    找不到同行则退回到 anchor 下方最近的框。

    `allow_below=False` 用于**内容词锚点**（如快递公司名）——真标签（「订单编号」「快递单号」）
    只会出现在它的值旁边，往下兜底是安全的；而公司名可能出现在页面任何地方（商品标题里的
    「顺丰包邮」），往下兜底会一路抓到页面下方的订单号，把它当成快递号。"""
    cands = [t for t in tokens if t is not anchor and t[key]
             and len(t[key]) >= min_len]
    same_row = [t for t in cands if abs(t["cy"] - anchor["cy"]) <= row_tol]
    if same_row:
        right = [t for t in same_row if t["x0"] >= anchor["x0"]]
        pick = min(right or same_row, key=lambda t: abs(t["cy"] - anchor["cy"]))
        return pick[key]
    if not allow_below:
        return None
    below = [t for t in cands if t["cy"] > anchor["cy"]]
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


def _detect_purchase_status(full_text: str, has_express: bool) -> str:
    """判交易状态：按 `_STATUS_RULES` 自上而下取第一条命中；都不命中兜底「待发货」。"""
    for patterns, status in _STATUS_RULES:
        for p in patterns:
            if (has_express if p is _EXPRESS else p in full_text):
                return status
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
            if out["express_company"] is None:
                out["express_company"] = canonical    # 只认出公司、没有单号：仍然报公司

    # 订单号：锚点为含「订单编号/订单号」的框；值为本框或同行最长数字（多为 15~20 位）
    for t in tokens:
        if "订单编号" in t["text"] or "订单号" in t["text"]:
            run = _longest_digit_run(t["text"], 10)
            out["order_no"] = run or _same_row_value(t, tokens, row_tol, 10)
            break

    # 成交价：锚点为含「成交价」的框；同行取 ¥ 金额
    price_anchor = None
    for t in tokens:
        if "成交价" in t["text"]:
            price_anchor = t
            price = _parse_price(t, tokens, row_tol)
            if price is not None:
                try:
                    out["price_cny"] = str(Decimal(price))
                except Exception:
                    out["price_cny"] = price
            break

    # 商品名称：成交价上方挂牌价所在行的中文标题
    out["product"] = _parse_product(tokens, row_tol, price_anchor)

    # 下单时间：锚点为含「下单时间」的框；同行取日期（区别于付款/发货时间）
    for t in tokens:
        if "下单时间" in t["text"]:
            out["order_date"] = _parse_order_date(t, tokens, row_tol)
            break

    return out


def recognize_order(image_bytes: bytes) -> dict:
    """对上传的截图跑 OCR 并解析出订单字段。抛 OcrUnavailable 表示引擎不可用。"""
    engine = _get_engine()
    try:
        from PIL import Image
        import numpy as np

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
    except Exception as e:
        raise ValueError(f"图片无法解析：{e}") from e

    with _infer_lock:                 # 串行化引擎推理（RapidOCR 非保证可重入）
        try:
            result, _ = engine(arr)
        except Exception as e:        # noqa: BLE001  极端尺寸/畸形图让引擎前处理内部报错
            # 转成 ValueError → 路由映射为 400「图片无法识别」，而不是裸 500。
            raise ValueError(f"图片无法识别（OCR 引擎处理失败）：{e}") from e
    fields = parse_order_fields(result)

    # OCR 只产出闲鱼数据：来源恒为「闲鱼」。仅当截图有明确淘宝/京东强标记、且无任何闲鱼信号
    # （文案线索或卡通卡车）时，判为拿错截图 → 拒识并提示改用爬虫（reject_reason 非空）。
    full = "\n".join(item[1] for item in (result or []))
    other = _detect_other_platform(full)
    # 只有真看到淘宝/京东强标记、需要判「拿错截图」时才求闲鱼信号：卡车是全图 8 尺度
    # matchTemplate，一张 1080×2400 就是 1 秒量级、长截图更久，而绝大多数闲鱼截图 other 为
    # None，这笔钱纯属白付。
    # ⚠️ **不要**再把它提成 `is_xianyu = ...` 变量提前算——这里最初就是短路写法，
    # 是一次「改成拒识语义」的重构顺手提成变量才把短路弄丢的。短路必须留在分支里。
    if other and not (_is_xianyu(full) or _truck_present(arr)):
        fields["platform"] = None
        fields["reject_reason"] = f"疑似{other}订单截图；OCR 仅支持闲鱼，淘宝/京东请用爬虫抓取"
    else:
        fields["platform"] = "闲鱼"
        fields["reject_reason"] = None
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
           "date": None, "channel": None, "express_nos": []}
    if not tokens:
        return out

    heights = [t["h"] for t in tokens if t["h"] > 0]
    row_tol = (sum(heights) / len(heights) * 0.8) if heights else 12.0

    # 同一个词可能在页面上出现多次（如「订单时间」既是灰色分组标题、又是数据行标签，标题那行
    # 同行没有值），故逐个锚点尝试直到取到值，而不是在首个锚点上 break。
    def _first(keyword: str, pick):
        for t in tokens:
            if keyword in t["text"] and (v := pick(t)) is not None:
                return t, v
        return None, None

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

    # 内含快递：**所有**含「快递单号」的框都是锚点（不是取首个），逐行取号后去重保序
    seen = set()
    for t in tokens:
        if "快递单号" not in t["text"]:
            continue
        no = _extract_tracking(t["text"], 8) or _same_row_value(t, tokens, row_tol, 8, key="tracking")
        if no and no not in seen:
            seen.add(no)
            out["express_nos"].append(no)

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
    try:
        from PIL import Image
        import numpy as np

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
    except Exception as e:
        raise ValueError(f"图片无法解析：{e}") from e

    with _infer_lock:                 # 串行化引擎推理（RapidOCR 非保证可重入）
        try:
            result, _ = engine(arr)
        except Exception as e:        # noqa: BLE001  极端尺寸/畸形图让引擎前处理内部报错
            # 转成 ValueError → 路由映射为 400「图片无法识别」，而不是裸 500。
            raise ValueError(f"图片无法识别（OCR 引擎处理失败）：{e}") from e
    fields = parse_shipment_fields(result)
    fields["raw_text"] = "\n".join(item[1] for item in (result or []))
    return fields
