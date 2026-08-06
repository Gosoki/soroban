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


def downgrade() -> None:
    """Downgrade schema.

    ⚠️ 这一步**可能失败**：回到 ai_ci 后唯一性变严格，如果升级之后录入过仅大小写/重音不同的
    值（'Alice' 与 'alice'），重建唯一索引时会撞 1062。这不是 bug，是数据真的不满足旧约束——
    先消歧义再降级。
    """
    if not is_mysql(op.get_bind()):
        return

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
