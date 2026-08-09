"""tools/ 下的一次性脚本。

这些脚本不经 HTTP、不进 CI 的常规路径，却直接改写用户账本——历史上正是这里出过
`unit_price_cny` 拼成 `unit_unit_price_cny` 的事故：SQLModel(table=True) 关掉了 Pydantic
校验，未知 kwarg 被**静默丢弃**，单价落 NULL，`sync_from_items` 把总价重算成「只剩邮费」，
而且二次运行会把 0 固化。501 条测试一条都没覆盖到它。
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
from decimal import Decimal

import pytest
from sqlmodel import Session, SQLModel, col, create_engine, select

from app.models import Order, OrderItem, OrderStaging, StagingItem
from tools.backfill_item_price import backfill

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def iso_engine(tmp_path):
    """独立空库：回填是全表扫描，绝不能跑在共享的会话库上。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}",
                        connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


# --- 回归：货款不得被清零 -------------------------------------------------------

def test_backfill_preserves_price_for_itemless_order(iso_engine):
    """0 物品的历史订单：回填后总价必须原样保留，物品单价 = 货款。"""
    with Session(iso_engine) as s:
        s.add(Order(date=dt.date(2026, 1, 1), title="老订单", price_cny=Decimal("1234.56"),
                    fx_rate=Decimal("20.0")))
        s.commit()

    backfill(iso_engine)

    with Session(iso_engine) as s:
        o = s.exec(select(Order)).one()
        assert o.price_cny == Decimal("1234.56"), "货款被回填改动了"
        item = s.exec(select(OrderItem)).one()
        assert item.unit_price_cny == Decimal("1234.56"), "单价没落上（kwarg 被静默丢弃？）"
        assert item.quantity == 1 and item.auto is True


def test_backfill_subtracts_postage_from_seed(iso_engine):
    """种子是「货款」而非订单价：sync_from_items 会再加一次邮费，否则邮费被计两遍。"""
    with Session(iso_engine) as s:
        s.add(Order(date=dt.date(2026, 1, 1), title="含邮费", price_cny=Decimal("100.00"),
                    postage_cny=Decimal("15.00")))
        s.commit()

    backfill(iso_engine)

    with Session(iso_engine) as s:
        o = s.exec(select(Order)).one()
        assert o.price_cny == Decimal("100.00")
        assert s.exec(select(OrderItem)).one().unit_price_cny == Decimal("85.00")


def test_backfill_preserves_staging_price(iso_engine):
    """暂存侧走 StagingItem，是另一条构造路径——必须单独覆盖。"""
    with Session(iso_engine) as s:
        s.add(OrderStaging(order_no="BF-1", title="暂存老行", price_cny=Decimal("888.00")))
        s.commit()

    backfill(iso_engine)

    with Session(iso_engine) as s:
        assert s.exec(select(OrderStaging)).one().price_cny == Decimal("888.00")
        assert s.exec(select(StagingItem)).one().unit_price_cny == Decimal("888.00")


def test_backfill_is_idempotent(iso_engine):
    """二次运行必须走 skip 分支，绝不能再动一次金额（事故里正是二次运行固化了 0）。"""
    with Session(iso_engine) as s:
        s.add(Order(date=dt.date(2026, 1, 1), title="幂等", price_cny=Decimal("500.00")))
        s.commit()

    backfill(iso_engine)
    rep = backfill(iso_engine)

    assert rep["orders"]["skip"] == 1 and rep["orders"]["auto_item"] == 0
    with Session(iso_engine) as s:
        assert s.exec(select(Order)).one().price_cny == Decimal("500.00")


def test_backfill_splits_price_across_priced_items(iso_engine):
    """有物品但全部无单价：折成首件单价，**总价分毫不差**（余数进「金额尾差」行）。

    回填走的就是 `routers/common.build_items`（不再自带一份规则），所以这里钉的是
    「两条路径同结果」：同一批数据经回填工具和经 API 建单必须得到同一个总价。
    """
    with Session(iso_engine) as s:
        o = Order(date=dt.date(2026, 1, 1), title="折价", price_cny=Decimal("100.00"))
        o.items = [OrderItem(name="a", quantity=3), OrderItem(name="b", quantity=1)]
        s.add(o)
        s.commit()

    backfill(iso_engine)

    with Session(iso_engine) as s:
        o = s.exec(select(Order)).one()
        assert o.price_cny == Decimal("100.00")         # 33.33×3 + 0.01 —— 无损
        first = s.exec(select(OrderItem).where(OrderItem.name == "a")).one()
        assert first.unit_price_cny == Decimal("33.33")
        resid = s.exec(select(OrderItem).where(col(OrderItem.name).like("%（金额尾差）"))).one()
        assert resid.unit_price_cny == Decimal("0.01") and resid.quantity == 1


def test_backfill_aborts_instead_of_zeroing(iso_engine, monkeypatch):
    """事后校验的意义：万一构造又出错，必须**中止且不落库**，而不是把账本清零。"""
    import tools.backfill_item_price as mod

    def dropping_item(**kw):
        """精确复刻当年的事故：字段名拼错 → SQLModel 静默丢弃 → 单价落 NULL。"""
        kw.pop("unit_price_cny", None)
        return OrderItem(**kw)

    with Session(iso_engine) as s:
        s.add(Order(date=dt.date(2026, 1, 1), title="破坏", price_cny=Decimal("1000.00")))
        s.commit()

    monkeypatch.setattr(mod, "OrderItem", dropping_item)
    with pytest.raises(RuntimeError, match="远超取整误差"):
        backfill(iso_engine)

    with Session(iso_engine) as s:                       # 未 commit → 账本原封不动
        assert s.exec(select(Order)).one().price_cny == Decimal("1000.00")


# --- 通用护栏：静默丢弃的 kwarg ---------------------------------------------------

def _model_fields(name: str) -> set[str] | None:
    import app.models as m

    cls = getattr(m, name, None)
    if cls is None or not hasattr(cls, "__table__"):
        return None
    return set(cls.model_fields) | {c.name for c in cls.__table__.columns}


@pytest.mark.parametrize("pkg", ["app", "tools"])
def test_no_unknown_kwargs_in_model_construction(pkg):
    """SQLModel(table=True) 静默丢弃未知 kwarg —— 拼错字段名不报错，只是数据悄悄没了。
    在 AST 层扫掉整类问题，而不是等下一个 `unit_unit_price_cny` 咬人。"""
    bad = []
    for path in sorted((_BACKEND / pkg).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            fields = _model_fields(node.func.id)
            if fields is None:
                continue
            for kw in node.keywords:
                if kw.arg is not None and kw.arg not in fields:
                    rel = path.relative_to(_BACKEND)
                    bad.append(f"{rel}:{node.lineno} {node.func.id}(…{kw.arg}=…)")
    assert not bad, "模型构造传了不存在的字段名（会被静默丢弃）：\n  " + "\n  ".join(bad)
