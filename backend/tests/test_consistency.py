"""跨层一致性：后端枚举 ↔ 前端常量 ↔ 爬虫插件映射。
这些常量分散在三处、只能靠约定同步——这里把约定变成断言，改一处漏改另一处即红。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import ORDER_STATUS_RANK, OrderStatus, ShipmentStatus, StagingStatus

_REPO = Path(__file__).resolve().parents[2]
_CONSTANTS_JS = _REPO / "frontend" / "src" / "constants.js"
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
    assert _js_array(constants_js, "ORDER_STATUS") == [s.value for s in OrderStatus]


def test_frontend_shipment_status_matches_backend(constants_js):
    assert _js_array(constants_js, "SHIPMENT_STATUS") == [s.value for s in ShipmentStatus]


def test_frontend_staging_status_matches_backend(constants_js):
    assert _js_array(constants_js, "STAGING_STATUS") == [s.value for s in StagingStatus]


def test_frontend_status_rank_matches_backend():
    """Orders 页的 STATUS_RANK 决定 OCR 合并时状态能否推进；与后端 ORDER_STATUS_RANK 必须一致。"""
    text = _ORDERS_VUE.read_text(encoding="utf-8")
    m = re.search(r"const STATUS_RANK\s*=\s*\{(.*?)\}", text, re.S)
    assert m, "没在 Orders/index.vue 里找到 STATUS_RANK"
    fe = {k: int(v) for k, v in re.findall(r"([^\s,{]+)\s*:\s*(\d+)", m.group(1))}
    assert fe == ORDER_STATUS_RANK


def test_scraper_status_map_targets_are_valid(constants_js):
    """爬虫 STATUS_MAP 的目标值必须都是后端 OrderStatus 的合法值，否则回灌整批 422。"""
    if not _SCRAPER_NORMALIZE.is_file():
        pytest.skip("插件未安装")
    text = _SCRAPER_NORMALIZE.read_text(encoding="utf-8")
    m = re.search(r"STATUS_MAP\s*=\s*\{(.*?)\n\}", text, re.S)
    assert m, "没找到 STATUS_MAP"
    targets = {v for _, v in re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1))}
    valid = {s.value for s in OrderStatus}
    assert targets <= valid, f"爬虫会推出后端不接受的状态：{sorted(targets - valid)}"


def test_status_rank_covers_all_forward_statuses():
    """生命周期里「会前进」的状态都要有 rank；退款/交易关闭刻意不在表里（取 -1）。"""
    from app.models import order_status_rank
    side = {OrderStatus.refunded.value, OrderStatus.cancelled.value}
    for s in OrderStatus:
        if s.value in side:
            assert order_status_rank(s.value) == -1
        else:
            assert order_status_rank(s.value) >= 0, f"{s.value} 缺 rank"


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
    assert "key: 'status', label: '状态', type: 'select'" in src, "状态列不再是 select 单元格（点标签选）"
    assert "#cell-status" not in src, "状态列又被自定义槽接管了，交互会和其它标签列不一致"


def test_status_and_items_lock_when_attached():
    """挂靠集运单后这两格锁定：值由别处决定，能点开改只会让人困惑
    （改的是被遮住的国内段状态，改完看不出变化）。"""
    src = _orders_vue()
    for key in ("'status'", "'items'"):
        blk = src.split(f"key: {key}")[1][:400]
        assert "lock: (row) => !!row.shipment_order_id" in blk, f"{key} 列没有按挂靠状态锁定"
        assert "lockHint" in blk, f"{key} 列锁定了却没给提示，用户不知道为什么点不动"


def test_status_cell_displays_inherited_value():
    """锁定期间要显示**继承来的**集运状态，而不是订单自己的国内段状态。"""
    blk = _orders_vue().split("key: 'status'")[1][:400]
    assert "display: (row) => row.effective_status" in blk


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
