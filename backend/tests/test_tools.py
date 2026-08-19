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


# --- 演示数据脚本不许碰真账本 -----------------------------------------------------

def _stub_demo(monkeypatch, demo, *, backend, engine=None):
    """把 demo 的外部副作用换成记录器。**被断言的那几个调用不算「被绕过」**——
    这条测试要问的正是「它们有没有被调、按什么顺序」。
    """
    touched = []
    monkeypatch.setattr(demo, "current_backend", lambda: backend)
    monkeypatch.setattr(demo, "create_db_and_tables", lambda: touched.append("迁移"))
    monkeypatch.setattr(demo.single_process, "acquire", lambda url: touched.append("拿闸"))
    if engine is not None:
        monkeypatch.setattr(demo, "get_engine", lambda: engine)
        # **`app.seed` 那份也要换。** `ensure_admin()` 用的是它自己模块里的 `get_engine`，
        # 只换 demo 这一份的话，管理员会被建到会话共享库上，而断言查的是临时库
        # ⇒ 「建号排在闸前」这条破坏两种情况下都不红（判据被另一个原因满足）。
        from app import seed as _seed
        monkeypatch.setattr(_seed, "get_engine", lambda: engine)
    return touched


def test_demo_refuses_a_remote_backend_before_touching_it(monkeypatch, capsys):
    """**闸必须排在任何一次动库之前。**

    第一版把它写在 `create_db_and_tables()` **之后**，而那一句做的是
    「对当前生效的数据后端跑完整条 alembic upgrade」——MySQL 后端的用户跑一次
    `python -m app.demo`，生产库先被跑完整链迁移，**然后**才打印「已中止」。
    而且那一路完全没有单进程闸：soroban 正开着时就是「两个进程同时 ALTER 同一个库」。
    """
    from app import demo

    monkeypatch.delenv("SOROBAN_DEMO_YES", raising=False)
    touched = _stub_demo(monkeypatch, demo, backend="mysql")
    demo.main()

    out = capsys.readouterr().out
    assert "已中止" in out and "mysql" in out, out
    assert touched == [], f"中止之前已经动了库：{touched}"

    # **反面**：显式确认之后必须放行，而且顺序是「先拿闸、再迁移」
    monkeypatch.setenv("SOROBAN_DEMO_YES", "1")
    touched2 = _stub_demo(monkeypatch, demo, backend="mysql")
    try:
        demo.main()
    except Exception:
        pass                      # 之后会真去连库，本条只关心前两步
    assert touched2[:2] == ["拿闸", "迁移"], touched2


def test_demo_leaves_an_existing_ledger_completely_alone(tmp_path, monkeypatch, capsys):
    """任一张业务表非空就跳过，而且**跳过之前一个字节都不许写**。

    闸只看商品订单是不够的：只记了集运单/杂项的新用户会被放行。
    而建号原先排在这道闸**之前** ⇒ 一本已经在用的账本会先凭空多出一个
    用公开默认口令的管理员，紧接着才打印「不覆盖任何现有数据」——那句话当场是假的。

    这条在**自己的临时库**上跑真流程（上一版靠 mock 掉 `create_db_and_tables` 来避开，
    结果既遮住了上面那条 bug，又真的往会话共享库里灌了一整套演示数据）。
    """
    import datetime as dt

    from sqlmodel import Session, select

    from app.database import build_engine, run_migrations
    from app.models import Order, ShipmentOrder, User

    url = f"sqlite:///{tmp_path / 'demo-target.db'}"
    run_migrations(url)
    e = build_engine(url)
    try:
        with Session(e) as s:                     # 只有集运单：闸若只看商品订单就会放行
            s.add(ShipmentOrder(date=dt.date(2027, 1, 1)))
            s.commit()

        from app import demo
        monkeypatch.setenv("SOROBAN_DEMO_YES", "1")
        _stub_demo(monkeypatch, demo, backend="sqlite", engine=e)
        demo.main()

        out = capsys.readouterr().out
        assert "集运订单" in out and "跳过" in out, out
        with Session(e) as s:
            assert s.exec(select(User)).first() is None, "跳过之前先建了个管理员"
            assert s.exec(select(Order)).first() is None, "还是灌了演示数据"
    finally:
        e.dispose()


def test_demo_actually_seeds_an_empty_ledger(tmp_path, monkeypatch, capsys):
    """**反面**：空库要真的灌进去，否则上面那两条闸写成「永远不灌」也能过。"""
    from sqlmodel import Session, select

    from app.database import build_engine, run_migrations
    from app.models import Order, ShipmentOrder

    url = f"sqlite:///{tmp_path / 'demo-empty.db'}"
    run_migrations(url)
    e = build_engine(url)
    try:
        from app import demo
        monkeypatch.setenv("SOROBAN_DEMO_YES", "1")
        _stub_demo(monkeypatch, demo, backend="sqlite", engine=e)
        demo.main()
        with Session(e) as s:
            assert s.exec(select(Order)).first() is not None, "空库也没灌进去"
            assert s.exec(select(ShipmentOrder)).first() is not None
    finally:
        e.dispose()


def test_env_can_set_the_first_admin_credentials(tmp_path, monkeypatch):
    """`.env` 里的 `SOROBAN_ADMIN_USER/PASS` 必须真的生效。

    `seed.ensure_admin()` 读的是 `os.getenv`，而 `.env` 只被 pydantic-settings 读进
    `Settings`（这两个键还不是 Settings 的字段），**从不写回 `os.environ`**。
    于是冻结版用户在 `.env` 里写 `SOROBAN_ADMIN_PASS=强口令`，首启建出来的仍是默认口令
    ——而 `.env` 是双击用户**唯一**能编辑的入口（`run.py` 自己的注释就是这么写的）。
    删掉「把口令打到控制台」是对的，但替代文案不能指向一个不生效的地方。
    """
    import run as run_mod

    for k in ("SOROBAN_ADMIN_USER", "SOROBAN_ADMIN_PASS", "HOST", "BACKEND_PORT"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text(
        "SOROBAN_ADMIN_USER=gosoki\nSOROBAN_ADMIN_PASS=一个很长的口令\n", encoding="utf-8")

    # ① 启动器的 .env 解析器读得出这两项（整支 main() 会去起 uvicorn，只能单独调它）
    assert run_mod._runtime_setting(tmp_path, "SOROBAN_ADMIN_USER", "") == "gosoki"
    assert run_mod._runtime_setting(tmp_path, "SOROBAN_ADMIN_PASS", "") == "一个很长的口令"

    # ② 而且 main() 里确实把它们**落进了 os.environ**——这一步是承重的：
    #    seed 读的是 os.getenv，.env 不会自己进环境变量。
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(run_mod.main))
    keys = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.startswith("SOROBAN_ADMIN"):
            keys.add(node.value)
    assert keys == {"SOROBAN_ADMIN_USER", "SOROBAN_ADMIN_PASS"}, (
        f"启动器没把 .env 里的这两项落进 os.environ：{keys}。"
        "`seed.ensure_admin()` 读的是 os.getenv，而 .env 不会自己进环境变量")
