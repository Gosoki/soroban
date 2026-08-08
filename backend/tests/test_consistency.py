"""跨层一致性：后端枚举 ↔ 前端常量 ↔ 爬虫插件映射。
这些常量分散在三处、只能靠约定同步——这里把约定变成断言，改一处漏改另一处即红。"""
from __future__ import annotations

import re
import pathlib
from pathlib import Path

import pytest

from app.models import PURCHASE_STATUS_RANK, PurchaseStatus, ShipmentStatus, ImportStatus

_REPO = Path(__file__).resolve().parents[2]
_CONSTANTS_JS = _REPO / "frontend" / "src" / "constants.js"


def _template_of(path) -> str:
    """取 SFC 的整个 <template> 段。

    ⚠️ 别用 `src.split("</template>")[0]` —— 具名插槽自己就是 `<template #xxx>…</template>`，
    第一个 `</template>` 是**内层插槽**的收尾，那样切会把模板砍掉大半，
    于是「找不到插槽」被误判成「没有插槽」，测试假绿。按 <script 切才对。"""
    src = pathlib.Path(path).read_text(encoding="utf-8")
    return src.split("<script", 1)[0]
_ORDERS_VUE = _REPO / "frontend" / "src" / "views" / "Orders" / "index.vue"
_SCRAPER_NORMALIZE = _REPO / "scraper" / "soroban-scraper-taobao" / "taobao_scraper" / "normalize.py"


def _js_array(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\s*=\s*\[(.*?)\]", text, re.S)
    assert m, f"没在 constants.js 里找到 {name}"
    return re.findall(r"'([^']*)'", m.group(1))


@pytest.fixture(scope="module")
def constants_js() -> str:
    return _CONSTANTS_JS.read_text(encoding="utf-8")


def test_frontend_order_status_matches_backend(constants_js):
    assert _js_array(constants_js, "PURCHASE_STATUS") == [s.value for s in PurchaseStatus]


def test_frontend_shipment_status_matches_backend(constants_js):
    assert _js_array(constants_js, "SHIPMENT_STATUS") == [s.value for s in ShipmentStatus]


def test_frontend_staging_status_matches_backend(constants_js):
    assert _js_array(constants_js, "IMPORT_STATUS") == [s.value for s in ImportStatus]


def test_frontend_status_rank_matches_backend():
    """前端的推进序决定 OCR 合并时状态能否推进；与后端 PURCHASE_STATUS_RANK 必须一致。

    它已从 Orders 页搬进 constants.js —— 两个页面各存一份必然漂移。"""
    text = _CONSTANTS_JS.read_text(encoding="utf-8")
    m = re.search(r"const PURCHASE_STATUS_RANK\s*=\s*\{(.*?)\}", text, re.S)
    assert m, "没在 constants.js 里找到 PURCHASE_STATUS_RANK"
    fe = {k: int(v) for k, v in re.findall(r"([^\s,{]+)\s*:\s*(\d+)", m.group(1))}
    assert fe == PURCHASE_STATUS_RANK


def test_scraper_status_map_targets_are_valid(constants_js):
    """爬虫 STATUS_MAP 的目标值必须都是后端 PurchaseStatus 的合法值，否则回灌整批 422。"""
    if not _SCRAPER_NORMALIZE.is_file():
        pytest.skip("插件未安装")
    text = _SCRAPER_NORMALIZE.read_text(encoding="utf-8")
    m = re.search(r"STATUS_MAP\s*=\s*\{(.*?)\n\}", text, re.S)
    assert m, "没找到 STATUS_MAP"
    targets = {v for _, v in re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1))}
    valid = {s.value for s in PurchaseStatus}
    assert targets <= valid, f"爬虫会推出后端不接受的状态：{sorted(targets - valid)}"


def test_status_rank_covers_all_forward_statuses():
    """生命周期里「会前进」的状态都要有 rank；终态刻意不在表里（取 -1）。

    ⚠️ 但 -1 **不能**单独用来判「能不能覆盖」——见下面那条。"""
    from app.models import is_purchase_terminal, purchase_status_rank
    for s in PurchaseStatus:
        if is_purchase_terminal(s.value):
            assert purchase_status_rank(s.value) == -1
        else:
            assert purchase_status_rank(s.value) >= 0, f"{s.value} 缺 rank"


def test_terminal_status_is_never_overwritten_by_automation():
    """本项目栽过的坑：终态 rank 为 -1，而 -1 的效果是「**任何**推进态都 > 它」，
    于是一张已标「退款」的单被 OCR 再识别一次就被抹成「待发货」——
    退款本不计入看板合计，抹掉后**看板金额凭空变大**（实测 188606→211975）。"""
    from app.models import can_advance_purchase

    for terminal in ("退款", "交易关闭"):
        for incoming in ("待付款", "待发货", "待收货", "已签收"):
            assert not can_advance_purchase(terminal, incoming), \
                f"终态「{terminal}」被「{incoming}」盖掉了"


def test_automation_may_still_set_terminal_status():
    """反向：平台把单关了/退款了，账本该跟上。"""
    from app.models import can_advance_purchase

    assert can_advance_purchase("待收货", "退款")
    assert can_advance_purchase("已签收", "交易关闭")


def test_can_advance_only_moves_forward():
    from app.models import can_advance_purchase

    assert can_advance_purchase("待发货", "待收货")
    assert not can_advance_purchase("已签收", "待发货"), "不该回退"
    assert not can_advance_purchase("待收货", "待收货"), "相同值不该触发写入"
    assert not can_advance_purchase("待收货", None)


def test_frontend_merge_uses_the_same_terminal_rule():
    """前端 OCR 合并有自己一份 rank（跨语言没法直接共用）。至少钉住：
    它必须有终态保护、且不能再退回到「只比 rank」。"""
    src = (_REPO / "frontend" / "src" / "views" / "Orders" / "index.vue").read_text(encoding="utf-8")
    assert "canAdvancePurchase" in src, "前端合并没有走终态保护"
    assert "canAdvancePurchase(base.purchase_status, data.purchase_status)" in src, \
        "合并判定没走 canAdvancePurchase"
    # 不再断言「某串不存在」：改名之后那类否定断言会恒真（假绿）。
    # 真正的保护是行为级的 test_ocr_merge_never_downgrades_terminal_status。


def test_ocr_recognises_refund_before_express():
    """退款单同样带快递号。先判快递就会把退款单识别成「待收货」——之前正是这么错的。"""
    from app.services import ocr

    assert ocr._detect_purchase_status("退款成功 买家已收到退款", False) == "退款"
    assert ocr._detect_purchase_status("退款成功 买家已收到退款", True) == "退款", "有快递号也该判退款"
    assert ocr._detect_purchase_status("交易关闭", True) == "交易关闭"


def test_layout_table_whitelist_covers_frontend_tables():
    """前端 NotionTable 用到的 table-name 都必须在后端 _TABLES 白名单里，否则保存列布局 422。"""
    from app.routers.layout import _TABLES
    used = set()
    for vue in (_REPO / "frontend" / "src" / "views").rglob("index.vue"):
        used |= set(re.findall(r'table-name="([a-z_]+)"', vue.read_text(encoding="utf-8")))
    assert used <= _TABLES, f"前端用到但后端未白名单的表：{sorted(used - _TABLES)}"


def test_frontend_api_paths_exist_in_backend():
    """前端 api/index.js 里写死的每条 (方法, 路径)，都要能在 FastAPI 路由表里找到。
    路径里的模板变量与路由参数都归一成 `*`，只比结构。"""
    from app.main import app

    def route_regex(path: str) -> re.Pattern:
        # /api/orders/{order_id} → ^/api/orders/[^/]+$；{value:path} 允许含斜杠
        parts, pos = [], 0
        for m in re.finditer(r"\{([^}]+)\}", path):
            parts.append(re.escape(path[pos:m.start()]))
            parts.append(".+" if m.group(1).endswith(":path") else "[^/]+")
            pos = m.end()
        parts.append(re.escape(path[pos:]))
        return re.compile("^" + "".join(parts) + "$")

    # 用 OpenAPI 表而不是 app.routes：新版 FastAPI 把 include_router 的结果包成
    # _IncludedRouter，app.routes 顶层看不到子路由；openapi()["paths"] 是展平后的权威表。
    spec = app.openapi()["paths"]
    routes = [(method.upper(), route_regex(path))
              for path, ops in spec.items() for method in ops]
    assert any("/api/orders" in rx.pattern for _, rx in routes), "路由表未展开，测试自身失效"

    text = (_REPO / "frontend" / "src" / "api" / "index.js").read_text(encoding="utf-8")
    calls = re.findall(r"http\.(get|post|put|patch|delete)\(\s*[`']([^`'$)]*(?:\$\{[^}]+\}[^`'$)]*)*)",
                       text)
    missing = []
    for method, raw in calls:
        path = "/api" + re.sub(r"\$\{[^}]+\}", "X", raw)
        if not any(m == method.upper() and rx.match(path) for m, rx in routes):
            missing.append(f"{method.upper()} {path}")
    assert not missing, f"前端调用了后端没有的接口：{missing}"


# --- 列头说明（NotionTable 的 col.help）---------------------------------------

def test_price_help_is_shared_not_duplicated():
    """「人民币（元）」的口径说明同时出现在订单页与暂存页。必须共用 constants.js 的
    PRICE_HELP，不许各页抄一份——同一个解释抄两遍，改一处忘一处就会自相矛盾。"""
    root = _REPO / "frontend" / "src"
    assert "export const PRICE_HELP" in (root / "constants.js").read_text(encoding="utf-8")
    for page in ("Orders", "Staging"):
        src = (root / "views" / page / "index.vue").read_text(encoding="utf-8")
        assert "help: PRICE_HELP" in src, f"{page} 页的人民币列没挂说明"
        assert "PRICE_HELP" in src.split("from '@/constants'")[0], f"{page} 页没 import PRICE_HELP"


def test_price_help_explains_the_known_rounding_gap():
    """这条说明的存在意义就是解释「为什么和淘宝实付差几分」。
    真把内容改没了，用户点开「?」会看到一段答非所问的话。"""
    js = (_REPO / "frontend" / "src" / "constants.js").read_text(encoding="utf-8")
    body = js.split("export const PRICE_HELP")[1]
    for kw in ("单价", "数量", "邮费", "四舍五入", "误差"):
        assert kw in body, f"人民币列说明里缺少「{kw}」"


def test_notion_table_supports_column_help():
    """col.help 是通用能力（任何口径不直观的列都能用），不是给某一列写死的。"""
    src = (_REPO / "frontend" / "src" / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    assert 'v-if="col.help"' in src and "QuestionFilled" in src


# --- 表格单元格的「按行锁定」与面板手填货款 -------------------------------------

def _orders_vue() -> str:
    return (_REPO / "frontend" / "src" / "views" / "Orders" / "index.vue").read_text(encoding="utf-8")


def test_status_column_is_still_a_click_to_pick_tag():
    """状态必须保持「点标签就能选」——和其它标签列一致的交互。
    曾经为了显示继承来的集运状态，把它换成过 el-select 下拉，交互就和别的列不一样了。"""
    src = _orders_vue()
    assert "key: 'purchase_status', label: '状态', type: 'select'" in src, "状态列不再是 select 单元格（点标签选）"
    assert "#cell-purchase_status" not in src, "状态列又被自定义槽接管了，交互会和其它标签列不一致"


def test_status_locks_when_attached_but_items_does_not():
    """**只有状态列**锁定：它的值由所挂集运单决定，能点开改只会让人困惑
    （改的是被遮住的国内段状态，改完看不出变化）。

    物品列**不锁**——挂靠不改变「这单买了什么」，物品该照常能改（走展开面板）。
    曾误把它一起锁上，是把「物品列表页同理」错读成了「物品这一列同理」。"""
    src = _orders_vue()
    st = src.split("key: 'purchase_status'")[1][:400]
    assert "lock: (row) => !!row.shipment_order_id" in st, "状态列没有按挂靠锁定"
    assert "lockHint" in st, "状态列锁定了却没给提示"

    it = src.split("key: 'items'")[1][:300]
    assert "lock:" not in it, "物品列又被锁上了——挂靠不该影响改物品"


def test_items_page_status_follows_shipment_too():
    """「物品列表页同理」：那一页也有状态列，同样要显示继承来的集运状态。

    ⚠️ 这条曾是**假绿**：原来只 grep 列配置里有没有 `display: (row) => row.fulfillment_status`
    这串字符——而该列有 `#cell-purchase_status` 插槽，NotionTable 的插槽分支优先于 GotionCell，
    `col.display` 根本不会被调用。于是 bug 活着、测试全绿，比没有守卫更糟。
    现在改成断言**插槽内**真的用了 fulfillment_status。"""
    tpl = _template_of(_REPO / "frontend" / "src" / "views" / "Items" / "index.vue")
    assert "#cell-purchase_status" in tpl, "状态列改成不用插槽了？那要相应改回断言 col.display"
    slot = tpl.split("#cell-purchase_status")[1][:400]
    assert "fulfillment_status" in slot, "物品列表页的状态插槽没用继承来的状态"


def test_items_filter_matches_what_items_page_displays():
    """显示继承值、筛选却按订单自身状态查 → 「界面一排已发出，筛已发出搜不到」。
    orders.py 专门防过这个坑，items.py 一度没跟上。"""
    import inspect

    from app.routers import items as mod

    src = inspect.getsource(mod.list_items)
    assert "func.coalesce(ship_status, Order.purchase_status) == fulfillment_status" in src, \
        "物品列表的状态筛选没跟显示口径对齐"


def test_slot_columns_do_not_rely_on_col_display():
    """通用陷阱：任何列一旦有 `#cell-xxx` 插槽，它的 `col.display` 就是死代码。
    两者同时存在几乎必然是写错了——本项目已经栽过一次。"""
    import re

    root = _REPO / "frontend" / "src" / "views"
    bad = []
    for f in root.rglob("index.vue"):
        src = f.read_text(encoding="utf-8")
        slots = set(re.findall(r"#cell-(\w+)", _template_of(f)))
        for m in re.finditer(r"key: '(\w+)'[^}]*?display:", src, re.S):
            if m.group(1) in slots:
                bad.append(f"{f.relative_to(_REPO)} 的 {m.group(1)} 列同时有插槽和 display")
    assert not bad, "插槽会让 col.display 变成死代码：\n  " + "\n  ".join(bad)


def test_items_api_exposes_inherited_status():
    """前端要靠这两个字段渲染，后端不给就只能显示订单自己的国内段状态。"""
    from app.schemas import ItemListRead

    assert "fulfillment_status" in ItemListRead.model_fields
    assert "shipment_order_id" in ItemListRead.model_fields


def test_price_column_is_fillable_on_new_row_only():
    """金额由物品派生、数据行只读；但**新建一单时总得有个地方把金额填进去**，
    否则只能先建一张空单再进面板补——用户第一次就被这个卡住了。"""
    blk = _orders_vue().split("key: 'price_cny'")[1][:300]
    assert "readonly: true" in blk and "newEditable: true" in blk
    src = (_REPO / "frontend" / "src" / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    assert "col.newEditable" in src and "function newCol(" in src


def test_status_cell_displays_inherited_value():
    """锁定期间要显示**继承来的**集运状态，而不是订单自己的国内段状态。"""
    blk = _orders_vue().split("key: 'purchase_status'")[1][:400]
    assert "display: (row) => row.fulfillment_status" in blk


def test_locked_cell_greys_the_cell_not_the_tag():
    """置灰的是格子，标签保持原色——否则一眼看不出是什么状态。"""
    css = (_REPO / "frontend" / "src" / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    assert ".gtn-td-locked" in css
    assert ".gtn-td-locked .el-tag { opacity: 1; }" in css, "标签跟着格子一起变灰了"


def test_notion_table_supports_row_level_lock():
    """按行锁定是通用能力（col.lock(row)），不是给某一列写死的。"""
    src = (_REPO / "frontend" / "src" / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    assert "function isLocked(col, row)" in src and "col.lock === 'function'" in src
    cell = (_REPO / "frontend" / "src" / "components" / "GotionCell.vue").read_text(encoding="utf-8")
    assert "locked: { type: Boolean" in cell
    assert "props.col.readonly || props.locked" in cell, "锁定没挡住 date/text 等类型的编辑入口"


def test_panel_allows_typing_goods_amount():
    """面板要能直接填货款（不想拆明细时的懒人入口）：折成「订单标题 × 1」的单条物品。"""
    src = (_REPO / "frontend" / "src" / "components" / "OrderItemsEditor.vue").read_text(encoding="utf-8")
    assert "async function applyGoods" in src
    assert "quantity: 1" in src and "props.order.title" in src, "手填货款没绑成「订单名称×1」"
    assert ':disabled="!isSingleUnitItem"' in src, "拆过明细后仍允许从这里覆盖，会冲掉明细"


def test_settings_page_never_touches_raw_draft_array():
    """设置页的源列表在数据回来之前是 undefined。模板里直接 `draft['fx.sources'].xxx`
    会在渲染时抛 TypeError → **整页卡死白屏**（本页真这么炸过一次）。
    统一走带兜底的 `chain` computed，模板里不许再出现裸的 draft['fx.sources']。"""
    src = (_REPO / "frontend" / "src" / "views" / "Settings" / "index.vue").read_text(encoding="utf-8")
    assert "draft['fx.sources']" not in _template_of(
        _REPO / "frontend" / "src" / "views" / "Settings" / "index.vue"
    ), "模板里又直接摸 draft['fx.sources'] 了"
    assert "const chain = computed(" in src, "缺少带兜底的 chain computed"


def test_settings_source_keys_match_backend():
    """页面上那份「源 key → 中文名」必须与后端 SOURCE_LABELS 对齐，
    否则用户看到的是 `erapi` 这种原始 key。"""
    from app.services.fx import SOURCE_LABELS

    src = (_REPO / "frontend" / "src" / "views" / "Settings" / "index.vue").read_text(encoding="utf-8")
    for key in SOURCE_LABELS:
        assert f"{key}:" in src, f"设置页缺少源 {key} 的展示名"


def test_nav_is_generated_from_routes():
    """侧栏菜单曾是手写数组，与路由表两份——加了路由忘了加它，页面就进不去。
    必须从路由表生成。"""
    layout = (_REPO / "frontend" / "src" / "components" / "Layout.vue").read_text(encoding="utf-8")
    assert "router.getRoutes()" in layout, "侧栏又变回手写数组了"
