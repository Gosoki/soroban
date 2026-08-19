"""状态体系的结构守卫。

本项目反复出问题的地方是「状态」：同一个词在不同段指不同的事，或者同一件事在两处
各存一份。这里把状态体系的**结构性约束**写成断言——不是检查某个具体名字对不对，
而是检查「新增一个状态/一段业务时，作者必须显式表态」。

命名公理（`docs/审计报告-2026-08.md` 记录了推导）：
    状态列名 = <业务段>_status。同名 ⟺ 同概念（同一枚举、同一值域）。
    派生的跨段合流值必须用另一个词，且只出现在只读模型上。
"""
import inspect
from enum import Enum

import pytest

from app.models import (
    EXCLUDED_STATUSES,
    PURCHASE_STATUS_RANK,
    PURCHASE_TERMINAL_STATUSES,
    MiscExpense,
    Order,
    PurchaseStatus,
    ShipmentOrder,
)
from app.models import base as models_base


def _status_enums() -> dict[str, type]:
    """自动收集 `app.models.base` 里所有名字以 Status 结尾的字符串枚举。

    自动收集而不是硬编码清单：加 `SaleStatus` 时它**自动**被纳入下面所有约束，
    不需要有人记得来改测试。硬编码的清单是会腐烂的那种守卫。
    """
    return {
        name: obj
        for name, obj in vars(models_base).items()
        if isinstance(obj, type) and issubclass(obj, Enum) and issubclass(obj, str)
        and name.endswith("Status")
    }


def test_there_is_more_than_one_status_enum():
    """元断言：收集逻辑挂了（比如枚举挪了模块）会让下面几条全部空跑而依然全绿。"""
    found = _status_enums()
    assert len(found) >= 3, f"只收集到 {sorted(found)}，收集逻辑多半失效了"


def test_status_enum_value_domains_are_pairwise_disjoint():
    """任意两个状态枚举的值域不得相交。

    很多地方依赖这一点：前端 `constants.js` 的色表是**扁平**的一张 map（值→颜色）、
    `TagOption(field, value)` 的唯一键、以及人读日志时「看见『已取消』就知道是集运单」。
    值域一旦相交，这些地方全部同时静默出错。

    历史上「已签收」曾同时是订单状态和集运状态，正是这条约束被违反的那次事故
    （订单标『集运中』、它挂的集运单标『已发出』，同一件事两处对不上）。
    """
    enums = _status_enums()
    clashes = []
    names = sorted(enums)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            both = {e.value for e in enums[a]} & {e.value for e in enums[b]}
            if both:
                clashes.append(f"{a} 与 {b} 共用值 {sorted(both)}")
    assert not clashes, "状态枚举值域相交：\n  " + "\n  ".join(clashes)


def test_every_purchase_status_is_ranked_or_terminal():
    """采购段每个状态，要么在推进序里，要么是终态，**不许两不沾**。

    两不沾的值 `purchase_status_rank` 返回 -1，而 -1 的实际效果是「谁都能盖掉它」——
    这正是「退款被 OCR 抹成待发货、看板金额凭空变大」那次事故的机理。
    明年加「退货中」时，这条会在第一次跑测试时就红，而不是等账本对不上。
    """
    orphans = [
        s.value for s in PurchaseStatus
        if s.value not in PURCHASE_STATUS_RANK and s.value not in PURCHASE_TERMINAL_STATUSES
    ]
    assert not orphans, (
        f"这些采购状态既不在 PURCHASE_STATUS_RANK 也不在 PURCHASE_TERMINAL_STATUSES：{orphans}\n"
        "两不沾 → rank 取 -1 → 会被任何推进态静默盖掉。请显式表态它排在生命周期哪一节，或它是终态。"
    )


def test_rank_and_terminal_do_not_overlap():
    """终态不该同时出现在推进序里——那样 `can_advance_purchase` 的两条分支会互相打架。"""
    both = sorted(set(PURCHASE_STATUS_RANK) & set(PURCHASE_TERMINAL_STATUSES))
    assert not both, f"既在推进序又是终态：{both}"


# --- 看板：排除规则必须属于自己那张表 -----------------------------------------

_LEDGER_MODELS = [Order, ShipmentOrder, MiscExpense]


@pytest.mark.parametrize("model", _LEDGER_MODELS, ids=lambda m: m.__name__)
def test_ledger_exclusions_reference_only_own_columns(model):
    """每条排除规则的列必须长在这张表上。

    比「排除值 ⊆ 两个枚举的并集」强得多：那条允许把集运的值施加到订单列上。
    更要紧的是**引用到别的表**会让 SQLAlchemy 自动把对侧表加进 FROM，
    生成隐式交叉连接 → `SUM` 乘以对侧行数 → 看板金额静默变大（不报错）。
    """
    for col, _ in model.ledger_exclusions():
        assert col.table is model.__table__, (
            f"{model.__name__}.ledger_exclusions() 引用了别的表的列 {col}——"
            "会生成隐式交叉连接，合计金额会被乘以对侧行数")


@pytest.mark.parametrize("model", _LEDGER_MODELS, ids=lambda m: m.__name__)
def test_ledger_exclusion_values_are_in_that_columns_enum(model):
    """排除的值必须真的属于这张表那一段的枚举，否则这条规则是空转的。"""
    enums = _status_enums()
    for col, excluded in model.ledger_exclusions():
        domains = [set(e.value for e in en) for en in enums.values()
                   if excluded <= {e.value for e in en}]
        assert domains, (
            f"{model.__name__} 排除的值 {sorted(excluded)} 不完全属于任何一个状态枚举——"
            "这条规则要么写错了值，要么该值已被删除，现在是空转的")


def test_excluded_statuses_is_exactly_the_union():
    """`EXCLUDED_STATUSES` 是派生量。有人把它改回手写常量、和两段分表对不上时这里红。"""
    union = set()
    for m in _LEDGER_MODELS:
        for _, excluded in m.ledger_exclusions():
            union |= set(excluded)
    assert set(EXCLUDED_STATUSES) == union, (
        f"EXCLUDED_STATUSES={sorted(EXCLUDED_STATUSES)} 与各表实际排除的并集 {sorted(union)} 不一致")


def test_dashboard_does_not_duck_type_the_status_column():
    """看板不得再用 `model.status` / `has_status` 这类鸭子类型取列。

    它们成立的唯一依据是「两张表的状态列恰好同名」——一个巧合被当成了契约。
    """
    import ast

    from app.routers import dashboard

    tree = ast.parse(inspect.getsource(dashboard))
    bad = []
    for node in ast.walk(tree):
        # `model.status` 这样的属性访问
        if (isinstance(node, ast.Attribute) and node.attr == "status"
                and isinstance(node.value, ast.Name)):
            bad.append(f"{node.value.id}.status（第 {node.lineno} 行）")
        # `has_status` 无论作形参、实参还是变量
        if isinstance(node, ast.Name) and node.id == "has_status":
            bad.append(f"has_status（第 {node.lineno} 行）")
        if isinstance(node, ast.arg) and node.arg == "has_status":
            bad.append(f"形参 has_status（第 {node.lineno} 行）")
    # 走 AST 而不是文本包含：解释这段历史的注释与 docstring 里必然会写到旧写法，
    # 文本匹配会被自己的注释绊倒（第一版就是这么假红的）。
    assert not bad, "看板又用鸭子类型取状态列了：\n  " + "\n  ".join(bad)


def test_dashboard_aggregates_exactly_the_three_expense_tables():
    """看板合计的模型集合必须恰好是这三张**支出**表。

    接第四张表时这条会红——那正是需要有人停下来想清楚「它是收入还是支出、符号怎么算」
    的时刻。只实现一个 `ledger_exclusions()` 就被 `_sum` 进去，会把收入当支出加进合计。
    """
    import re

    from app.routers import dashboard

    src = inspect.getsource(dashboard)
    aggregated = set(re.findall(r"_(?:sum|count|by_month_cat)\(session,\s*(\w+)\)", src))
    assert aggregated == {"Order", "ShipmentOrder", "MiscExpense"}, (
        f"看板聚合的表变成了 {sorted(aggregated)}。新增一张表进合计前，"
        "必须先确定它是收入还是支出——total_jpy 现在的语义是**总支出**。")


# --- 命名公理的自持守卫 -------------------------------------------------------

_LEG_REGISTRY = {
    "purchase_status": "PurchaseStatus",     # 采购段：下单 → 国内快递签收
    "shipment_status": "ShipmentStatus",     # 集运段：打包 → 送达
    "import_status": "ImportStatus",         # soroban 自己的导入工作流（不是交易）
}


def test_no_column_is_named_bare_status():
    """任何表都不许有名字恰为 `status` 的列。

    这是整套公理的自持守卫。裸 `status` 之所以危险，不是因为难看，而是它让
    `model.status` 这种鸭子类型**看起来能用**——看板就这么写过，靠的是「两张表的状态列
    恰好同名」这个巧合。加卖出段时巧合会绷断，而绷断的表现是金额悄悄变了，不是报错。
    """
    from sqlmodel import SQLModel

    bad = [f"{name}.status" for name, t in SQLModel.metadata.tables.items() if "status" in t.columns]
    assert not bad, f"出现了裸 status 列：{bad}。请改成 <业务段>_status（见本文件顶部的公理）"


def test_status_columns_are_leg_qualified_and_single_meaning():
    """每个含 status 的列必须登记在段表里；同名列必须对应同一个枚举。

    「同名 ⟺ 同概念」是公理的后半句：`orders.purchase_status` 与
    `orderstaging.purchase_status` 同名，就必须是同一套值——它们本来就是同一件事的
    两处表示。反过来，两套不同的值域绝不能共用一个列名。
    """
    from sqlmodel import SQLModel

    enums = _status_enums()
    unregistered, mismatched = [], []
    for tname, table in SQLModel.metadata.tables.items():
        for col in table.columns:
            if "status" not in col.name:
                continue
            want = _LEG_REGISTRY.get(col.name)
            if want is None:
                unregistered.append(f"{tname}.{col.name}")
                continue
            # 列上没有直接的枚举引用，靠「这张表的模型默认值属于该枚举」间接核对
            domain = {e.value for e in enums[want]}
            default = getattr(col.default, "arg", None)
            if isinstance(default, str) and default not in domain:
                mismatched.append(f"{tname}.{col.name} 默认值 {default!r} 不属于 {want}")
    assert not unregistered, (
        f"这些状态列没在段登记表里：{unregistered}\n"
        "新增状态列时必须显式表态它属于哪一业务段、用哪个枚举（改 _LEG_REGISTRY）。")
    assert not mismatched, "同名列指向了不同的枚举：\n  " + "\n  ".join(mismatched)


def test_leg_registry_has_not_gone_stale():
    """登记表的元断言：登记了却没有任何表在用的名字，说明表已改而名单没跟上。"""
    from sqlmodel import SQLModel

    used = {c.name for t in SQLModel.metadata.tables.values() for c in t.columns if "status" in c.name}
    stale = set(_LEG_REGISTRY) - used
    assert not stale, f"_LEG_REGISTRY 里这些名字已经没有任何列在用了：{sorted(stale)}"


def test_fulfillment_status_is_read_only():
    """`fulfillment_status` 是跨段合流的**派生**值，只能出现在读模型上。

    给它一个可写入口，等于让人把「有时属于集运单」的值写进订单自己的列——
    正是历史上「订单标集运中、它挂的集运单标已发出，两处对不上」那起事故。
    """
    from app import schemas

    writable = [
        name for name in dir(schemas)
        if name.endswith(("Create", "Update"))
        and hasattr(getattr(schemas, name), "model_fields")
        and "fulfillment_status" in getattr(schemas, name).model_fields
    ]
    assert not writable, f"这些写模型上出现了派生字段 fulfillment_status：{writable}"


# --- 写入口的白名单闸门 ------------------------------------------------------
#
# 上面几条守的是「结构」——列叫什么名、属于哪一段。这一节守的是同一套公理在
# **写入口**上的执行面：每个业务段的 POST 都必须由该段自己的枚举白名单把关。
#
# 为什么不能只断言 422：写模型是 extra="forbid"，任何写错的键同样 422。于是一次
# 列改名就能让「非法状态被拒」的测试拿着 extra_forbidden 继续绿，而它本该守的校验器
# 早已无人过问。集运段就这么失守过一轮（tests/test_shipment.py 那条）。断言拒绝的
# **理由**（detail[0].type == value_error 且 loc 落在该字段上），才让它改名即红。

_ILLEGAL_STATUS_CASES = [
    ("/api/orders", {"date": "2026-03-01"}, "purchase_status"),
    ("/api/staging", {"order_no": "S-BADST"}, "purchase_status"),
    ("/api/shipment", {"date": "2026-06-01"}, "shipment_status"),
    ("/api/staging", {"order_no": "S-BADIMP"}, "import_status"),
]


@pytest.mark.parametrize("endpoint,base,field", _ILLEGAL_STATUS_CASES)
def test_illegal_status_hits_the_whitelist_validator(client, endpoint, base, field):
    r = client.post(endpoint, json={**base, field: "无此状态"})
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["type"] == "value_error" and err["loc"][-1] == field, (
        f"{endpoint} 的 {field} 白名单校验器可能已丢失，422 却来自 {err['type']}：{err}")


def test_every_writable_leg_status_has_a_whitelist_case():
    """段一旦新增，上面的参数表必须跟着登记——否则新段的写入口没有闸门也没人发现。

    ⚠️ 这里原先把 `import_status` 显式排除，理由写的是「它没有独立的 POST 写入口
    （只能经 /ignore、/import 推进）」——**那句话是假的**：`StagingCreate` 就是那个写入口，
    而它当时对这个字段一点校验都没有。豁免理由不成立的豁免，比遗漏更难发现：
    元断言 `test_server_owned_names_are_real_columns` 只检查名字还是不是真列，
    检查不到「豁免的理由还成不成立」。现已补上校验，例外随之取消。
    """
    covered = {field for _, _, field in _ILLEGAL_STATUS_CASES}
    assert covered == set(_LEG_REGISTRY), (
        f"业务段与白名单用例对不上：段={sorted(_LEG_REGISTRY)}，已覆盖={sorted(covered)}")


def test_a_new_staging_row_cannot_claim_it_is_already_imported(client):
    """`POST /api/staging {"import_status": "已导入"}` 必须被拒。

    「已导入」是合法枚举值，所以光有白名单还不够——它建出来的是一条
    `import_status=已导入` 而 `imported_order_id=NULL` 的行：
      · 按状态筛选时它算已导入 ⇒ 用户以为这笔账已经记了；
      · `POST /{id}/import` 判的是 `imported_order_id`，照样能再导一次
        ⇒ **同一笔货进账本两遍**。
    `update_staging` 用十行注释堵死的正是这个洞，但只堵在 PATCH 上。
    """
    r = client.post("/api/staging", json={"order_no": "S-IMPCLAIM", "import_status": "已导入"})
    assert r.status_code == 422, f"新建就能自称已导入：{r.status_code} {r.text[:200]}"

    # 初始值取自枚举本身，不在测试里再抄一份字面量（抄了就会漂）。
    from app.models import ImportStatus
    initial = ImportStatus.pending.value

    # **反面一**：默认值（不传）当然要放行。
    ok = client.post("/api/staging", json={"order_no": "S-IMPOK"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["import_status"] == initial

    # **反面二**：显式传初始值也要放行——闸是「只能是初始值」，不是「不许出现这个键」。
    ok2 = client.post("/api/staging",
                      json={"order_no": "S-IMPOK2", "import_status": initial})
    assert ok2.status_code == 200, ok2.text


def test_a_new_staging_row_cannot_smuggle_an_oversized_status(client):
    """超长值同样要在门口挡掉：这一列是 VARCHAR(32)，SQLite 静默收下 100 个字符，
    MySQL 抛 1406 DataError —— 而 main.py 没有 DataError 的 handler ⇒ 裸 500。
    同一份数据库导出，两个后端两种结局。
    """
    r = client.post("/api/staging", json={"order_no": "S-IMPLONG", "import_status": "x" * 100})
    assert r.status_code == 422, r.text
