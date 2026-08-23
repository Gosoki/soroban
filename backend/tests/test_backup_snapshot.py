"""备份/迁移的拷贝要在**一个读快照**里完成，不能边拷边被写。

不这么做的话，源库那 13 张表是 13 次各自独立的 SELECT——13 个不同的时间点。
期间有人写入，拷出来的就是撕裂的：**子行有了、父行没有**，恢复时外键直接炸。

原先挡这件事靠只读屏障，但屏障是**进程内**的（`threading.Lock`）。
`python -m tools.backup_db` 与 README 里那条 cron 都跑在另一个进程里——
也就是说，唯一会天天自动跑的那条备份路径，恰恰是屏障保护不到的那条。
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
from sqlmodel import Session, select

from app.database import build_engine, run_migrations
from app.models import Order, OrderItem
from app.services.db_migrate import replace_data


def _mk(eng, n: int, tag: str) -> None:
    with Session(eng) as s:
        for i in range(n):
            o = Order(date=dt.date(2027, 1, 1), title=f"{tag}-{i}", order_no=f"{tag}-{i}",
                      purchase_status="待收货")
            o.items = [OrderItem(name="物", quantity=1, unit_price_cny=None, auto=True)]
            o.compute_money()
            s.add(o)
        s.commit()


@pytest.fixture()
def src(tmp_path):
    url = f"sqlite:///{tmp_path / 'src.db'}"
    run_migrations(url)
    eng = build_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture()
def dst(tmp_path):
    url = f"sqlite:///{tmp_path / 'dst.db'}"
    run_migrations(url)
    eng = build_engine(url)
    yield eng
    eng.dispose()


def test_a_copy_taken_while_someone_writes_is_internally_consistent(src, dst):
    """拷贝进行中有人一直在写入，拷出来的每一条物品都必须找得到它的父订单。

    这是撕裂快照唯一**可判定**的形态：孤儿子行。
    只断言「行数对得上」是测不出来的——撕裂的拷贝行数同样自洽。

    去掉快照之后，这条会以两种形态之一变红：
      · 目标库开着外键 ⇒ 拷「订单物品」时直接 `CopyFailed: FOREIGN KEY constraint failed`；
      · 没开外键 ⇒ 拷完了，但下面这句孤儿断言会红。
    第一种正是生产上真正会发生的事：挂 cron 的那条备份，**只要当晚还有人在编辑就会失败**，
    而它的输出进日志、没人看——「备份静默失效」换了个机制又回来了。
    """
    _mk(src, 40, "旧")

    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set():
            try:
                _mk(src, 1, f"新{i}")
                written.append(i)
                i += 1
            except Exception:            # 写不进去不影响本条测试要验的东西
                pass
            time.sleep(0.002)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.05)                     # 让写入先跑起来，确保拷贝期间真的有并发写
    try:
        counts = replace_data(src, dst)
    finally:
        stop.set()
        t.join(timeout=5)

    assert written, "并发写入没跑起来，这条测试的前提不成立"

    with Session(dst) as s:
        order_ids = {o.id for o in s.exec(select(Order)).all()}
        items = s.exec(select(OrderItem)).all()
        orphans = [i.id for i in items if i.order_id not in order_ids]
    assert not orphans, (
        f"拷贝是撕裂的：{len(orphans)} 条物品找不到父订单（拷贝期间写入了 {len(written)} 单）")
    assert counts["orders"] == len(order_ids)


def test_the_snapshot_does_not_block_writers(src, dst):
    """开了读快照之后**写入方不许被挡住**——WAL 的读者与写者本来就不互斥。

    挡住的话，备份期间整个应用会写不进去，而备份是要挂 cron 天天跑的。
    """
    _mk(src, 200, "底")

    blocked = []

    def writer():
        t0 = time.time()
        try:
            _mk(src, 1, "并发")
        except Exception as e:           # noqa: BLE001
            blocked.append(repr(e))
        return time.time() - t0

    took = []
    t = threading.Thread(target=lambda: took.append(writer()), daemon=True)

    class _Slow(dict):
        pass

    t.start()
    replace_data(src, dst)
    t.join(timeout=10)

    assert not blocked, f"备份期间写入被挡住了：{blocked}"
