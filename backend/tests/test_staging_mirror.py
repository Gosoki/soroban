"""暂存行与账本订单之间那份「共享字段」清单的守卫。

`_SHARED_TO_ORDER` 描述的是：暂存页上的哪些列，在导入之后其真相已经转移到账本订单上。
围绕它有三处代码必须同步：
  1. `_SHARED_TO_ORDER` 本身（映射表）
  2. `staging._overlay`      —— 读方向：暂存页显示账本的实时值
  3. `common.mirror_to_staging` —— 写方向：账本改了要写回暂存行

原先 2、3 都是手抄的字段清单，已经漂了一项（`_overlay` 漏 `platform`）。当时没造成
可见错误——因为写方向把值落进了暂存行，读出来正好对——但那是**巧合掩盖了发散**。
现在两处都从映射表派生，这里用行为级断言钉住：源码怎么写不管，行为必须对。

行为级而不是 grep 源码：上一轮审计栽过一次「测试只 grep 字符串，于是 bug 活着测试全绿」。
"""
import datetime as dt
import itertools
from decimal import Decimal, InvalidOperation

import pytest
import sqlalchemy as sa
from sqlmodel import select

from app.models import Order, OrderStaging
from app.routers.staging import _SHARED_TO_ORDER

# 每个**账本侧**字段改成什么值来验证它传导过去了。
# 值必须与建行时的初值不同，否则「没传导」和「传导了」看起来一样。
# 标识类列带唯一约束，固定值会在第二个参数化用例上撞 409（报错还看不出是造数撞了），
# 所以整张表按调用序号取值。
def _probe(n: int) -> dict:
    return {
        "date": "2026-09-09",
        "order_no": f"SHARED-PROBE-{n}",
        "title": f"共享字段探针标题{n}",
        # 带 query string 是刻意的：它同时压了「长值不被截断」与「特殊字符不被吃掉」，
        # 而 URL 恰好是这张表里唯一会长过 64 字符的共享列。
        "url": f"https://item.taobao.com/item.htm?id=probe{n:08d}",
        "platform_account": f"探针账号{n}",
        "platform": "闲鱼",
        "express_no": f"SFPROBE{n:06d}",
        "express_company": f"探针快递{n}",
        "postage_cny": "7.77",
        "fx_rate": "19.99",
        "purchase_status": "已签收",
    }


def test_probe_covers_every_shared_field():
    """元断言：往 `_SHARED_TO_ORDER` 加字段却不加探针值，这里先红。

    没有这条，新增的共享字段会**静默地不被下面那条测试覆盖**——守卫看起来还在，
    实际保护范围悄悄缩小了。这正是「豁免名单型」测试最常见的腐烂方式。
    """
    probe = _probe(0)
    missing = set(_SHARED_TO_ORDER.values()) - set(probe)
    extra = set(probe) - set(_SHARED_TO_ORDER.values())
    assert not missing, f"新增了共享字段但没加探针值：{sorted(missing)}"
    assert not extra, f"探针值对应的字段已不在共享清单里：{sorted(extra)}"


_seq = itertools.count(1)


@pytest.fixture()
def imported(client):
    """建一条暂存行并导入，返回 (暂存 id, 账本订单 id)。

    order_no 逐次唯一：conftest 的库在整个会话里共享，固定单号会在第二个用例撞唯一索引，
    而 409 的报错信息（「数据完整性冲突」）完全看不出是测试造数撞了。
    """
    r = client.post("/api/staging", json={
        "order_date": "2026-06-01", "order_no": f"MIRROR-{next(_seq)}", "platform": "淘宝",
        "title": "原始标题", "platform_account": "原账号",
        "items": [{"name": "a", "quantity": 1, "unit_price_cny": "5"}]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert client.post(f"/api/staging/{sid}/import").status_code == 200
    row = next(x for x in client.get("/api/staging", params={"limit": 200}).json()["items"]
               if x["id"] == sid)
    return sid, row["imported_order_id"]


def _patch_order(client, oid, field, value):
    cur = client.get("/api/orders", params={"limit": 200}).json()["items"]
    ver = next(x for x in cur if x["id"] == oid)["version"]
    r = client.patch(f"/api/orders/{oid}", json={"version": ver, field: value})
    assert r.status_code == 200, f"改 {field} 失败：{r.status_code} {r.text}"


def _same(got, want) -> bool:
    """探针值都写成字符串，这里按值本身比。

    金额/汇率列在 API 上是带标度的字符串（`19.9900`），直接 `==` 会假红——
    能解析成数就按数比，否则按字符串比。
    """
    if isinstance(got, dt.datetime):
        got = got.date()
    try:
        return Decimal(str(got)) == Decimal(str(want))
    except InvalidOperation:
        return str(got) == str(want)


def _coerce(col, want: str):
    """把字符串探针值转成该列的 Python 类型，供**直接写库**用。

    走列类型而不是 `col.type.python_type`：本项目的键列用了自定义类型（BinStr/UtcDateTime），
    它们没实现 python_type，一调就 NotImplementedError。
    """
    if isinstance(col.type, sa.Date):
        return dt.date.fromisoformat(want)
    if isinstance(col.type, sa.Numeric):
        return Decimal(want)
    return want


@pytest.mark.parametrize("staging_field,order_field", sorted(_SHARED_TO_ORDER.items()))
def test_overlay_shows_the_ledger_even_when_the_row_is_stale(
    client, session, imported, staging_field, order_field
):
    """读方向（`_overlay`）：暂存页显示的必须是**账本**的值，而不是暂存行里存着的那份。

    ⚠️ 这里刻意**绕过 API 直接改库**。走 PATCH 的话 `mirror_to_staging` 会把新值写进
    暂存行，于是 `_overlay` 即使漏掉这个字段，从行里读出来也正好是对的——
    「巧合掩盖发散」，测试全绿而覆盖为零。`_overlay` 漏 `platform` 那次就是这么藏住的。
    造出「行陈旧、账本新」这个状态，才真正测到 `_overlay`。
    """
    sid, oid = imported
    probe = _probe(next(_seq))
    order = session.get(Order, oid)
    col = Order.__table__.columns[order_field]
    setattr(order, order_field, _coerce(col, probe[order_field]))
    session.add(order)
    session.commit()

    shown = next(x for x in client.get("/api/staging", params={"limit": 200}).json()["items"]
                 if x["id"] == sid)[staging_field]
    row = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    assert not _same(getattr(row, staging_field), probe[order_field]), (
        f"前提没造出来：暂存行的 {staging_field} 已经等于新值了，这条测不到 _overlay")
    assert _same(shown, probe[order_field]), (
        f"_overlay 漏了 {staging_field}：暂存页显示 {shown!r}，账本已是 {probe[order_field]!r}")


@pytest.mark.parametrize("staging_field,order_field", sorted(_SHARED_TO_ORDER.items()))
def test_mirror_writes_the_field_back_to_the_row(
    client, session, imported, staging_field, order_field
):
    """写方向（`mirror_to_staging`）：走 API 改账本后，暂存**行**里也得是新值。

    只靠 `_overlay` 显示对是不够的：任何绕过 `_overlay` 直接读行的路径
    （删单复位、清账本后再导入）会拿到陈旧快照，把订单页上做的编辑丢掉。
    """
    sid, oid = imported
    probe = _probe(next(_seq))
    _patch_order(client, oid, order_field, probe[order_field])
    session.expire_all()
    row = session.exec(select(OrderStaging).where(OrderStaging.id == sid)).one()
    got = getattr(row, staging_field)
    assert _same(got, probe[order_field]), (
        f"mirror_to_staging 漏了 {staging_field}：暂存行是 {got!r}，账本已是 {probe[order_field]!r}")


def test_imported_row_status_follows_the_ledger(client, session, imported):
    """爬虫「不把用户手动推进的状态抹回去」的唯一支点。

    爬虫拉 `/api/staging` 读当前状态、再决定要不要推进（soroban_client 的 `_can_advance`）。
    如果这里读到的是**暂存行自己的**陈旧状态而不是账本的实时状态，爬虫每轮都会认为
    「用户还停在待收货」，于是把手动标好的「已签收」一路抹回去——淘宝要 7–10 天才自动
    确认收货，这个窗口里每轮抹一次。这条链路此前没有任何测试。
    """
    sid, oid = imported
    _patch_order(client, oid, "purchase_status", "已签收")
    shown = next(x for x in client.get("/api/staging", params={"limit": 200}).json()["items"]
                 if x["id"] == sid)
    assert shown["purchase_status"] == "已签收"

    # 反向：账本回到「待收货」，暂存页也必须跟着回落（不是单向锁死）
    _patch_order(client, oid, "purchase_status", "待收货")
    shown = next(x for x in client.get("/api/staging", params={"limit": 200}).json()["items"]
                 if x["id"] == sid)
    assert shown["purchase_status"] == "待收货"


def test_shared_map_names_real_columns():
    """映射表两侧都必须是真列名。写错一侧不会报错——`getattr` 拿不到就抛 AttributeError，
    但那要等到有人真的导入一行才发现；这里在导入前就红。"""
    st_cols = set(OrderStaging.__table__.columns.keys())
    od_cols = set(Order.__table__.columns.keys())
    bad_l = set(_SHARED_TO_ORDER) - st_cols
    bad_r = set(_SHARED_TO_ORDER.values()) - od_cols
    assert not bad_l, f"_SHARED_TO_ORDER 的键不是 orderstaging 的列：{sorted(bad_l)}"
    assert not bad_r, f"_SHARED_TO_ORDER 的值不是 orders 的列：{sorted(bad_r)}"


@pytest.mark.parametrize("staging_field,order_field", sorted(_SHARED_TO_ORDER.items()))
def test_import_carries_every_shared_field_into_the_ledger(client, staging_field, order_field):
    """**导入**时每个共享字段都要搬进账本——`import_staging` 是手写的第四份清单。

    `_overlay`（读时覆盖）与 `mirror_to_staging`（写回镜像）都从 `_SHARED_TO_ORDER` 派生，
    加一列自动跟上；而 `import_staging` 里是一句一句手写的 `Order(...)`。
    漏一列的表现最安静：暂存页上明明有值，导入之后账本那一格是空的，
    而**没有任何一处会报错**——`express_company` 这一列就是这么长期缺席的
    （它当时连暂存表都没有，所以连「漏了」都看不出来）。
    """
    n = next(_seq)
    probe = _probe(n)
    body = {"order_no": f"IMPORT-COVER-{n}", "order_date": "2026-06-02",
            "items": [{"name": "a", "quantity": 1, "unit_price_cny": "5"}]}
    # 探针值按**暂存侧**字段名下发；order_date/date 这一对名字不同，单独映射。
    body[staging_field] = probe[order_field] if staging_field != "order_date" else "2026-09-09"
    if staging_field == "order_no":
        body["order_no"] = probe["order_no"]
    r = client.post("/api/staging", json=body)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert client.post(f"/api/staging/{sid}/import").status_code == 200

    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == sid)
    order = client.get(f"/api/orders/{row['imported_order_id']}").json()
    assert _same(order[order_field], body[staging_field]), \
        f"导入时丢了 {staging_field} → {order_field}：账本是 {order[order_field]!r}"


# 账本有、暂存**刻意**没有的列。每一条都要写清为什么，加新列时这张表会逼人做一次决定。
_LEDGER_ONLY = {
    # 账本记账机制，暂存行不参与记账
    "date": "暂存用 order_date（下单日期），导入时才落成账本的记账日 date",
    "jpy_auto": "派生日元，暂存不算钱",
    "jpy_override": "人工覆盖金额，是账本侧的动作",
    "jpy_settled": "同上",
    "override_note": "同上",
    "note": "备注是导入后才写的",
    "created_at": "暂存用 scraped_at",
    "created_via": "暂存行天然就是「抓来的」，这一列在账本侧才有区分意义",
    "payer_id": "谁付的钱，导入后才定",
    "is_delete": "暂存用 import_status=已忽略 表达「不要了」，不做软删",
    # 关联
    "shipment_order_id": "集运挂靠是账本侧的事，暂存行不挂集运单",
    # ⬇️ 这一条**不是**刻意的，是还没做的：账本上是用户可填的业务列，
    #    暂存侧没有对应列，所以插件即便哪天能抓到也没地方放。加它要一条迁移。
    #    （`url` 原本也在这里，2026-08-11 补上了：淘宝的列表接口里就有 itemUrl，
    #      零额外请求，见迁移 c3d4e5f6a7b8。category 至今仍没有生产者。）
    "category": "【待定】账本可填的分类，暂存无对应列；今天没有生产者",
}


def test_ledger_only_columns_are_all_accounted_for():
    """账本比暂存多出来的每一列，都必须在 `_LEDGER_ONLY` 里写明理由。

    这两张表**不是**两份相同的表（暂存有导入工作流列、账本有记账与集运列），
    但「哪些列刻意不对齐」必须是写下来的决定，而不是没人注意到的差集。
    `express_company` 就是这么缺了很久的：插件从同一个响应里解析出快递公司，
    而暂存表没有这一列 ⇒ 跨表那一步静默丢掉，链路上一个字节的报错都没有。

    加了新列忘了想这件事 → 这里先红，那正是要一次决定的时刻。
    """
    ledger = {c.name for c in Order.__table__.columns} - {"id"}
    staging = {c.name for c in OrderStaging.__table__.columns}
    unexplained = ledger - staging - set(_LEDGER_ONLY)
    assert not unexplained, (
        f"账本新增了列但暂存没有、也没说明为什么：{sorted(unexplained)}。"
        "要么给暂存也加上（并进 _SHARED_TO_ORDER），要么在 _LEDGER_ONLY 里写清理由。")
    stale = set(_LEDGER_ONLY) - ledger
    assert not stale, f"_LEDGER_ONLY 里列着账本已经没有的列：{sorted(stale)}"
    # 两边**同名**的业务列必须进共享清单，否则导入/镜像会各走各的。
    # 用「⊆」而不是「==」：共享清单里还有一对名字不同的（暂存 order_date → 账本 date），
    # 它当然不会出现在同名交集里。
    both = ledger & staging
    workflow = {"version", "updated_at", "price_cny"}   # 乐观锁/时间戳/派生价各自维护
    assert (both - workflow) <= set(_SHARED_TO_ORDER), (
        "两张表都有、却没进共享清单的业务列："
        f"{sorted(both - workflow - set(_SHARED_TO_ORDER))}")
    # 反向：共享清单里的每个键都得真是暂存表的列、值都得真是账本的列
    assert set(_SHARED_TO_ORDER) <= staging, \
        f"共享清单里有暂存表没有的列：{sorted(set(_SHARED_TO_ORDER) - staging)}"
    assert set(_SHARED_TO_ORDER.values()) <= ledger | {"date"}, \
        f"共享清单里有账本没有的列：{sorted(set(_SHARED_TO_ORDER.values()) - ledger)}"


def test_renaming_an_order_onto_a_staging_only_number_says_where_the_clash_is(client):
    """两张表的唯一性契约不一样，而镜像会把新单号推进暂存行：

      · 账本：`(order_no, COALESCE(platform,''))` —— 注释明写「不同来源下允许同号」；
      · 暂存：`order_no` **单列**部分唯一索引，不分平台、不分是否已导入。

    于是**账本这边合法的改名，会被暂存表的索引否决**。原先撞上去得到的是
    全局 handler 的一句「数据完整性冲突（唯一约束/外键/必填）」，
    而前端把 409 当乐观锁冲突整表重拉 —— 编辑消失，提示里一个字都没提暂存表。
    """
    import uuid

    a = "MIRA" + uuid.uuid4().hex[:8].upper()
    b = "MIRB" + uuid.uuid4().hex[:8].upper()

    s1 = client.post("/api/staging", json={"order_no": a, "platform": "淘宝"}).json()
    client.post(f"/api/staging/{s1['id']}/import")
    oid = client.get(f"/api/staging?q={a}").json()["items"][0]["imported_order_id"]
    # 另一条**未导入**的暂存行占着目标号（用户很可能根本没在看它）
    client.post("/api/staging", json={"order_no": b, "platform": "闲鱼"})

    o = client.get(f"/api/orders/{oid}").json()
    r = client.patch(f"/api/orders/{oid}", json={"version": o["version"], "order_no": b})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "暂存" in detail and b in detail, f"没说清撞在哪：{detail}"
    # 账本没被改动
    assert client.get(f"/api/orders/{oid}").json()["order_no"] == a

    # **反面**：目标号没人占时，改名必须照常成功（别把闸写成「不许改单号」）
    free = "MIRC" + uuid.uuid4().hex[:8].upper()
    o = client.get(f"/api/orders/{oid}").json()
    ok = client.patch(f"/api/orders/{oid}", json={"version": o["version"], "order_no": free})
    assert ok.status_code == 200, ok.text
    assert client.get(f"/api/orders/{oid}").json()["order_no"] == free
