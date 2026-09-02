"""并发/交错写：直接在 Session 层复现「读—判断—写」竞态，验证 DB 层守卫真的守住了。

不用线程（SQLite 单写者会变成锁竞争而非逻辑竞态），而是开两个 Session 手工交错，
这正是 guarded_bump / 原子门闸 想防的那类交错。
"""
import datetime as dt

import pytest
from sqlmodel import Session, select

from app.database import get_engine
from app.models import Order, OrderStaging, ShipmentOrder, ImportStatus
from app.routers.common import guarded_bump


def test_guarded_bump_only_one_winner(client):
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "cc1"}).json()
    with Session(get_engine()) as a, Session(get_engine()) as b:
        assert guarded_bump(a, Order, o["id"], o["version"]) is True
        a.commit()
        # b 拿着同一个旧 version 再来一次 → 必须失败
        assert guarded_bump(b, Order, o["id"], o["version"]) is False
        b.rollback()


def test_guarded_bump_refuses_soft_deleted(client):
    """软删过的行不许再被 bump——**判据要用软删之后的 version**。

    原先传的是删除**之前**那个 version，而 `soft_delete` 自己会把 version +1
    （删除也是一次写）。于是这条测试是被「版本对不上」挡下的，
    `guarded_bump` 里那句 `is_delete.is_(False)` **一次都没参与判定**：
    把它整行去掉，1063 条测试没有一条会红（变异测试实测）。

    这道条件是第二层防线：路由层会先 `session.get` + `if order.is_delete → 404`，
    但那是在事务真正开始写之前读的。并发下（A 读完之后、bump 之前 B 删了这行）
    只有 UPDATE 语句里带着的这个条件挡得住，因为它和自增 version 是同一条语句。
    所以它必须自己被测到，而不是靠上一层遮住。
    """
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "cc2"}).json()
    client.delete(f"/api/orders/{o['id']}")
    with Session(get_engine()) as s:
        row = s.get(Order, o["id"])
        assert row is not None and row.is_delete, "软删没生效，这条测试的前提不成立"
        # 用**当前**版本号：这样唯一能挡住它的就只剩 is_delete 那一条
        assert guarded_bump(s, Order, o["id"], row.version) is False, \
            "软删过的行还能被 bump——UPDATE 语句里少了 is_delete 守卫"
    # 反面：没删的行、版本对得上，就该成功。否则把条件写成恒 False 也能让上面绿。
    live = client.post("/api/orders", json={"date": "2027-04-01", "title": "cc2-live"}).json()
    with Session(get_engine()) as s:
        assert guarded_bump(s, Order, live["id"], live["version"]) is True
        s.rollback()


def test_import_gate_claims_only_once(client):
    """原子门闸：imported_order_id IS NULL 只让一次导入成功。"""
    from sqlalchemy import update as sa_update

    from app.models import utcnow
    s = client.post("/api/staging", json={"order_no": "CC-GATE"}).json()
    with Session(get_engine()) as a, Session(get_engine()) as b:
        def claim(sess, fake_order_id):
            return sess.execute(
                sa_update(OrderStaging)
                .where(OrderStaging.id == s["id"], OrderStaging.imported_order_id.is_(None))
                .values(import_status=ImportStatus.imported.value, imported_order_id=fake_order_id,
                        version=OrderStaging.version + 1, updated_at=utcnow())
            ).rowcount

        o = client.post("/api/orders", json={"date": "2027-04-01"}).json()
        assert claim(a, o["id"]) == 1
        a.commit()
        assert claim(b, o["id"]) == 0          # 已被认领
        b.rollback()


def test_two_people_importing_the_same_row_end_up_with_one_order(client, monkeypatch):
    """两个人同时点「导入」，账本里只能多出**一张**单。

    上面那条 `test_import_gate_claims_only_once` 测的是**手抄的一份 SQL**，
    不是 `import_staging` 端点——把生产代码那条 `imported_order_id.is_(None)`
    整个删掉，全套 1394 条一条都不红（2026-09-02 变异实测）。

    **为什么这条闸不能只靠前面那个快速检查**：端点开头确实有
    `if row.imported_order_id is not None: 409`，但两个并发请求会**双双**在
    任何一方提交之前读到 None ⇒ 双双建单 ⇒ 全靠这条 UPDATE 的 WHERE 分胜负。

    **为什么唯一索引兜不住**：有 `order_no` 的行撞唯一约束会被全局 409 接住，
    而 OCR 认不出单号的行 `order_no` 是 NULL ——部分唯一索引对 NULL 不生效，
    两张单会一起落进账本，钱算两遍。所以这条用例**故意不给 order_no**，
    测的正是没有第二道保险的那一类行。

    注入点是 `Order.sync_from_items`——它在建单之后、`session.flush()` **之前**
    无条件调用一次。**必须在 flush 之前**：flush 会拿走 SQLite 的写锁，
    这之后另一个连接连写都写不进去（实测直接 SQLITE_BUSY → 503），
    而那不是真实时序。真实时序是「后到的那个请求阻塞在自己的 flush 上、
    等前一个提交完再继续」，也就是**认领发生在本请求取得写锁之前**。
    下面第一条断言先钉住「注入点真的被调用过」，免得探测方式过期后这条测试
    退化成一句什么都不证明的话。
    """
    from sqlalchemy import update as sa_update

    from app.models import utcnow as real_utcnow

    s = client.post("/api/staging", json={"title": "并发导入", "price_cny": "300.00"}).json()
    assert s.get("order_no") is None, "这条用例要的就是没有单号的行"
    other_order = client.post("/api/orders", json={"date": "2027-04-01"}).json()

    stolen = []
    original_sync = Order.sync_from_items

    def steal_then_sync(self, **kw):
        if not stolen:
            stolen.append(True)
            with Session(get_engine()) as thief:
                n = thief.execute(
                    sa_update(OrderStaging)
                    .where(OrderStaging.id == s["id"],
                           OrderStaging.imported_order_id.is_(None))
                    .values(import_status=ImportStatus.imported.value,
                            imported_order_id=other_order["id"],
                            version=OrderStaging.version + 1, updated_at=real_utcnow())
                ).rowcount
                thief.commit()
            assert n == 1, "抢先认领没成功，这条用例的前提就不成立"
        return original_sync(self, **kw)

    monkeypatch.setattr(Order, "sync_from_items", steal_then_sync)
    r = client.post(f"/api/staging/{s['id']}/import")

    assert stolen, "注入点一次都没被调用 —— utcnow 的位置变了，这条测试已经不测那个竞态了"
    assert r.status_code == 409, (
        f"输给了并发的另一次导入，却照样返回 {r.status_code}——"
        f"这条暂存行会在账本里变成两张单，钱算两遍：{r.text[:200]}")

    # **两半都钉**：不只要报 409，输的那一方刚建的订单必须被回滚掉。
    with Session(get_engine()) as chk:
        made = chk.exec(select(Order).where(Order.title == "并发导入")).all()
    assert made == [], f"409 了，但输的那一方建的单留在库里：{[o.id for o in made]}"


def test_attach_gate_blocks_double_attach(client):
    """挂靠守卫：shipment_order_id IS NULL 让并发的第二次挂靠 rowcount=0。"""
    from sqlalchemy import update as sa_update

    from app.models import utcnow
    s1 = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S1"}).json()
    s2 = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S2"}).json()
    o = client.post("/api/orders", json={"date": "2027-04-01"}).json()

    def attach(sess, ship_id):
        return sess.execute(
            sa_update(Order)
            .where(Order.id == o["id"], Order.shipment_order_id.is_(None), Order.is_delete.is_(False))
            .values(shipment_order_id=ship_id, version=Order.version + 1, updated_at=utcnow())
        ).rowcount

    with Session(get_engine()) as a, Session(get_engine()) as b:
        assert attach(a, s1["id"]) == 1
        a.commit()
        assert attach(b, s2["id"]) == 0
        b.rollback()
    assert client.get(f"/api/orders/{o['id']}").json()["shipment_order_id"] == s1["id"]


def test_attach_after_shipment_deleted_is_rejected(client):
    """EXISTS 守卫：集运单在极小窗口内被软删 → 挂靠必须失败，不留悬空外键。"""
    s = client.post("/api/shipment", json={"date": "2027-04-01", "shipment_no": "CC-S3"}).json()
    o = client.post("/api/orders", json={"date": "2027-04-01"}).json()
    client.delete(f"/api/shipment/{s['id']}")
    r = client.post(f"/api/shipment/{s['id']}/order/{o['id']}")
    assert r.status_code == 404
    assert client.get(f"/api/orders/{o['id']}").json()["shipment_order_id"] is None


def test_stale_patch_after_someone_else_wrote_is_409(client):
    """两个页面各持一份表单：先提交的赢，后提交的必须 409 而不是覆盖。"""
    o = client.post("/api/orders", json={"date": "2027-04-01", "title": "初始"}).json()
    tab_a = dict(o)
    tab_b = dict(o)
    assert client.patch(f"/api/orders/{o['id']}",
                        json={"version": tab_a["version"], "title": "A 改的"}).status_code == 200
    r = client.patch(f"/api/orders/{o['id']}", json={"version": tab_b["version"], "title": "B 改的"})
    assert r.status_code == 409
    assert client.get(f"/api/orders/{o['id']}").json()["title"] == "A 改的"


def test_staging_write_through_bumps_both_versions(client):
    """暂存写穿账本：两边 version 都要推进，否则另一页拿旧版本能悄悄覆盖。"""
    s = client.post("/api/staging", json={"order_no": "CC-WT", "title": "a"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    row = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
               if x["id"] == s["id"])
    client.patch(f"/api/staging/{s['id']}", json={"version": row["version"], "title": "b"})
    assert client.get(f"/api/orders/{o['id']}").json()["version"] > o["version"]
    row2 = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                if x["id"] == s["id"])
    assert row2["version"] > row["version"]


def test_order_edit_bumps_mirrored_staging_version(client):
    """账本改动镜像回暂存时也必须推进暂存 version（否则暂存页旧表单不会 409）。"""
    s = client.post("/api/staging", json={"order_no": "CC-MIR", "title": "a"}).json()
    o = client.post(f"/api/staging/{s['id']}/import").json()
    before = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                  if x["id"] == s["id"])["version"]
    client.patch(f"/api/orders/{o['id']}", json={"version": o["version"], "title": "改"})
    after = next(x for x in client.get("/api/staging", params={"limit": 500}).json()["items"]
                 if x["id"] == s["id"])["version"]
    assert after > before


# --- 跨表写的锁序必须全仓一致 -------------------------------------------------

def test_multi_table_writers_take_locks_in_one_order():
    """同时写 orders 与 orderstaging 的函数，必须**先 orders 后 orderstaging**。

    反向就是 AB-BA 锁环：同一对「暂存行 ↔ 已导入订单」被两条路径并发写时，
    MySQL/InnoDB 报 1213 死锁回滚一方，而 main.py 只挂了 IntegrityError / ValueError
    两个 handler，OperationalError(1213) 会直接逃成裸 500。

    这条只能静态查：**SQLite 是单写者串行，死锁在本地测试里永远复现不出来**，
    而项目支持双引擎。上一轮的教训正是「只在 MySQL 上炸」的问题 SQLite 测不到。
    """
    import ast
    from pathlib import Path

    _ORDER, _STAGING = "Order", "OrderStaging"

    def _model_of(call: ast.Call):
        """取 guarded_bump 的 model 实参名。位置参数与关键字参数都要认。

        只看 `node.args[1]` 是不够的——`guarded_bump(session, model=Order, ...)` 这种
        等价写法会让整条守卫**静默失效**，而它守的是一条本地永远测不出来的 MySQL 缺陷
        （SQLite 单写者串行，死锁复现不了）。守卫被绕过 = 那条缺陷重新裸奔。
        """
        if len(call.args) >= 2:
            return getattr(call.args[1], "id", _UNKNOWN)
        for kw in call.keywords:
            if kw.arg == "model":
                return getattr(kw.value, "id", _UNKNOWN)
        return _UNKNOWN

    _UNKNOWN = "<无法静态判定>"
    bad, unknown = [], []
    for path in sorted((Path(__file__).resolve().parents[1] / "app" / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            seq = []
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "guarded_bump"):
                    continue
                model = _model_of(node)
                if model == _UNKNOWN:
                    unknown.append(f"{path.name}:{node.lineno} {fn.name}")
                elif model in (_ORDER, _STAGING):
                    seq.append((node.lineno, model))
            seq.sort()
            models = [m for _, m in seq]
            if _ORDER in models and _STAGING in models and models.index(_STAGING) < models.index(_ORDER):
                bad.append(f"{path.name}::{fn.name} 先锁 {_STAGING} 后锁 {_ORDER}（行 {[l for l, _ in seq]}）")
    assert not bad, ("跨表写的锁序不一致，MySQL 上会死锁：\n  " + "\n  ".join(bad)
                     + "\n统一成 orders → orderstaging（orders 是共享字段的唯一真源）。")
    # 静默跳过判定不了的调用，等于给绕过留后门：宁可在这里红，逼作者把模型写成字面量。
    assert not unknown, ("这些 guarded_bump 调用的 model 参数无法静态判定，锁序守卫覆盖不到：\n  "
                         + "\n  ".join(unknown) + "\n请直接把模型类名写成字面量实参。")


# --- MySQL 并发写冲突（甲）------------------------------------------------------

@pytest.mark.parametrize("errno,as_409", [
    (1213, True),      # ER_LOCK_DEADLOCK：服务端已回滚一方，重试即可
    (1205, True),      # ER_LOCK_WAIT_TIMEOUT：等锁超时，同上
    (2013, False),     # 连接断开——报成「数据已变，请刷新重试」会让人一直刷一个连不上的库
    (1044, False),     # 权限不足
    (None, False),     # 没有 errno 的 OperationalError
])
def test_only_retryable_mysql_errors_become_409(errno, as_409):
    """死锁转 409，其余 OperationalError 原样抛出去走 500。

    OperationalError 是个大杂烩：「连接断了」「库不存在」「权限不足」也走它。
    整类转 409 的话，前端会对着一个根本连不上的库反复提示「数据已变，请重试」——
    比裸 500 更误导。所以按 errno 精确挑。
    """
    import asyncio

    from sqlalchemy.exc import OperationalError

    from app.main import _deadlock_handler

    orig = Exception(errno, "boom") if errno is not None else Exception()
    exc = OperationalError("SELECT 1", {}, orig)

    class _Req:
        method = "POST"

        class url:
            path = "/api/orders"

    if as_409:
        resp = asyncio.run(_deadlock_handler(_Req(), exc))
        assert resp.status_code == 409
        assert "重试" in resp.body.decode()
    else:
        with pytest.raises(OperationalError):
            asyncio.run(_deadlock_handler(_Req(), exc))
