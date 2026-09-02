"""写接口的字段契约：前端能发出去的每个键，后端都得认。

背景：写模型现在是 `extra="forbid"`。这挡住了「200 OK + 什么都没改 + 零日志」那类
静默失败（字段名漂移时 pydantic 默默丢弃 → setattr 空转 → 而 version 已经自增，
还会把别的客户端顶成 409）。代价是：前端只要多发一个键，保存就直接 422。

所以必须有守卫把两侧钉在一起：
  - 前端表格「新建行」发的是用户填过的那些 `col.key`（NotionTable.onNew → data[col.key]）；
  - 单元格保存与编辑面板发的是 `{version, [col.key]: v}`；
  - OCR 建单额外显式赋一批键。
下面逐页核对。**纯展示列**（派生金额、关系列）要写进各页的豁免名单——
名单是显式的，加新列时必须表态它是可写还是只展示。
"""
import re
from pathlib import Path

import pytest

from app import schemas

_REPO = Path(__file__).resolve().parents[2]
_VIEWS = _REPO / "frontend" / "src" / "views"


def _keys(page: str) -> set[str]:
    src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
    return set(re.findall(r"key: '(\w+)'", src))


# 每页：(列配置文件, 新建用的 schema, 改一格用的 schema, 只展示不可写的列)
_PAGES = [
    ("Orders", schemas.OrderCreate, schemas.OrderUpdate, {
        "jpy_settled",   # 派生：结算日元，后端算
        # `jpy_auto` = 按汇率算出来的日元（`jpy_settled` 是「覆盖值优先」的最终额）。
        # 两者并列显示才看得出「这一行被手工覆盖过」，但它同样是**派生**的，不可写。
        "jpy_auto",
    }),
    ("Shipment", schemas.ShipmentCreate, schemas.ShipmentUpdate, {
        "jpy_settled",
        # `jpy_auto` = 按汇率算出来的日元（`jpy_settled` 是「覆盖值优先」的最终额）。
        # 两者并列显示才看得出「这一行被手工覆盖过」，但它同样是**派生**的，不可写。
        "jpy_auto",
        # 到岸成本 = 子订单货款 + 本单国际运费。**派生且跨表**——它甚至不是集运表上的列，
        # 由 `_landed()` 从已加载的子订单算出来。可写的话就等于让人手改一个合计。
        "landed_jpy",
        "orders",        # 关系列：挂靠子订单走 /api/shipment/{id}/order，不在 body 里
        "bind_express",  # 操作列（「内含快递」按钮），不是字段
    }),
    ("Staging", schemas.StagingCreate, schemas.StagingUpdate, {
        "scraped_at",    # 抓取时间，机器写
    }),
    ("Misc", schemas.MiscCreate, schemas.MiscUpdate, {
        "jpy_settled",   # 派生：结算日元，后端算
        # `jpy_auto` = 按汇率算出来的日元（`jpy_settled` 是「覆盖值优先」的最终额）。
        # 两者并列显示才看得出「这一行被手工覆盖过」，但它同样是**派生**的，不可写。
        "jpy_auto",
    }),
]


@pytest.mark.parametrize("page,create,update,display_only",
                         _PAGES, ids=[p[0] for p in _PAGES])
def test_every_editable_column_key_is_a_writable_field(page, create, update, display_only):
    """列配置里的键，除豁免外都必须是写模型上的真字段。

    漏一个的表现是：用户在那一格填了东西 → 保存 422 →「保存失败」而说不清为什么。
    """
    writable = set(create.model_fields) | set(update.model_fields)
    unknown = _keys(page) - writable - display_only
    assert not unknown, (
        f"{page} 页这些列后端写模型不认，用户填了就会 422：{sorted(unknown)}\n"
        f"要么加进写模型，要么写进本测试的「只展示」名单并说明理由。")


@pytest.mark.parametrize("page,create,update,display_only",
                         _PAGES, ids=[p[0] for p in _PAGES])
def test_display_only_list_has_not_gone_stale(page, create, update, display_only):
    """豁免名单的元断言：名单里的列若已可写、或列本身已删除，就该从名单里去掉。

    豁免名单是最容易腐烂的一类守卫——留着旧名永远不会红，保护范围却在悄悄缩小。
    """
    writable = set(create.model_fields) | set(update.model_fields)
    keys = _keys(page)
    assert not (display_only & writable), (
        f"{page}：{sorted(display_only & writable)} 已经是可写字段，不该再列为「只展示」")
    assert not (display_only - keys), (
        f"{page}：{sorted(display_only - keys)} 已不是列配置里的键，名单该清理了")


def test_items_page_writes_go_through_the_order_schema():
    """物品页的格子最终 PATCH 的是**订单**（`ordersApi.update`），
    所以它的列键要么是订单可写字段，要么是物品行字段，要么纯展示。"""
    order_writable = set(schemas.OrderCreate.model_fields) | set(schemas.OrderUpdate.model_fields)
    item_fields = set(schemas.OrderItemIn.model_fields)
    display_only = {"amount_cny"}     # 单价×数量，前端算给人看
    unknown = _keys("Items") - order_writable - item_fields - display_only
    assert not unknown, f"物品页这些列没有对应的写字段：{sorted(unknown)}"


def test_ocr_created_order_keys_are_accepted():
    """OCR 建单会显式赋一批键再走 `ordersApi.create`。

    这批键是在 JS 里手写的（`data.xxx = ...`），不受列配置约束——改名时最容易漏，
    而漏掉的表现是「识别出来了但建单失败」或（forbid 之前）「建单成功但字段丢了」。
    """
    src = (_VIEWS / "Orders" / "index.vue").read_text(encoding="utf-8")
    assigned = set(re.findall(r"\bdata\.(\w+)\s*=", src))
    unknown = assigned - set(schemas.OrderCreate.model_fields)
    assert not unknown, f"OCR 建单赋了后端不认的键：{sorted(unknown)}"


# --- 查询参数：旧名必须响亮地失败 ---------------------------------------------

def test_legacy_status_query_param_is_rejected(client):
    """四个端点的旧参数名 `?status=` 一律 400。

    FastAPI 对**未知** query 参数默认忽略。所以改名之后若有调用方没跟上，
    表现是「筛选点了没反应、悄悄返回全量、HTTP 200、日志里什么都没有」——
    在一个记账程序里，这等于给人看了一份不是他要的账。宁可 400。
    """
    for path in ("/api/orders", "/api/items", "/api/shipment", "/api/staging"):
        r = client.get(path, params={"status": "已签收"})
        assert r.status_code == 400, f"{path}?status= 没被拒绝（{r.status_code}），会静默返回全量"
        assert "改名" in r.json()["detail"], f"{path} 的报错没告诉调用方新名字是什么"


def test_new_status_query_params_actually_filter(client):
    """光拒绝旧名不够，新名得真的在筛。"""
    s = client.post("/api/shipment", json={"date": "2026-11-01", "shipment_no": "QP-1",
                                           "shipment_status": "已发出"}).json()
    o = client.post("/api/orders", json={
        "date": "2026-11-01", "order_no": "QP-O1", "platform": "淘宝",
        "purchase_status": "已签收", "shipment_order_id": s["id"],
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "1"}]}).json()

    def ids(path, **params):
        return [x["id"] for x in client.get(path, params={**params, "limit": 200}).json()["items"]]

    assert o["id"] in ids("/api/orders", fulfillment_status="已发出"), "按显示口径筛不到"
    assert o["id"] not in ids("/api/orders", fulfillment_status="已签收"), "显示的是集运状态，不该被自身状态筛出"
    assert o["id"] in ids("/api/orders", purchase_status="已签收"), "按订单自身采购段状态筛不到"
    assert s["id"] in ids("/api/shipment", shipment_status="已发出"), "集运筛选失效"


# --- 前端筛选参数必须是后端真声明过的 -----------------------------------------

_PAGE_ENDPOINT = {
    "Orders": "/api/orders", "Items": "/api/items",
    "Shipment": "/api/shipment", "Staging": "/api/staging", "Misc": "/api/misc",
}


def _declared_query_params(path: str) -> set[str]:
    """该 GET 端点声明的 query 参数名。

    走 OpenAPI schema 而不是遍历 `app.routes`：本版 FastAPI 把 include_router 进来的
    路由包在 `_IncludedRouter` 里，`app.routes` 不展开它们，遍历只能看到 /docs 那几条
    ——那样这条测试会因为「一个端点都找不到」而报错，或者（更糟）静默通过。
    """
    from app.main import app

    spec = app.openapi()
    item = spec["paths"].get(path, {}).get("get")
    assert item is not None, f"OpenAPI 里没有 GET {path}"
    return {p["name"] for p in item.get("parameters", []) if p.get("in") == "query"}


@pytest.mark.parametrize("page,path", sorted(_PAGE_ENDPOINT.items()))
def test_frontend_filter_params_exist_in_backend(page, path):
    """前端 `params.X = ...` 里的每个 X，后端都得声明过。

    FastAPI 对未知 query 参数默认忽略 → 前端改名漏改（或后端改名前端没跟上）的表现是
    「筛选点了没反应、返回全量、HTTP 200、零日志」。这类失败在现有测试体系里
    **完全没有覆盖**：路径比对只比路径，不比参数。
    """
    src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
    used = set(re.findall(r"params\.(\w+)\s*=", src))
    declared = _declared_query_params(path)
    unknown = used - declared
    assert not unknown, (
        f"{page} 页发的这些查询参数后端不认，会被静默忽略、返回全量：{sorted(unknown)}\n"
        f"后端 GET {path} 声明的是：{sorted(declared)}")


# --- forbid 必须递归覆盖，不能只撒在顶层 --------------------------------------

# 请求体的**根**模型。从这里出发递归遍历字段注解里出现的每一个 SQLModel 子类。
_REQUEST_ROOTS = [
    "OrderCreate", "OrderUpdate",
    "StagingCreate", "StagingUpdate",
    "ShipmentCreate", "ShipmentUpdate",
    "MiscCreate", "MiscUpdate",
]

# 显式豁免：写清楚为什么这个嵌套模型可以不 forbid。空着就是「没有豁免」。
_FORBID_EXEMPT: dict[str, str] = {}


def _nested_models(model, seen=None):
    """递归收集一个写模型的字段注解里出现的所有 SQLModel 子类（含自身）。"""
    import typing

    from sqlmodel import SQLModel

    seen = seen if seen is not None else set()
    if model in seen:
        return seen
    seen.add(model)
    for field in model.model_fields.values():
        stack = [field.annotation]
        while stack:
            ann = stack.pop()
            stack.extend(typing.get_args(ann))
            if isinstance(ann, type) and issubclass(ann, SQLModel):
                _nested_models(ann, seen)
    return seen


def test_every_nested_write_model_forbids_extra_keys():
    """请求体里的**每一层**都必须 extra="forbid"，不只是顶层。

    只在顶层撒 forbid 是个必然会漏的做法：`items[]` 就漏过——它恰好是整个请求体里
    唯一装钱的地方，单价键名写错时 pydantic 静默丢弃，物品记成 0.00 + auto=True，
    订单价派生成 ¥0.00 而接口返回 200。同一个错名放在顶层是 422，一层之隔两种行为。

    这条测试的价值不在于钉住 items 那一处，而在于让「下一个嵌套写模型」不可能再漏。
    """
    bad = []
    for root_name in _REQUEST_ROOTS:
        root = getattr(schemas, root_name)
        for model in _nested_models(root):
            name = model.__name__
            if name in _FORBID_EXEMPT:
                continue
            if model.model_config.get("extra") != "forbid":
                bad.append(f"{name}（经 {root_name} 可达）")
    assert not bad, (
        "这些写模型没有 extra=\"forbid\"，错名键会被静默丢弃：\n  "
        + "\n  ".join(sorted(set(bad)))
        + "\n加 `model_config = _FORBID`；确有理由不加就写进 _FORBID_EXEMPT 并说明。")


def test_forbid_exemptions_have_not_gone_stale():
    """豁免名单里的模型必须还存在且确实可达，否则名单会变成过期的免死金牌。"""
    reachable = {m.__name__ for r in _REQUEST_ROOTS for m in _nested_models(getattr(schemas, r))}
    stale = set(_FORBID_EXEMPT) - reachable
    assert not stale, f"这些豁免项已经不在任何请求体里了，请删掉：{sorted(stale)}"


# --- 必填列不许在界面上被「清除」 ---------------------------------------------

# 每页：(列配置文件, 该页行背后的表模型, 该页允许清空的 NOT NULL 列 → 理由)
_CLEARABLE_PAGES = [
    ("Orders", "Order", {}),
    ("Shipment", "ShipmentOrder", {}),
    ("Misc", "MiscExpense", {}),
    # 暂存页的列背后是 OrderStaging，它自己那几列都是可空的（爬虫回灌、未导入行编辑都合法要 null）。
    # 「已导入行不许把账本必填列写空」这条约束在写穿那一刻由 routers/staging.py 挡，不在这里。
    ("Staging", "OrderStaging", {}),
]


def _column_options(page: str) -> dict[str, str]:
    """从页面列配置里取每个 `key: 'xxx'` 所属的**完整**列定义原文，供检查 clearable 标记。

    结束花括号必须**配平**着找，不能 `find("}")` 拿第一个：列定义里可以有嵌套对象
    （`options: {...}`、`lock: (row) => ({...})`），拿内层的 `}` 会把片段截断，
    让本来标了 `clearable: false` 的列看起来没标 —— 那是假红。假红比假绿安全，
    但同样有害：没人愿意留一条时不时无故变红的测试，最后它会被删掉。
    """
    src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"key: '(\w+)'", src):
        start = src.rfind("{", 0, m.start())
        if start == -1:
            continue
        depth, end = 0, -1
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            out[m.group(1)] = src[start:end + 1]
    return out


@pytest.mark.parametrize("page,model_name,exempt",
                         _CLEARABLE_PAGES, ids=[p[0] for p in _CLEARABLE_PAGES])
def test_not_null_columns_are_marked_not_clearable(page, model_name, exempt):
    """界面上可编辑、且背后是 NOT NULL 列的，必须标 `clearable: false`。

    不标的后果不是「保存失败」而是「保存失败得莫名其妙」：null 一路走到 commit 被数据库
    拦成 409「数据完整性冲突」，而前端把 409 当乐观锁冲突，弹「数据已变，已刷新」并整表重拉。
    用户既没看懂错在哪，也不知道自己那一步本来就不允许。

    这条测试的意义是**防漂移**：上一轮修这个问题时只标了两个 select 列，date 分支根本没接
    `col.clearable`，text 分支的空串→null 也没考虑。现在把「前端标了什么」和「数据库要求什么」
    对账，加列或改可空性时会自动红。
    """
    from app import models

    model = getattr(models, model_name)
    cols = _column_options(page)
    bad = []
    for key, snippet in cols.items():
        col = model.__table__.columns.get(key)
        if col is None or col.nullable:
            continue
        if key in exempt:
            continue
        if "readonly: true" in snippet or "readonly:true" in snippet:
            continue          # 只读列根本进不了编辑态
        if "clearable: false" not in snippet.replace(" ", " "):
            bad.append(f"{page}/{key}")
    assert not bad, (
        "这些列背后是 NOT NULL，但界面上可以被清除：\n  " + "\n  ".join(bad)
        + "\n给列配置加 `clearable: false`；确有理由允许清空就写进 _CLEARABLE_PAGES 的豁免表并说明。")


def test_the_writable_page_lists_cover_every_editable_page():
    """两张按页写死的名单（`_PAGES` / `_CLEARABLE_PAGES`）必须覆盖每一个可直接编辑的页面。

    「可编辑」有个结构判据：页面自己有 `async function saveCell(`。
    `Items` 页没有——它整表 `readonly: true`，改东西走 `OrderEditPanel`，
    所以它不在这两张名单里是对的，而不是漏了。

    这条断言的作用是**把漏掉变成红的**：新增一个能改格子的页面时，
    上面那两条守卫（写契约、必填列不许清空）不会自己长出覆盖来，
    它们只会安静地少验一页。
    """
    editable = _pages_with("async function saveCell(")
    assert {p for p, *_ in _PAGES} == editable, (
        f"写契约名单 _PAGES 覆盖 {sorted(p for p, *_ in _PAGES)}，"
        f"而磁盘上能改格子的是 {sorted(editable)}"
    )
    assert {p for p, *_ in _CLEARABLE_PAGES} == editable, (
        f"必填列名单 _CLEARABLE_PAGES 覆盖 {sorted(p for p, *_ in _CLEARABLE_PAGES)}，"
        f"而磁盘上能改格子的是 {sorted(editable)}"
    )


# --- 筛选器的声明与使用必须双向一致 -------------------------------------------

def _pages_with(marker: str) -> set[str]:
    """磁盘上 `index.vue` 含 `marker` 的页面名。"""
    return {
        d.name for d in sorted(_VIEWS.iterdir())
        if (d / "index.vue").is_file()
        and marker in (d / "index.vue").read_text(encoding="utf-8")
    }


def test_filter_keys_declared_and_used_match_exactly(subtests=None):
    """每页 `filters` 的 `reactive({...})` 键集，必须与页面里出现的 `filters.X` 集合**完全相等**。

    两个方向都要管，缺一不可：
      · 读了没声明 → 该键的**默认值丢失**。暂存页就这么坏的：状态筛选改名成 importStatus，
        模板与 load() 都跟着改了，唯独 reactive({...}) 还写着 `status: '待处理'`，
        于是 `filters.importStatus` 初值是 undefined，首屏不再默认只看「待处理」，
        而紧邻的注释还写着「默认只看待处理」。Vue 的 reactive 允许写入未声明的键，所以不报错。
      · 声明了没人读 → 死键。这正是上面那种漏改能成立的土壤：只要允许残渣留着，
        下一次改名照样能把一个**带默认值**的键变成死键而无人察觉。
    """
    import re as _re

    # **名单不写死。** 原先钉着四页（Orders/Items/Staging/Shipment），`Misc` 从没进过
    # 视野——它有同样的筛选栏、同样的 reactive 声明，只是没人想起它。硬名单不会报错，
    # 它只是安静地不覆盖新页。
    filter_pages = _pages_with("const filters = reactive(")
    # 反空转：发现方式一旦失效（换个写法、改成 ref），名单会变成空集，
    # 上面那个循环就一句话也不验、照样全绿。所以拿另一条独立线索对一遍：
    # 模板上挂着 `applyFilters` 的页，就是有筛选栏的页，两边必须一模一样。
    assert filter_pages == _pages_with('@change="applyFilters"'), (
        f"筛选页的两条线索对不上：声明 reactive 的是 {sorted(filter_pages)}，"
        f"模板挂 applyFilters 的是 {sorted(_pages_with('@change=\"applyFilters\"'))}。"
        "多半是发现方式过期了——这条测试会因此变成空转。"
    )

    bad = []
    for page in sorted(filter_pages):
        src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
        m = _re.search(r"const filters = reactive\((\{[^}]*\})\)", src)
        assert m, f"{page}: 找不到 filters 的 reactive 初始化"
        declared = set(_re.findall(r"(\w+)\s*:", m.group(1)))
        used = set(_re.findall(r"filters\.(\w+)", src))
        if declared != used:
            bad.append(f"{page}: 声明了没人读 {sorted(declared - used) or '—'}；"
                       f"读了没声明 {sorted(used - declared) or '—'}")
    assert not bad, "筛选器键集对不上：\n  " + "\n  ".join(bad)


# --- 设计 token 不许再漂 ------------------------------------------------------

_TOKENS_CSS = _REPO / "frontend" / "src" / "styles" / "tokens.css"

# 这些取值有 token 了，`.vue` 的 <style> 里不许再出现字面量。
# 之所以按「值」而不是按「数量」卡：数量阈值会随页面增加而失效，而一个已经有 token
# 的颜色再次出现字面量，就是一次确凿的漂移。
_TOKENIZED = {
    "#606266": "Element 亮色灰，在暗底上只有 2.78:1，低于 WCAG AA → var(--txt-2)",
    "#909399": "→ var(--txt-3)",
    "#c0c4cc": "→ var(--txt-2)",
    "#9ba8bf": "→ var(--txt-2)",
    "#7d8aa3": "→ var(--txt-3)",
    "#5b6880": "→ var(--txt-3)",
    "#c7d2e6": "→ var(--txt-body)",
    "#d6deea": "→ var(--txt-body)",
    "#409eff": "Element 默认蓝，与品牌蓝 #1890ff 并排出现 → var(--brand)",
    "#1890ff": "→ var(--brand)",
    "#67c23a": "Element 绿，与家规的 emerald 并排出现 → var(--ok)",
    "#e6a23c": "→ var(--warn)",
    "#f56c6c": "→ var(--danger)",
    "#28354a": "→ var(--border)",
    "#2a3446": "→ var(--border)",
    "#0b1220": "→ var(--bg-page)",
    "#131c2f": "→ var(--bg-card)",
}


def test_design_tokens_exist():
    """token 层必须存在且被 main.js 引入——否则下面那条守卫会变成一句空话。"""
    assert _TOKENS_CSS.exists(), "缺 frontend/src/styles/tokens.css"
    main = (_REPO / "frontend" / "src" / "main.js").read_text(encoding="utf-8")
    i_el = main.index("theme-chalk/dark/css-vars.css")
    i_tok = main.index("styles/tokens.css")
    assert i_el < i_tok, "tokens.css 必须排在 Element 暗色变量之后，否则 --el-color-* 覆盖不生效"


def test_no_style_block_reintroduces_a_tokenized_color():
    """`.vue` 里不许再写已经有 token 的颜色字面量——**模板属性与 script 字面量一样查**。

    审计基线：全仓 222 处硬编码 hex / 54 个不同值，同一语义角色最多分裂成 4 套；
    其中 Settings/Database 两页是照 Element **亮色**默认值写的，而应用恒暗色——
    `#606266` 在卡片底上只有 2.78:1，13px 正文，是实打实的可读性缺陷而非审美问题。
    """
    import re

    def _rgb_to_hex(m):
        """把 rgb(24, 144, 255) 归一成 #1890ff——否则换个记法就绕过整条守卫。"""
        r, g, b = (int(x) for x in m.groups())
        return f"#{r:02x}{g:02x}{b:02x}"

    bad = []
    for path in sorted((_REPO / "frontend" / "src").rglob("*.vue")):
        src = path.read_text(encoding="utf-8")
        # **整份文件**都查，不只 <style>：`Layout.vue` 的 `text-color="#9ba8bf"`
        # 与 `Dashboard/index.vue` 的 `color: '#1890ff'` 用的正是名单内的值，
        # 却因为写在模板属性 / script 字面量里而合法通过——守卫只盖了三分之一。
        text = re.sub(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", _rgb_to_hex, src.lower())
        for lit, hint in _TOKENIZED.items():
            if lit in text:
                bad.append(f"{path.relative_to(_REPO)}: {lit}  {hint}")
    assert not bad, "这些颜色已经有 token 了，别再写字面量：\n  " + "\n  ".join(sorted(set(bad)))


def test_item_editor_never_silently_drops_a_nameless_row():
    """物品编辑器**不许**把没名字的行连同它的钱一起静默丢掉。

    原先 `saveItems` 第一行是 `.filter(有名字)`。于是「Ctrl+A 清掉名字准备重打」期间，
    任何一次其它保存（改数量、改单价、改另一条物品）都会把那条删掉，订单金额随之缩水
    ——无确认、无撤销、无提示，用户只看到「我改了个数量，另一行怎么没了」。
    `onItemEdit` 里那道守卫只挡住「改这一条本身」，挡不住「改别的东西时顺带整体保存」，
    所以守卫必须在 `saveItems` 这条所有入口的必经之路上。

    真正的删除只走 `removeItem`（有二次确认）。
    """
    src = (_REPO / "frontend" / "src" / "components" / "OrderItemsEditor.vue").read_text(
        encoding="utf-8")
    body = src[src.index("async function saveItems"):]
    body = body[:body.index("\n}\n")]
    assert ".filter((it) => it.name" not in body, \
        "saveItems 又在按名字过滤了——那是静默丢数据"
    assert "blank" in body and "warning" in body, \
        "saveItems 必须挡下空名行并提示，而不是替用户删掉"


# 每页：(页, 加载调用, 加载函数名, 失败标志名)。后两项各页不同名，不能写死成
# `load` / `loadFailed`——设置页叫 `fxFailed`、数据库页叫 `loadBackups`/`backupsFailed`，
# 按死锚点找的话它们会**整页落在守卫外**，而它们显示的正是同一句「加载失败」。
_LOADER_PAGES = [
    ("Dashboard", "dashboardApi.get", "load", "loadFailed"),
    ("Items", "itemsApi.list", "load", "loadFailed"),
    ("Orders", "ordersApi.list", "load", "loadFailed"),
    ("Staging", "stagingApi.list", "load", "loadFailed"),
    ("Shipment", "shipmentApi.list", "load", "loadFailed"),
    ("Misc", "miscApi.list", "load", "loadFailed"),
    # 这两页的空态**在断言用户的系统状态**（「装上汇率插件」/「plugins 下没有目录」），
    # 请求失败时照样显示的话，给出的是一条错误的行动指令——比单纯的假空态更贵。
    ("Fx", "fxApi.history", "load", "loadFailed"),
    ("Plugins", "pluginsApi.list", "load", "loadFailed"),
    # 备份列表的空态是「还没有备份。」——拉失败时照样显示的话，人会以为备份没了，
    # 而这一页紧接着就是「切换数据源」，那是本仓最贵的一个操作。
    ("Database", "dbApi.backups", "loadBackups", "backupsFailed"),
    # 汇率那一栏的空态是「库里没有汇率」，会把人引去装汇率插件；拉失败时它在说假话。
    ("Settings", "fxApi.get", "load", "fxFailed"),
]


def test_every_page_that_shows_load_failed_is_in_the_family():
    """凡是会显示 `MSG_LOAD_FAILED` 的页面，都必须在 `_LOADER_PAGES` 里。

    名单写死就会**安静地不覆盖**新页：这条守卫原先钉着八页，而 `Database` 与
    `Settings` 同样显示那句「加载失败」，只因为把加载函数叫 `loadBackups`、
    把标志叫 `fxFailed`，就整页落在守卫外——不是它们不合规，是没人在看。

    用 `<=` 而不是 `==`：显示那句话的页必须被覆盖，反之不必——
    `Dashboard` 的失败态是自己的文案（「总支出」那一屏不适合套通用句），它在名单里，
    但不引用这个常量。
    """
    covered = {p for p, *_ in _LOADER_PAGES}
    shows = {
        d.name for d in sorted(_VIEWS.iterdir())
        if (d / "index.vue").is_file()
        and "MSG_LOAD_FAILED" in (d / "index.vue").read_text(encoding="utf-8")
    }
    assert shows <= covered, (
        f"这些页显示「加载失败」却没被守卫覆盖：{sorted(shows - covered)}。"
        "请连同它的加载函数名与失败标志名一起加进 _LOADER_PAGES。"
    )


@pytest.mark.parametrize("page,loader,fn_name,flag", _LOADER_PAGES)
def test_list_pages_do_not_render_failure_as_emptiness(page, loader, fn_name, flag):
    """请求失败**不能**渲染成「空」。

    这两页原先只有 `try/finally`：接口挂了，Dashboard 保持初值全 0（「总支出 ¥0、
    暂无数据」），Items 保持空数组（「没有符合条件的记录 / 共 0 条」）。
    而拦截器那句 toast 3 秒后就消失——此后这一屏与「真的还没记过账」完全无法区分。
    对一个记账工具，「你的账全没了」是最不该让人误会的一句话。

    判据：加载函数必须有 catch 分支，并把失败状态留在页面上（而不是只依赖 toast）。
    """
    src = (_REPO / "frontend" / "src" / "views" / page / "index.vue").read_text(encoding="utf-8")
    assert loader in src, f"{page} 的加载调用变了，这条测试要跟着更新"

    # **按结构判，不是搜标识符。** 原先只有 `"loadFailed" in src` 与
    # `"loadFailed" in tpl` 两句：把 `loadFailed.value = true` 那一行整个删掉，
    # 标识符仍然留在模板的 `:empty-text` 里，**测试照样绿**——守卫在保护一个
    # 永远为 false 的变量。三段各钉一环，缺一环这条链就断了。
    body = re.sub(r"//.*$", "", src[src.index("<script"):], flags=re.M)   # 剥注释再判
    # 锚点要带括号：Orders/Staging 里还有 `async function loadAccounts()`，
    # 用 "async function load" 会切到那个（它自己带 catch，于是这条守卫在验别人的代码）。
    m = re.search(rf"^async function {fn_name}\(\) \{{", body, re.M)
    assert m, f"{page} 里找不到 {fn_name}()——这条测试要跟着更新"
    fn = body[m.start():]
    end = re.search(r"^(?:async )?function |^const |^onMounted", fn[1:], re.M)
    fn = fn[:end.start() + 1] if end else fn
    assert re.search(r"\}\s*catch\b", fn), f"{page} 的 {fn_name}() 没有 catch——失败会被静默吞掉"
    assert re.search(rf"{flag}\.value\s*=\s*true", fn), \
        f"{page} 捕获了失败却没把状态留下来"
    assert re.search(rf"{flag}\.value\s*=\s*false", fn), \
        f"{page} 成功时没有清掉失败标志——失败一次就永远显示失败"
    tpl = src[:src.index("<script")]
    assert flag in tpl, f"{page} 定义了 {flag} 却没在模板里用——等于没修"


def test_user_facing_copy_has_no_bare_markdown():
    """用户可见的文案里不写 markdown 强调符。

    Element Plus 的 message/alert 都**不解析** markdown，`**不是**` 会原样显示成
    带星号的字面量——在一句本来就紧张的提示里（「你的账本没了」「未切换」）
    多出两对星号，读起来像是界面坏了。

    口径：模板里要强调就用真的 `<b>`；纯字符串（ElMessage 的 message 参数）里
    靠措辞和「」，因为那里连 HTML 都不解析。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    # **只查字符串字面量**。第一版按行跳过注释，结果误报一片：行尾注释、多行注释的
    # 中间行都被当成代码。而真正会显示给用户的文案（ElMessage 的 message、
    # tooltip 的 content、`{{ col.help }}`）全都是字符串字面量——查那里既准又够。
    # 代价：模板里的裸文本（不在引号内）查不到；那种星号会直接显示在页面上，
    # 肉眼 review 时反而最容易发现。
    lit = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`", re.S)
    bad = []
    for f in sorted(root.rglob("*.vue")) + sorted(root.rglob("*.js")):
        text = f.read_text(encoding="utf-8")
        for m in lit.finditer(text):
            val = next((g for g in m.groups() if g), "")
            if re.search(r"\*\*[^*]+\*\*", val):
                line_no = text[:m.start()].count("\n") + 1
                bad.append(f"{f.relative_to(root)}:{line_no}: {val.strip()[:70]}")
    assert not bad, "用户可见文案里有裸 markdown（Element 不解析，会显示成字面星号）：\n" + "\n".join(bad)


def test_humans_cannot_write_into_the_plugin_private_namespace(client):
    """人类令牌写「插件私有存储」→ 400，而不是 200 + 一行谁都读不回来的数据。

    这类数据按 plugin_id 分命名空间，人没有 plugin_id，会落进 `plugin_id="?"`；
    而读接口（GET /api/plugins/records/{kind}）明确拒绝人类令牌 ⇒ **写得进、读不出**。
    「200 OK、库里真的多了行、然后永远拿不出来」比直接报错难查得多。
    """
    r = client.post("/api/plugins/ingest", json={
        "kind": "plugin.record",
        "items": [{"kind": "note", "key": "k1", "data": {"a": 1}}],
    })
    assert r.status_code == 400, r.text
    assert "插件令牌" in r.json()["detail"]


def test_humans_can_still_write_the_ledger_kinds(client):
    """反面：别的 kind 人类本来就该能写（手工补录、排障），别一刀切。"""
    r = client.post("/api/plugins/ingest", json={
        "kind": "fx.rate", "items": [{"rate": "20.5000", "source": "manual-test"}]})
    assert r.status_code == 200, r.text


def test_dashboard_tells_stale_data_apart_from_an_empty_ledger():
    """看板加载失败时，**「从没成功过」与「成功过但这次失败」得说两句不同的话**。

    `load()` 的 catch 分支一个字节都不改 `data`。所以：
      · 首次就失败 → 屏幕上是全 0 的初值 → 「下面显示的不是你的账本」是准的；
      · 成功过、重试又失败 → 屏幕上留着的**恰恰就是**用户的账本（只是旧的）
        → 同一句话变成假话，用户会以为数据没了、或者以为这些数字是垃圾。

    这条修复的初衷本来是「别让失败长得像空账本」，而那半句是**反过来的同一类错误**：
    让「有点旧的真数据」长得像「不是你的数据」。记账工具在这两个方向上都不该说错。

    另外钉住控制台留痕：toast 三秒就没了、alert 只说结论不说原因，
    事后要查「是网络断了还是后端 500」就只剩这一条。
    """
    src = (_REPO / "frontend" / "src" / "views" / "Dashboard" / "index.vue").read_text(encoding="utf-8")
    tpl = src[:src.index("<script")]
    assert "loadedAt" in tpl, "模板里没有区分两种失败的分支"
    assert re.search(r'v-if="loadedAt"', tpl), "没有按「以前成功过吗」分岔的条件"
    assert "只是初值" in tpl and "可能已经过时" in tpl, "两种情形的文案不全"

    body = src[src.index("async function load"):]
    body = body[:body.index("\nonMounted")]
    body = re.sub(r"//.*$", "", body, flags=re.M)
    assert "loadedAt.value =" in body, "成功时没记下时刻——那两句话就分不出来"
    assert "console.error" in body, "失败没有留下任何可事后排查的痕迹"
