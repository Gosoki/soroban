"""把「键列」的 MySQL 排序规则改成二进制，与 SQLite 的 BINARY 对齐

**问题**：baseline 建表时只给了 `mysql_charset='utf8mb4'` 而没给 COLLATE。MySQL 的规则是
「表声明了 CHARACTER SET 却没声明 COLLATE 时，取该**字符集的默认排序规则**」——不是库的
（所以 `ensure_database` 里那句 `COLLATE utf8mb4_unicode_ci` 从来没生效过）。于是全部字符串
列落在 `utf8mb4_0900_ai_ci`：大小写不敏感**且**重音不敏感。实测 MySQL 9.7 判为相等的有：

    'Alice'='alice'   'José'='Jose'   'ＡＢＣ'='ABC'   'ヤマダ'='やまだ'   'ｶﾞ'='ガ'

而 SQLite 无 COLLATE 即 BINARY 逐字节。同一份数据两种语义，后果：
- SQLite 里合法共存的 'Alice'/'alice' 两个标签，拷进 MySQL 撞 1062 →
  `replace_data` 是单事务，**整次迁移回滚**，而 UI 里还无法自救（删标签说"正被使用"、
  改名说"新名字已被占用"）。
- `routers/tags` 的按值批量改名/删除用 `WHERE col = value`：MySQL 上会命中**本不该动的行**，
  按账号清空暂存会硬删掉另一个账号的行（连同 StagingItem，不可恢复）。

**改哪些列**：只改两类，别的列（备注、标题、商品分类…）按 ci 比较对用户反而更自然：
  1. 参与唯一约束的（含活跃唯一约束的生成列及其组成列）
  2. 被 `WHERE col = value` 批量精确匹配的（tags 的改名 / 按账号清空）

**为什么是 utf8mb4_0900_bin 而不是 utf8mb4_bin**：后者是 PAD SPACE（`'a ' = 'a'`），
只有前者是 NO PAD，才与 SQLite BINARY 真正等价。实测：

    SHOW COLLATION LIKE 'utf8mb4%bin'
    → utf8mb4_0900_bin  NO PAD  /  utf8mb4_bin  PAD SPACE

`utf8mb4_0900_bin` 是 MySQL 8.0+ 才有（MariaDB 没有），故 `dialect.bin_collation()` 会探测
并回退到 `utf8mb4_bin`——尾空格折叠的影响面远小于大小写/重音折叠，何况入口都 .strip() 过。

**SQLite 侧无 DDL**：它本来就是 BINARY，这一版对 SQLite 是纯 no-op。

**顺序很重要**：生成列依赖基础列，必须先拆生成列 → 再改基础列 → 最后按新排序规则重建，
否则 MySQL 会拒绝改一个被生成列引用的列。

**升级方向不会失败**：ci → bin 只会让唯一性**更宽松**（原本被判重的值本就存不进来），
不可能撞约束。反方向（downgrade）则可能撞 1062——见 downgrade 的说明。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-06 06:20:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.db.dialect import bin_collation, drop_active_unique, emit_active_unique, is_mysql

# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (表, 列, 长度, 可空) —— 长度/可空必须与模型一致，因为 MODIFY 会重写整个列定义，
# 漏写 NOT NULL 就会把约束悄悄丢掉。
_KEY_COLUMNS = [
    ('tagoption',     'field',            32,  False),   # 唯一索引 (field, value)
    ('tagoption',     'value',            128, False),   # 同上
    ('user',          'username',         64,  False),   # 唯一索引
    ('orders',        'order_no',         64,  True),    # 活跃唯一键组成列
    ('orders',        'platform',         32,  True),    # 同上 + tags 批量匹配
    ('orders',        'platform_account', 64,  True),    # tags 批量改名/按账号清空
    ('orderstaging',  'order_no',         64,  True),    # 活跃唯一键组成列
    ('orderstaging',  'platform',         32,  True),    # tags 批量匹配
    ('orderstaging',  'platform_account', 64,  True),    # 同上
    ('shipmentorder', 'shipment_no',      64,  True),    # 活跃唯一键组成列
    ('shipmentorder', 'recipient',        128, True),    # tags 批量改名
]

# 三处「活跃行唯一」约束：拆掉再按新排序规则重建（参数与 head 状态一致）。
_ACTIVE_UNIQUE = [
    dict(table='orders',
         index_name='ix_orders_order_no_platform_active',
         gen_col='order_no_platform_active_key',
         mysql_expr="CASE WHEN order_no IS NOT NULL AND is_delete = 0 "
                    "THEN CONCAT(order_no, CHAR(31 USING utf8mb4), COALESCE(platform, '')) END",
         sqlite_columns="order_no, COALESCE(platform, '')",
         sqlite_where='order_no IS NOT NULL AND is_delete = 0'),
    dict(table='orderstaging',
         index_name='ix_staging_order_no',
         gen_col='order_no_active_key',
         mysql_expr="CASE WHEN order_no IS NOT NULL THEN order_no END",
         sqlite_columns='order_no',
         sqlite_where='order_no IS NOT NULL'),
    dict(table='shipmentorder',
         index_name='ix_shipmentorder_shipment_no_active',
         gen_col='shipment_no_active_key',
         mysql_expr="CASE WHEN shipment_no IS NOT NULL AND is_delete = 0 THEN shipment_no END",
         sqlite_columns='shipment_no',
         sqlite_where='shipment_no IS NOT NULL AND is_delete = 0'),
]


def _set_collation(collation: str | None) -> None:
    """把键列改成指定排序规则；collation=None 表示回落到表默认（即 ai_ci）。"""
    for table, column, length, nullable in _KEY_COLUMNS:
        op.alter_column(
            table, column,
            existing_type=mysql.VARCHAR(length),
            type_=mysql.VARCHAR(length, collation=collation) if collation
            else mysql.VARCHAR(length),
            existing_nullable=nullable,
        )


def upgrade() -> None:
    """Upgrade schema."""
    if not is_mysql(op.get_bind()):
        return                                  # SQLite 本来就是 BINARY，无需任何 DDL

    for spec in _ACTIVE_UNIQUE:                 # 1) 拆生成列（否则基础列改不动）
        drop_active_unique(op, table=spec['table'], index_name=spec['index_name'],
                           gen_col=spec['gen_col'])
    _set_collation(bin_collation(op.get_bind()))  # 2) 基础列换二进制排序规则
    for spec in _ACTIVE_UNIQUE:                 # 3) 重建（emit 会给生成列也带上 COLLATE）
        emit_active_unique(op, **spec)


# 降级会把这些键列换回 ai_ci（大小写/重音不敏感），于是「原本合法的两行」变成重复。
# (表, 人话, 用来分组的表达式) ——表达式必须与降级后那道唯一约束**逐字对应**。
_AI_CI = "utf8mb4_0900_ai_ci"
_DOWNGRADE_CONFLICTS = [
    ('tagoption', '标签取值', "CONCAT(field, CHAR(31 USING utf8mb4), value)", None),
    ('user', '用户名', "username", None),
    ('orders', '商品订单号', "CONCAT(order_no, CHAR(31 USING utf8mb4), COALESCE(platform, ''))",
     "order_no IS NOT NULL AND is_delete = 0"),
    ('orderstaging', '暂存订单号', "order_no", "order_no IS NOT NULL"),
    ('shipmentorder', '集运单号', "shipment_no", "shipment_no IS NOT NULL AND is_delete = 0"),
]


def _conflicts_after_downgrade(conn) -> list[str]:
    """降级之后会撞 1062 的值，全查一遍。**在动 schema 之前调用。**

    `c2d3e4f5a6b7` 已经为同一件事立过规矩（「先查数据，再动 schema」），
    但这一条当时没跟上，而它比那条更危险：降级的第一步就把**三处**「活跃行唯一」
    连同 MySQL 生成列一起 DROP 掉，随后才跑它自己在 docstring 里承认「可能撞 1062」的那步。
    **MySQL 的 DDL 是隐式提交的**，而 `env.py` 开了 transaction_per_migration——
    于是失败之后：三处唯一约束全没了，版本号却被回滚、仍停在 `f2a3b4c5d6e7`。

    用户只看到一句 1062 原始报错，然后**一切看起来完全正常**：下次启动
    `upgrade head` 从这里往后照跑（后面的迁移都成功），应用正常打开、页面正常，
    没有任何一处会提示约束没了。他会以为「降级失败了，升回去就没事」。
    真正的后果很久以后才显形——同一个订单号可以重复导入、重复建单，不再有 409，
    看板合计凭空变大，而**没有任何机制会再把那三条约束建回来**（后面的迁移都不碰它们）。

    一次性把五处全查完再报，而不是撞一个报一个：用户要的是「一共要清理哪些」。
    """
    found: list[str] = []
    for table, label, expr, where in _DOWNGRADE_CONFLICTS:
        # **`COLLATE` 要写在 SELECT 的表达式上、再按别名分组。** 写成
        # `SELECT expr ... GROUP BY (expr) COLLATE ai_ci` 在 `only_full_group_by`
        # （MySQL 8 的默认 sql_mode）下直接 1055——两个表达式不是同一个。
        # 这个错只有真连 MySQL 才看得见，纯 SQLite 的测试一条都发现不了。
        sql = (f"SELECT ({expr}) COLLATE {_AI_CI} AS k, COUNT(*) c FROM `{table}` "
               + (f"WHERE {where} " if where else "")
               + "GROUP BY k HAVING c > 1 LIMIT 5")
        # **查询失败一律往外抛，不吞。** 到这一步五张表都必然存在，查不动只可能是
        # 预检自己坏了或环境有问题——而「不知道有没有冲突」时继续降级是最危险的选择
        # （下一步就 DROP 掉三处唯一约束，且 MySQL 的 DDL 隐式提交、再也建不回来）。
        # 第一版在这里 `except` 成「查不了，请手工确认」并计入冲突，实测更糟：
        # SQL 一写错就变成**永远拒绝降级**，而且看起来像是数据有问题。
        rows = conn.execute(sa.text(sql)).fetchall()
        if rows:
            listed = "、".join(f"{r[0]}（{r[1]} 条）" for r in rows)
            found.append(f"{label}（{table}）：{listed}")
    return found


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 这一步**可能失败**：回到 ai_ci 后唯一性变严格，如果升级之后录入过仅大小写/重音不同的
    值（'Alice' 与 'alice'），重建唯一索引时会撞 1062。这不是 bug，是数据真的不满足旧约束——
    先消歧义再降级。
    """
    conn = op.get_bind()
    if not is_mysql(conn):
        return

    # **先查数据，再动 schema。** 下面第一步就会 DROP 掉三处唯一约束，而 MySQL 的 DDL
    # 隐式提交——之后任何一步失败，那三条约束就永久没了（详见 `_conflicts_after_downgrade`）。
    if conn is not None:                        # 离线 --sql 模式查不了；那条路本来也执行不了数据步骤
        conflicts = _conflicts_after_downgrade(conn)
        if conflicts:
            raise RuntimeError(
                "降级会把这些键列换回大小写/重音**不敏感**的排序规则，"
                "而库里存在只差大小写/重音的值——那正是本条迁移放开的东西：\n  "
                + "\n  ".join(conflicts)
                + "\n请先在应用里改掉或合并它们，再降级。"
                  "（一个字节都还没改，库仍是完好的——"
                  "真让它跑下去，三处唯一约束会先被删掉且再也建不回来。）")

    for spec in _ACTIVE_UNIQUE:
        drop_active_unique(op, table=spec['table'], index_name=spec['index_name'],
                           gen_col=spec['gen_col'])
    _set_collation(None)                        # 回落表默认（utf8mb4_0900_ai_ci）
    # emit_active_unique 现在恒带 COLLATE；降级要的是「不带」，故手工重建生成列。
    for spec in _ACTIVE_UNIQUE:
        op.execute(
            f"ALTER TABLE `{spec['table']}` ADD COLUMN `{spec['gen_col']}` VARCHAR(512) "
            f"GENERATED ALWAYS AS ({spec['mysql_expr']}) STORED"
        )
        op.execute(
            f"CREATE UNIQUE INDEX `{spec['index_name']}` "
            f"ON `{spec['table']}` (`{spec['gen_col']}`)"
        )
