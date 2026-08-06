"""统一有歧义的列名与状态值（审计报告「三、命名歧义」全部落地）

每一条都是「同一个名字在两处指不同东西」或「名字骗人」，读代码时必须先查一遍才敢下手：

| 表 | 旧 | 新 | 为什么 |
|---|---|---|---|
| orders / orderstaging | `shop` | `title` | 存的是**商品标题**（爬虫 normalize、UI 列头都叫「商品」），不是店铺名 |
| orderitem / stagingitem | `price_cny` | `unit_price_cny` | 与订单的 `price_cny`（**订单总价**）一名两义，且两者有 Σ单价×数量=总价 的关系，最容易看串 |
| orderstaging | `status` | `import_status` | 一行上两个 status：这个是**导入工作流**状态（待处理/已导入/已忽略） |
| orderstaging | `order_status` | `trade_status` | ……这个是**真实交易**状态（待发货/待收货/…），对齐上面 |
| orders / shipmentorder / miscexpense | `source` | `created_via` | 与 `platform`（UI 标签就叫「来源」）撞义；它其实是「这行怎么进来的」 |

状态值改名（同一字面量在两个枚举里指不同事）：
    orders.status / orderstaging.order_status 里的 `已签收` → `已入仓`
理由：订单的「已签收」= 国内快递**被集运仓**签收；集运单的「已签收」= 国际包裹**本人**收到。
同字面量还被 EXCLUDED_STATUSES 这类跨表集合共用，是实打实的坑。改后：
订单尾段 = 待收货 → 已入仓 → 集运中 → 已到达；集运单保持「已签收」不动。

三处**数据**也要跟着迁移，否则界面会白屏/丢列：
- `columnlayout.columns_json` 里存的是列键，按表名分别重映射（`items` 表的 `price_cny` 是**单价**
  → `unit_price_cny`；`orders`/`staging` 表的 `price_cny` 是订单总价 → 不动）。
- 状态值那两列（见上）。

方言说明（同 b8c9d0e1f2a3）：`ALTER TABLE ... RENAME COLUMN` 两方言同语法（MySQL 8+/SQLite 3.25+）；
普通索引 MySQL 用 `RENAME INDEX`、SQLite 只能 DROP+CREATE。被改名的列都不参与「活跃行唯一」
约束（那条只引用 order_no/platform/is_delete），故无需 drop/re-emit 生成列。

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-05 18:30:00.000000

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import is_mysql

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, Sequence[str], None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 旧列, 新列)
_COLUMNS = [
    ("orders", "shop", "title"),
    ("orderstaging", "shop", "title"),
    ("orderitem", "price_cny", "unit_price_cny"),
    ("stagingitem", "price_cny", "unit_price_cny"),
    ("orderstaging", "status", "import_status"),
    ("orderstaging", "order_status", "trade_status"),
    ("orders", "source", "created_via"),
    ("shipmentorder", "source", "created_via"),
    ("miscexpense", "source", "created_via"),
]

# (表, 旧索引名, 新索引名, 新列名)——只有被改名的列上确实有索引的才在这里
_INDEXES = [
    ("orderstaging", "ix_orderstaging_status", "ix_orderstaging_import_status", "import_status"),
    ("orders", "ix_orders_source", "ix_orders_created_via", "created_via"),
    ("shipmentorder", "ix_shipmentorder_source", "ix_shipmentorder_created_via", "created_via"),
    ("miscexpense", "ix_miscexpense_source", "ix_miscexpense_created_via", "created_via"),
]

# columnlayout.columns_json 里的列键，**按表名**分别重映射。
# 注意 price_cny：在 items 表上是「单价」列 → 跟着改；在 orders/staging 上是「订单总价」→ 不动。
_LAYOUT_KEYS = {
    "orders":   {"shop": "title"},
    "staging":  {"shop": "title", "status": "import_status", "order_status": "trade_status"},
    "items":    {"shop": "title", "price_cny": "unit_price_cny"},
}

# 状态值：仅这两列存 OrderStatus（集运单的 status 是另一套枚举，绝不能动）
_STATUS_COLUMNS_BEFORE = (("orders", "status"), ("orderstaging", "order_status"))
_STATUS_COLUMNS_AFTER = (("orders", "status"), ("orderstaging", "trade_status"))


def _rename_col(table: str, old: str, new: str) -> None:
    # 反引号在 MySQL 与 SQLite 都被接受为标识符引号；RENAME COLUMN 两方言同语法。
    op.execute(f"ALTER TABLE `{table}` RENAME COLUMN `{old}` TO `{new}`")


def _rename_index(table: str, old: str, new: str, column: str) -> None:
    if is_mysql(op.get_bind()):
        op.execute(f"ALTER TABLE `{table}` RENAME INDEX `{old}` TO `{new}`")
    else:                                    # SQLite 不支持索引改名
        op.execute(f'DROP INDEX IF EXISTS "{old}"')
        op.execute(f'CREATE INDEX "{new}" ON "{table}" ("{column}")')


def _rename_status_value(columns, old: str, new: str) -> None:
    for table, column in columns:
        op.execute(sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old")
                   .bindparams(new=new, old=old))


def _remap_layout_keys(mapping: dict[str, dict[str, str]]) -> None:
    """重写 columnlayout.columns_json 里的列键。

    用 Python 解析 JSON 再写回，而不是 SQL 字符串替换——键名之间互为子串（`status` 是
    `order_status` 的后缀），字符串替换会连带改坏另一个。"""
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT table_name, columns_json FROM columnlayout")).fetchall()
    for table_name, columns_json in rows:
        keymap = mapping.get(table_name)
        if not keymap:
            continue
        try:
            cols = json.loads(columns_json or "[]")
        except (TypeError, ValueError):      # 手改坏的 JSON：跳过而不是让整次升级失败
            continue
        changed = False
        for c in cols:
            if isinstance(c, dict) and c.get("key") in keymap:
                c["key"] = keymap[c["key"]]
                changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE columnlayout SET columns_json = :j WHERE table_name = :t")
                .bindparams(j=json.dumps(cols, ensure_ascii=False), t=table_name))


def upgrade() -> None:
    """Upgrade schema."""
    # 1) 状态值：趁 order_status 还没改名，用旧列名改
    _rename_status_value(_STATUS_COLUMNS_BEFORE, "已签收", "已入仓")
    # 2) 索引：先按旧名 drop/rename，再改列名（SQLite 重建索引要引用新列名，故顺序是 drop→改列→create）
    for table, old, new, column in _INDEXES:
        if is_mysql(op.get_bind()):
            continue                          # MySQL 的 RENAME INDEX 与列名无关，放到改完列再做
        op.execute(f'DROP INDEX IF EXISTS "{old}"')
    # 3) 列改名
    for table, old, new in _COLUMNS:
        _rename_col(table, old, new)
    # 4) 索引重建/改名
    for table, old, new, column in _INDEXES:
        if is_mysql(op.get_bind()):
            op.execute(f"ALTER TABLE `{table}` RENAME INDEX `{old}` TO `{new}`")
        else:
            op.execute(f'CREATE INDEX "{new}" ON "{table}" ("{column}")')
    # 5) 存在 columnlayout 里的列键
    _remap_layout_keys(_LAYOUT_KEYS)


def downgrade() -> None:
    """Downgrade schema."""
    _remap_layout_keys({t: {v: k for k, v in m.items()} for t, m in _LAYOUT_KEYS.items()})
    for table, old, new, column in _INDEXES:
        if is_mysql(op.get_bind()):
            op.execute(f"ALTER TABLE `{table}` RENAME INDEX `{new}` TO `{old}`")
        else:
            op.execute(f'DROP INDEX IF EXISTS "{new}"')
    for table, old, new in _COLUMNS:
        _rename_col(table, new, old)
    for table, old, new, column in _INDEXES:
        if not is_mysql(op.get_bind()):
            src = {"import_status": "status", "created_via": "source"}[column]
            op.execute(f'CREATE INDEX "{old}" ON "{table}" ("{src}")')
    _rename_status_value(_STATUS_COLUMNS_BEFORE, "已入仓", "已签收")
