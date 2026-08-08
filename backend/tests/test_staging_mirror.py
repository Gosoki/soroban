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
        "platform_account": f"探针账号{n}",
        "platform": "闲鱼",
        "express_no": f"SFPROBE{n:06d}",
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
