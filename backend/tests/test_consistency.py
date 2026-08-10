"""跨层一致性：后端枚举 ↔ 前端常量 ↔ 爬虫插件映射。
这些常量分散在三处、只能靠约定同步——这里把约定变成断言，改一处漏改另一处即红。"""
from __future__ import annotations

import re
import pathlib
from pathlib import Path

import pytest

from app.models import (
    PURCHASE_STATUS_RANK,
    PURCHASE_TERMINAL_STATUSES,
    ImportStatus,
    PurchaseStatus,
    ShipmentStatus,
)

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
def plugin_source(*parts) -> Path:
    """定位插件仓里的文件。找不到就**红**，不是 skip。

    这些守卫钉的是跨仓契约（插件按字段名 POST，名字对不上 = 整批 422 静默丢同步）——
    历史上排第一的 bug 类。原先写的是 `if not path.is_file(): pytest.skip("插件未安装")`，
    于是把插件目录从 `scraper/` 搬到 `plugins/` 之后，它们**全部静默跳过**，
    而整套测试依然全绿。守卫悄悄归零比没有守卫更危险。

    真的没 checkout 插件仓时（CI、纯后端开发机），显式设 `SOROBAN_NO_PLUGINS=1` 跳过——
    要跳过必须是**有人明确表示**，不能是路径写错的副作用。
    """
    import os

    for base in ("plugins/soroban-plugin-taobao", "scraper/soroban-scraper-taobao"):
        p = _REPO.joinpath(base, *parts)
        if p.is_file():
            return p
    if os.environ.get("SOROBAN_NO_PLUGINS"):
        pytest.skip("显式声明了本机没有插件仓（SOROBAN_NO_PLUGINS=1）")
    raise AssertionError(
        f"找不到插件文件 {'/'.join(parts)}。插件仓应在 plugins/soroban-plugin-taobao/。\n"
        "本条守卫钉的是跨仓字段名契约，不能静默跳过——真没有插件仓请设 SOROBAN_NO_PLUGINS=1。")



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


def test_frontend_terminal_statuses_match_backend(constants_js):
    """终态集合两边必须逐值一致——**这条断言比那次改名重要得多**。

    前端拿它决定「这个状态还能不能往前推」；后端拿它决定「这一单计不计入合计」。
    两边漂移的后果不是报错，是**看板金额凭空变大**：前端以为某单是终态、不再推进，
    后端却不认为它是终态、照样加进 SUM——这个项目栽过一次一模一样的跟头
    （OCR 合并把终态盖掉），根因正是前后端各存了一份规则。
    改名（PURCHASE_TERMINAL → PURCHASE_TERMINAL_STATUSES）只是让两边同名好找，
    真正拦住漂移的是这一条。
    """
    assert set(_js_array(constants_js, "PURCHASE_TERMINAL_STATUSES")) == \
        set(PURCHASE_TERMINAL_STATUSES)


def test_scraper_status_map_targets_are_valid(constants_js):
    """爬虫 STATUS_MAP 的目标值必须都是后端 PurchaseStatus 的合法值，否则回灌整批 422。"""
    text = plugin_source("taobao_scraper", "normalize.py").read_text(encoding="utf-8")
    m = re.search(r"PURCHASE_STATUS_MAP\s*=\s*\{(.*?)\n\}", text, re.S)
    assert m, "没找到 PURCHASE_STATUS_MAP"
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
    import re

    tpl = _template_of(_REPO / "frontend" / "src" / "views" / "Settings" / "index.vue")
    # 会炸的形状是「取下标之后直接点属性」：draft[...]. → undefined.xxx → 渲染时 TypeError。
    # 判据取这个形状本身，而不是某个具体变量名（原先钉的是 `const chain = computed(`，
    # 页面改成按注册表渲染、不再有那个变量之后，这条会变成一句与安全无关的空话）。
    bad = re.findall(r"draft\[[^\]]+\]\s*\.", tpl)
    assert not bad, f"模板里直接对可能为 undefined 的 draft 下标取属性：{bad}"
    # 反面：凡是在模板里当数组用的，必须带兜底
    for m in re.finditer(r"draft\[[^\]]+\](?!\s*(?:\|\||\)|\"|\s*$))", tpl):
        seg = tpl[m.start():m.start() + 60]
        assert "|| []" in seg or 'v-model' in tpl[max(0, m.start() - 40):m.start()], (
            f"这处 draft 下标没兜底，数据回来之前会是 undefined：{seg!r}")






def test_nav_is_generated_from_routes():
    """侧栏菜单曾是手写数组，与路由表两份——加了路由忘了加它，页面就进不去。
    必须从路由表生成。"""
    layout = (_REPO / "frontend" / "src" / "components" / "Layout.vue").read_text(encoding="utf-8")
    assert "router.getRoutes()" in layout, "侧栏又变回手写数组了"


# --- 设计语言：控件尺寸统一 ----------------------------------------------------

def test_control_size_is_set_globally_not_per_widget():
    """控件尺寸定在 `main.js` 的全局配置里，页面里不再逐个写 `size=`。

    改之前：55 处显式 `size="small"`、另外 32 处什么都不写（走 Element 的 default，
    比 small 高一档）。同一行筛选栏里输入框和按钮不一样高，就是这么来的——
    而且**新写的控件默认是错的**，得靠人记得加。

    登录页刻意用 `large`（独立页面、大输入框），是唯一的例外。
    """
    import re

    main_js = (_REPO / "frontend" / "src" / "main.js").read_text(encoding="utf-8")
    assert re.search(r"size:\s*['\"]small['\"]", main_js), \
        "main.js 里没设全局控件尺寸，新控件会各自走默认值"

    bad = []
    for f in sorted((_REPO / "frontend" / "src").rglob("*.vue")):
        if "Login" in str(f):
            continue                     # 登录页刻意放大
        # 只认 Element 的三档尺寸。别的 size=（图标像素 size="18"、分页 :size="pageSize"、
        # el-image 的 size="none"）与控件尺寸无关，一起查会产生假红——
        # 而假红的下场是有人把断言改松，等于没有守卫。
        for m in re.finditer(r'\bsize="(large|default|small)"', f.read_text(encoding="utf-8")):
            bad.append(f"{f.relative_to(_REPO)}: size=\"{m.group(1)}\"")
    assert not bad, (
        "这些地方又逐个写死了控件尺寸，会和全局默认打架：\n  " + "\n  ".join(bad)
        + "\n尺寸统一定在 main.js；确实要破例的（如登录页）请在本测试里显式豁免。")


def test_shared_toolbar_does_not_stretch_its_controls():
    """列表页共用的 `.gtn-toolbar` 必须显式写 `align-items`。

    flex 的默认值是 `stretch`：工具栏里混着输入框(24px)和日期范围选择器(32px)时，
    搜索框会被悄悄拉到 32px。同一行里两种高度，而且只在带日期筛选的页面上出现，
    很难联想到是 flex 干的——实测就是这么找出来的（浏览器里量到 wrapper 32px，
    但没有任何一条 CSS 规则匹配到它）。

    诚实说明：这是「防这行被删掉」的守卫，不是行为测试。真要验高度得开浏览器量，
    那不在 pytest 的射程内。所以断言只做一件事，不假装做更多。
    """
    import re

    css = (_REPO / "frontend" / "src" / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    rule = re.search(r"\.gtn-toolbar\s*\{([^}]*)\}", css)
    assert rule, "NotionTable.vue 里找不到 .gtn-toolbar 的样式规则"
    assert "align-items" in rule.group(1), \
        "`.gtn-toolbar` 没写 align-items，flex 默认 stretch 会把控件拉成不同高度"


def test_router_has_a_catch_all_so_bad_urls_are_not_a_blank_page():
    """未知路由必须有兜底。

    没有兜底时 vue-router 什么都不渲染——**连左侧导航都没有**，用户在界面里
    找不到任何退回去的办法。这不是假想：插件从 `scraper` 改名成 `plugin` 之后，
    旧书签 `#/scrapers` 就是整页空白（浏览器里实测过）。
    """
    src = (_REPO / "frontend" / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "pathMatch" in src, "router 没有兜底路由，写错的地址会渲染成整页空白"


def test_sidebar_order_covers_every_page():
    """侧栏菜单的 `ORDER` 数组必须覆盖**全部**页面路由。

    菜单本身是从路由表生成的（加页面只要写路由，菜单会自己长出来），但**顺序**
    仍靠这个手写数组；没列进去的自动排到最后（`ia < 0 ? 999`）。
    于是「加了新页面、忘了加进 ORDER」的表现是它悄悄躺在「设置」后面——
    `/fx`（日元汇率）就这么待过一阵子，而这种错位没有任何报错。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    layout = (root / "components" / "Layout.vue").read_text(encoding="utf-8")
    m = re.search(r"const ORDER = \[(.*?)\]", layout, re.S)
    assert m, "Layout.vue 里找不到 ORDER 数组"
    ordered = set(re.findall(r"'(/[a-z-]+)'", m.group(1)))

    router = (root / "router" / "index.js").read_text(encoding="utf-8")
    # 带 meta.title 的子路由就是会出现在菜单里的页面。
    # **逐行匹配**，不要写「花括号内无嵌套」的正则：路由是单行写的且内含 `meta: { ... }`，
    # 那种正则一个都匹配不到 —— 于是 `missing` 恒为空集，这条测试变成装饰品。
    # （第一版就是这么写的，删掉 `/fx` 之后它照样绿。）
    pages = set()
    for line in router.splitlines():
        m = re.search(r"path:\s*'([a-z-]+)'", line)
        if m and "title:" in line:
            pages.add("/" + m.group(1))
    assert len(pages) >= 8, f"路由解析失败（只认出 {sorted(pages)}）——空集合上的断言永远成立"
    missing = pages - ordered
    assert not missing, (
        f"这些页面没列进侧栏 ORDER，会被排到最后：{sorted(missing)}。"
        f"加页面时请一并决定它在菜单里的位置。"
    )


def test_fx_history_window_is_inclusive():
    """「近 N 天」就该是 N 天，不是 N+1 天。

    区间是闭的（`date >= since` 且今天也算），所以 `since` 必须是 `today - (N-1)`。
    写成 `today - N` 会多返回一天——看板按它算日均就偏低，而这种偏差没人会察觉。
    """
    import datetime as dt
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "fx.py").read_text(
        encoding="utf-8")
    assert re.search(r"dt\.timedelta\(days=days - 1\)", src), \
        "fx.py 的历史窗口又变回 `days`（会多返回一天）"


def test_fx_history_returns_exactly_n_days(client, session):
    """行为侧：造 10 天的汇率，问「近 7 天」必须正好回 7 天。"""
    import datetime as dt
    from decimal import Decimal

    from sqlmodel import delete

    from app.models import FxRate
    from app.services.fx import JST

    session.exec(delete(FxRate))
    session.commit()
    today = dt.datetime.now(JST).date()
    for i in range(10):
        session.add(FxRate(date=today - dt.timedelta(days=i),
                           rate=Decimal("20.0000"), source="boc"))
    session.commit()

    got = client.get("/api/fx/history", params={"days": 7}).json()
    assert len(got["items"]) == 7, f"「近 7 天」返回了 {len(got['items'])} 天"


def test_409_is_never_silent():
    """409 的失败方向必须是「多一条提示」，不是「什么都不显示」。

    原先的约定是「409 交给页面处理，拦截器不弹」。约定本身没错，错的是失败方向：
    页面忘了处理 → 点下去**完全没反应**，是最难排查的那种「没坏但也不动」。
    409 的调用点已经涨到 22 处，其中 6 处漏了处理——复发率证明「靠作者记住」不成立。

    现在拦截器延后一拍兜底：页面给了更好的提示就 `handled(e)` 取消，忘了就由它说话。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    http = (root / "api" / "http.js").read_text(encoding="utf-8")
    assert "export function handled" in http, "取消兜底的入口没了，页面无法避免重复提示"
    assert "pending409" in http and "setTimeout(" in http, "409 没有兜底提示"
    # 老写法：整段跳过 409。留着它就等于兜底从来不会触发。
    assert "status !== 409" not in http, \
        "拦截器又把 409 整段跳过了——兜底提示失效，漏处理的调用点重新变成静默"

    # 每个 `handled(` 的使用点都得先 import，否则运行时才炸（而 409 本就是少见分支）
    for f in sorted(root.rglob("*.vue")):
        src = f.read_text(encoding="utf-8")
        if "handled(e)" in src:
            assert "from '@/api/http'" in src, f"{f.name} 用了 handled() 却没 import"


def test_frontend_pre_checks_image_size_on_every_ocr_upload():
    """三条 OCR 上传路径都要在本机先量一下分辨率。

    后端 `MAX_OCR_PIXELS` 是硬拒绝（400），而且必须硬：降采样会掉小字体识别率，
    而**识别错的快递单号比识别不出来贵得多**（错号会精确匹配并挂到别人的订单上）。
    但硬拒绝不等于让用户白等——尺寸浏览器本地一读就知道，传上去再回 400
    在慢网络上是几十秒的空等。

    漏一条路径的表现最难受：另外两处会提前提示、这一处传完才报错，
    用户会以为是「这张图有什么特别」。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    gate = (root / "utils" / "imageGate.js").read_text(encoding="utf-8")
    # 判据必须与后端同一个数字，抄歪了就是两边说法不一致
    from app.services.ocr import MAX_OCR_PIXELS
    assert str(MAX_OCR_PIXELS).replace("000000", "_000_000") in gate or \
        f"{MAX_OCR_PIXELS:,}".replace(",", "_") in gate, "前端上限与后端对不上"

    uploads = {
        "views/Orders/index.vue": "ordersApi.ocr(",
        "views/Shipment/index.vue": "shipmentApi.ocr(",
    }
    for rel, call in uploads.items():
        src = (root / rel).read_text(encoding="utf-8")
        assert call in src, f"{rel} 里找不到 OCR 上传调用，这条守卫已经守错了地方"
        assert "checkImageSize" in src, f"{rel} 的 OCR 上传没有做分辨率预检"
    ship = (root / "views" / "Shipment" / "index.vue").read_text(encoding="utf-8")
    assert ship.count("checkImageSize(file)") >= 2, \
        "集运页有两条上传路径（成品包裹页 / 内含快递挂靠），预检只加了一条"


def test_no_page_sets_its_own_width_cap():
    """页面不许自己写死宽度上限——全站一个页宽。

    这条守卫是从一次真实的割裂里长出来的：汇率 900px、数据库 760px、设置 820px，
    其余页面占满宽度。四种页宽，而那三个数字彼此毫无关系，只是各写各的时随手定的。
    代价不只是「不好看」：汇率页主体是一张**表**，760/900 下右边白掉几百像素，
    数据库页的 DSN 那一列则被挤得折行。

    统一后的规则：**卡片占满宽度，卡片内的字段按可用宽度自动分列**
    （`.field-grid`，定义在 tokens.css，两页共用）。
    要收窄就收窄**控件**，不要收窄**页面**。
    """
    import re
    from pathlib import Path

    views = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "views")
    offenders = []
    for f in sorted(views.rglob("index.vue")):
        src = f.read_text(encoding="utf-8")
        m = re.search(r"<template>\s*<div class=\"([\w-]+)\"", src)
        if not m:
            continue                      # 根节点没带 class 的页面本来就没有这个问题
        root = m.group(1)
        style = src.split("<style", 1)[-1]
        # 只看**根节点那条规则**：控件级的 max-width（输入框收窄）是正当的
        for rule in re.findall(rf"\.{re.escape(root)}\s*\{{([^}}]*)\}}", style):
            if "max-width" in rule:
                offenders.append(f"{f.parent.name}: .{root} {{{rule.strip()}}}")
    assert not offenders, (
        "页面自己写死了宽度上限，会和其余页面长得不一样：\n  " + "\n  ".join(offenders)
        + "\n要收窄请收窄控件（或用 tokens.css 的 .field-grid 分列），不要收窄页面。")


def test_field_grid_is_defined_once_globally():
    """`.field-grid` 只能有一处定义。

    这三页要的是同一件事——各页抄一份必然漂移，它们原来那三个不一样的
    max-width 正是这么来的。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    defs = [f for f in list(root.rglob("*.vue")) + list(root.rglob("*.css"))
            if ".field-grid {" in f.read_text(encoding="utf-8")]
    assert [f.name for f in defs] == ["tokens.css"], \
        f".field-grid 被定义了多份：{[str(f) for f in defs]}"
    users = [f.parent.name for f in root.rglob("index.vue")
             if 'class="field-grid"' in f.read_text(encoding="utf-8")]
    assert set(users) >= {"Settings", "Database"}, f"没人用它了？当前使用者：{users}"


def test_both_tables_share_one_expanded_row_surface():
    """两张表的「展开明细区」必须是同一个面。

    这里原本是两个方向相反的取值：NotionTable 写死 `#10192c`（比卡片**深**，
    读作「从上面那行里翻出来的」），汇率表用 `--bg-head`（比卡片**浅**，读作「浮起来的一层」）。
    同一种交互两种层次语言，而且其中一个还是裸十六进制——tokens.css 的开篇就写着
    「新写样式一律取 var(--…)，不要再往 .vue 里写字面 hex」。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    tokens = (root / "styles" / "tokens.css").read_text(encoding="utf-8")
    assert "--bg-sunken" in tokens, "展开区的面没有 token，两边只能各写各的"

    nt = (root / "components" / "NotionTable.vue").read_text(encoding="utf-8")
    fx = (root / "views" / "Fx" / "index.vue").read_text(encoding="utf-8")
    for name, src, sel in (("NotionTable", nt, ".gtn-exp-row"), ("汇率表", fx, ".detail td")):
        line = next((l for l in src.splitlines() if l.strip().startswith(sel)), None)
        assert line and "var(--bg-sunken)" in line, \
            f"{name} 的展开区没有用 --bg-sunken：{line!r}"
    assert "#10192c" not in nt, "NotionTable 里又出现了裸色值 #10192c"


def test_fx_table_matches_the_shared_table_language():
    """汇率表是手写的（只读 + 按天展开，用不上 NotionTable 的列宽持久化/单元格编辑/
    幽灵新建行），但**看上去必须是同一个应用里的表**。

    钉住三处最扎眼的：外框、表头底色、纵向网格线。这三样原先一个都没有，
    于是它看起来像「几行字浮在卡片上」，而其余五页都是有边界的表格。
    """
    from pathlib import Path

    fx = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "views" / "Fx"
          / "index.vue").read_text(encoding="utf-8")
    import re

    style = fx.split("<style", 1)[-1]

    def rule(selector: str) -> str:
        """取某条选择器的声明块。**必须按选择器取**，不能在整段样式里 substring 搜——
        第一版就是那么写的：把表头底色改成 transparent 之后测试照样绿，
        因为 `.day.open td` 那条里也有 `background: var(--bg-hover)`。
        「在某处出现过」不等于「用在该用的地方」。"""
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", style)
        return m.group(1) if m else ""

    checks = {
        "外框（1px 边 + 圆角，同 .gtn-scroll）":
            ("border: 1px solid var(--border)", rule(".fx-scroll")),
        "表头底色（同 .gtn-th）":
            ("background: var(--bg-hover)", rule(".fx-tbl th")),
        "表头 2px 下边框（同 .gtn-th）":
            ("border-bottom: 2px solid var(--border)", rule(".fx-tbl th")),
        # 表头和表体**都要**有竖线。只查表体的话，把 th 上那条删掉测试照样绿，
        # 而屏幕上是「表头没有竖线、表体有」——比两边都没有更扎眼。
        "纵向网格线·表体（同 .gtn-td）":
            ("border-right: 1px solid var(--border)", rule(".fx-tbl td")),
        "纵向网格线·表头（同 .gtn-th）":
            ("border-right: 1px solid var(--border)", rule(".fx-tbl th")),
        "行高 36px（同 .gtn-td）":
            ("height: 36px", rule(".fx-tbl td")),
    }
    missing = [k for k, (want, got) in checks.items() if want not in got]
    assert not missing, f"汇率表与其余表格的共同视觉语言缺了：{missing}"
    # 等宽数字走 tokens.css 的共享规则，不再各页自己加一个类
    tokens = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "styles"
              / "tokens.css").read_text(encoding="utf-8")
    assert ".fx-tbl" in tokens.split("font-variant-numeric")[0].split("/* 金额必须等宽")[-1], \
        "汇率表没被共享的 tabular-nums 规则覆盖到"
