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
    }),
    ("Shipment", schemas.ShipmentCreate, schemas.ShipmentUpdate, {
        "jpy_settled",
        "orders",        # 关系列：挂靠子订单走 /api/shipment/{id}/order，不在 body 里
        "bind_express",  # 操作列（「内含快递」按钮），不是字段
    }),
    ("Staging", schemas.StagingCreate, schemas.StagingUpdate, {
        "scraped_at",    # 抓取时间，机器写
    }),
    ("Misc", schemas.MiscCreate, schemas.MiscUpdate, {
        "jpy_settled",   # 派生：结算日元，后端算
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
    """从页面列配置里粗取每个 `key: 'xxx'` 所在那一条的原文，供检查 clearable 标记。"""
    src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
    # 列定义要么是一行 `{ ... key: 'x' ... }`，要么跨几行；按 key 出现处向前后各取一段
    out = {}
    for m in re.finditer(r"key: '(\w+)'", src):
        start = src.rfind("{", 0, m.start())
        end = src.find("}", m.end())
        if start != -1 and end != -1:
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


# --- 筛选器的声明与使用必须双向一致 -------------------------------------------

_FILTER_PAGES = ["Orders", "Items", "Staging", "Shipment"]


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

    bad = []
    for page in _FILTER_PAGES:
        src = (_VIEWS / page / "index.vue").read_text(encoding="utf-8")
        m = _re.search(r"const filters = reactive\((\{[^}]*\})\)", src)
        assert m, f"{page}: 找不到 filters 的 reactive 初始化"
        declared = set(_re.findall(r"(\w+)\s*:", m.group(1)))
        used = set(_re.findall(r"filters\.(\w+)", src))
        if declared != used:
            bad.append(f"{page}: 声明了没人读 {sorted(declared - used) or '—'}；"
                       f"读了没声明 {sorted(used - declared) or '—'}")
    assert not bad, "筛选器键集对不上：\n  " + "\n  ".join(bad)
