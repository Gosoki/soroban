"""状态列一律带业务段前缀：不再有裸 status

| 表 | 旧 | 新 |
|---|---|---|
| orders | `status` | `purchase_status` |
| orderstaging | `trade_status` | `purchase_status` |
| shipmentorder | `status` | `shipment_status` |

**为什么**：`status` 这个裸名在本仓同时指四件事——订单的国内采购段、集运单的国际段、
暂存的导入工作流、HTTP 响应码。歧义已经咬过三次（详见 docs/审计报告-2026-08.md）。
更要紧的是它**挡着以后的路**：路线图上有「卖出/利润」，届时订单会多出卖出侧状态，
中文值域还高度重合（待付款/待发货/待收货），`status` 到那时就是必然的二义。

采用的公理：**状态列名 = `<业务段>_status`；同名 ⟺ 同概念（同一枚举、同一值域）。**
- `purchase_status` 用在 orders 与 orderstaging 上——它们本就是同一件事的两处表示
  （`_SHARED_TO_ORDER` 里 `trade_status → status` 这条映射就是证据），
  统一之后那条映射变成恒等。
- 不叫 `trade_status`：卖出同样是 trade，`Order.trade_status` 与 `Sale.trade_status`
  同名不同义正是这次要消灭的病。`purchase_` 永远只指买入侧。
- `shipment_status` 沿用仓内既有词汇（ShipmentOrder / ShipmentStatus / /api/shipment），
  不引入 `consolidation` 造第三套说法。

**零数据回填**：只改列名与索引名，状态**值**一个字节不动（值改名是另一类风险，
`b4c5d6e7f8a9` / `c5d6e7f8a9b0` 干过，不与列改名混做）。因此本迁移完全可逆、可 roundtrip。

⚠️ 索引那步两方言分叉，顺序不能错（沿用 e1f2a3b4c5d6 的做法）：
   SQLite：先 DROP 旧索引 → 改列 → 按新列名 CREATE。**漏掉 DROP 不会报错**——
           SQLite 的 RENAME COLUMN 会自动重写索引定义，结果是两条索引并存、
           测试全绿、而 schema 与 MySQL 永久发散。
   MySQL：先改列 → 再 RENAME INDEX（它与列名无关）。
   MySQL 的 RENAME INDEX **没有 IF EXISTS**，所以先探测旧索引在不在。

⚠️ 不用 CHANGE COLUMN / MODIFY：那要重述 `VARCHAR(32) NOT NULL`，漏写就静默丢约束。
⚠️ 不顺手加 COLLATE：f2a3b4c5d6e7 的 _KEY_COLUMNS 刻意只覆盖唯一约束列与被
   `WHERE col = value` 批改的列，状态列两者都不是；加了反而让 MySQL 契约测试变红。

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.dialect import is_mysql

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (表, 旧列名, 新列名)
_COLUMNS = [
    ("orders", "status", "purchase_status"),
    ("orderstaging", "trade_status", "purchase_status"),
    ("shipmentorder", "status", "shipment_status"),
]

# (表, 旧索引名, 新索引名, 新列名, 旧列名)
# 第 5 位是旧列名：downgrade 时 SQLite 要按**旧**列名重建索引。
# orderstaging.trade_status 没有索引（models/order/staging.py 上没有 index=True），
# 所以不在这张表里——凭印象补一条会让 downgrade 尝试建一条从不存在的索引。
_INDEXES = [
    ("orders", "ix_orders_status", "ix_orders_purchase_status", "purchase_status", "status"),
    ("shipmentorder", "ix_shipmentorder_status", "ix_shipmentorder_shipment_status",
     "shipment_status", "status"),
]

# columnlayout.columns_json 里存着用户拖过的列键，按表名分别重映射。
# ⚠️ **绝不能**给 staging 加 {"status": ...}：`status` 在 e1f2a3b4c5d6 之前是
# import_status 的旧名，downgrade 时会把「交易状态」列错位成「导入状态」列。
_LAYOUT_KEYS = {
    "orders": {"status": "purchase_status"},
    "items": {"status": "purchase_status"},
    "shipment": {"status": "shipment_status"},
    "staging": {"trade_status": "purchase_status"},
}


def _rename_col(table: str, old: str, new: str) -> None:
    # 反引号在 MySQL 与 SQLite 都被接受为标识符引号；RENAME COLUMN 两方言同语法。
    op.execute(f"ALTER TABLE `{table}` RENAME COLUMN `{old}` TO `{new}`")


def _index_names(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def _remap_layout_keys(mapping: dict[str, dict[str, str]]) -> None:
    """重写 columnlayout.columns_json 里的列键。

    解析 JSON 再按 key 精确替换，**不能**做 SQL 字符串替换：`status` 是 `import_status`
    的后缀，字符串替换会把暂存页的「导入状态」列一起改坏。
    """
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
                .bindparams(j=json.dumps(cols, ensure_ascii=False), t=table_name)
            )


def _move_indexes(pairs) -> None:
    """pairs = [(表, 现索引名, 目标索引名, 目标列名)]，在列已改名之后调用。"""
    mysql = is_mysql(op.get_bind())
    for table, cur, want, column in pairs:
        if mysql:
            if cur in _index_names(table):          # RENAME INDEX 没有 IF EXISTS
                op.execute(f"ALTER TABLE `{table}` RENAME INDEX `{cur}` TO `{want}`")
            elif want not in _index_names(table):
                op.execute(f"CREATE INDEX `{want}` ON `{table}` (`{column}`)")
        else:
            op.execute(f'CREATE INDEX IF NOT EXISTS "{want}" ON "{table}" ("{column}")')


def upgrade() -> None:
    """Upgrade schema."""
    mysql = is_mysql(op.get_bind())
    # 1) SQLite：先丢旧索引。不丢的话 RENAME COLUMN 会把它连同定义一起带过去，
    #    留下一条名字还叫 ix_orders_status 的索引，与 MySQL 侧永久发散。
    if not mysql:
        for table, old, _new, _col, _oldcol in _INDEXES:
            op.execute(f'DROP INDEX IF EXISTS "{old}"')
    # 2) 列改名
    for table, old, new in _COLUMNS:
        _rename_col(table, old, new)
    # 3) 索引改名/重建
    _move_indexes([(t, old, new, col) for t, old, new, col, _ in _INDEXES])
    # 4) 用户存下来的列布局
    _remap_layout_keys(_LAYOUT_KEYS)


def downgrade() -> None:
    """Downgrade schema."""
    mysql = is_mysql(op.get_bind())
    _remap_layout_keys({t: {v: k for k, v in m.items()} for t, m in _LAYOUT_KEYS.items()})
    if not mysql:
        for table, _old, new, _col, _oldcol in _INDEXES:
            op.execute(f'DROP INDEX IF EXISTS "{new}"')
    for table, old, new in _COLUMNS:
        _rename_col(table, new, old)
    _move_indexes([(t, new, old, oldcol) for t, old, new, _col, oldcol in _INDEXES])
