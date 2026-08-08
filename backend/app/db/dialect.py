"""方言翻译层：把 SQLite / MySQL 之间的「语法差异」集中翻译，业务模型与迁移只声明意图。

目前翻译的差异：
1. **软删/空值感知的唯一约束**——本项目多处需要「仅对未软删且关键列非空的行唯一」：
   - SQLite：直接用带 WHERE 的**部分唯一索引**（partial unique index）。
   - MySQL：不支持部分索引 → 改用 **STORED 生成列**（删除行/空值时算出 NULL，而 NULL 在
     唯一索引里互不冲突），再对生成列建唯一键。语义与 SQLite 部分索引完全等价。

   两条路径都由本模块产出（见 emit_active_unique / drop_active_unique），迁移脚本只描述
   「哪几列、什么条件下唯一」，不关心具体方言。

2. **方言探测**——is_sqlite / is_mysql，供**迁移脚本**按方言短路（6 个迁移在用），
   外加 services/db_migrate.py 与 routers/dashboard.py。
   database.py 与 alembic/env.py **不**走这里：它们拿得到的是引擎/连接对象，
   直接读 `dialect.name` 更直白，没必要绕一层函数。
"""
from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from sqlalchemy import String
from sqlalchemy.dialects import mysql as _mysql
from sqlalchemy.dialects.mysql import insert as _mysql_insert
from sqlalchemy.dialects.sqlite import insert as _sqlite_insert
from sqlalchemy.engine import Connection, Dialect, Engine


def _name(bind=None) -> str:
    """取当前方言名。bind 可传 Connection/Engine/Dialect；不传则用 Alembic 的 op.get_bind()。"""
    if bind is None:
        from alembic import op  # 延迟导入：非迁移场景（database.py）不依赖 alembic 上下文
        bind = op.get_bind()
    if isinstance(bind, Dialect):
        return bind.name
    if isinstance(bind, (Connection, Engine)):
        return bind.dialect.name
    return getattr(getattr(bind, "dialect", None), "name", str(bind))


def is_sqlite(bind=None) -> bool:
    return _name(bind) == "sqlite"


def is_mysql(bind=None) -> bool:
    return _name(bind) in ("mysql", "mariadb")


# --- 比较语义：让 MySQL 的字符串列与 SQLite 一样逐字节比较 ---------------------------
# 建表时只给 CHARACTER SET 不给 COLLATE，MySQL 会取**字符集的默认排序规则**（不是库的），
# 即 utf8mb4_0900_ai_ci —— 大小写不敏感**且**重音不敏感。实测 MySQL 9.7 判为相等的有：
#     'Alice'='alice'  'José'='Jose'  'ＡＢＣ'='ABC'  'ヤマダ'='やまだ'  'ｶﾞ'='ガ'
# 对一个日本代购账本，这远不止「大小写」那么窄。而 SQLite 无 COLLATE 即 BINARY 逐字节。
# 后果：同一份数据，唯一键在 SQLite 上合法共存、拷进 MySQL 撞 1062；`WHERE col = v` 的
# 批量改名/删除在 MySQL 上会命中本不该动的行。
#
# 选 utf8mb4_0900_bin 而不是 utf8mb4_bin：后者是 PAD SPACE（'a ' = 'a'），
# 只有前者是 NO PAD，才与 SQLite BINARY 真正等价。实测：
#     SHOW COLLATION LIKE 'utf8mb4%bin' → utf8mb4_0900_bin=NO PAD / utf8mb4_bin=PAD SPACE
# 代价：utf8mb4_0900_bin 是 MySQL 8.0+ 才有的（MariaDB 没有）。迁移里会探测并回退，
# 见 alembic/versions/f2a3b4c5d6e7_binary_collation_for_key_columns.py。
BIN_COLLATION = "utf8mb4_0900_bin"
BIN_COLLATION_FALLBACK = "utf8mb4_bin"          # 8.0 以前 / MariaDB；PAD SPACE，尾空格仍会折叠


def bin_collation(bind=None) -> str:
    """本服务端可用的二进制排序规则名。8.0+ 用 _0900_bin，老服务端退到 _bin。

    宁可退到 PAD SPACE 的 utf8mb4_bin，也好过因为一个不存在的排序规则让整次升级失败：
    尾空格折叠的影响面远小于大小写/重音折叠，何况所有入口都 .strip() 过。"""
    if bind is None:
        from alembic import op
        bind = op.get_bind()
    try:
        from sqlalchemy import text as _text
        found = bind.execute(_text("SHOW COLLATION LIKE :c"), {"c": BIN_COLLATION}).first()
        return BIN_COLLATION if found else BIN_COLLATION_FALLBACK
    except Exception:                            # noqa: BLE001  探测失败就用保守值
        return BIN_COLLATION_FALLBACK


# 与 BIN_COLLATION 配套的**大小写不敏感**排序规则，只用于模糊搜索（见 ci_contains）。
CI_COLLATION = "utf8mb4_0900_ai_ci"
CI_COLLATION_FALLBACK = "utf8mb4_general_ci"    # 8.0 以前 / MariaDB


def ci_contains(col, q: str, bind=None):
    """BinStr 列上的**模糊搜索**：显式拉回大小写不敏感，与 SQLite 的 LIKE 对齐。

    BinStr 让 MySQL 的 `=` 逐字节，但它有个没被论证过的副作用：同列上的 `LIKE` 也跟着
    变成大小写敏感（MySQL 的 LIKE 采用列的排序规则，列的 coercibility 低于字面量），
    而 SQLite 的 LIKE 对 ASCII 本来就不敏感。于是同一份数据、同一个搜索词，
    两个后端返回不同结果——正是引入 BinStr 那一版想消灭的那类发散。

    引入 BinStr 是为了「唯一性」与「等值批改」两件事，不包括搜索；这里把搜索口径拉回原状。
    非 BinStr 列（title / express_no / 物品名）本来就是 ci，不必也不该走这里。"""
    if not is_mysql(bind):
        return col.contains(q, autoescape=True)
    return col.collate(_ci_collation(bind)).contains(q, autoescape=True)


def _ci_collation(bind=None) -> str:
    try:
        from sqlalchemy import text as _text
        conn = bind.connection if hasattr(bind, "connection") else bind
        found = conn.execute(_text("SHOW COLLATION LIKE :c"), {"c": CI_COLLATION}).first()
        return CI_COLLATION if found else CI_COLLATION_FALLBACK
    except Exception:                            # noqa: BLE001  探测失败就用保守值
        return CI_COLLATION_FALLBACK


def UtcDateTime():                              # noqa: N802  —— 当类型用，故用类型的命名风格
    """保留微秒的时间列：MySQL 显式 DATETIME(6)，SQLite 本来就存全精度 ISO 字符串。

    不写 fsp 的话 MySQL 建出来是 DATETIME(0)，而超出精度的值是**四舍五入**而非截断，也不报
    warning。实测 MySQL 9.7：存 '2026-08-05 14:59:59.700000' 读回 '2026-08-05 15:00:00'
    ——跨了分钟和小时，落在 UTC 日界附近时还会跨日（暂存页「入库日期」按 JST 显示，
    迁移前后会差一天）。
    影响面确实不大（指纹是同引擎自比、排序都有 id 兜底），但「同一份数据迁过去精度就变了」
    这件事本身就不该有，何况修它是零代价的。"""
    return sa.DateTime().with_variant(_mysql.DATETIME(fsp=6), "mysql")


def BinStr(length: int):                        # noqa: N802  —— 当类型用，故用类型的命名风格
    """「逐字节比较」的字符串列：MySQL 显式 utf8mb4_0900_bin，SQLite 本来就是 BINARY。

    用在两类列上，别的列不必动（用户可见文本按 ci 比较反而更自然）：
      1. 参与唯一约束的（含 emit_active_unique 生成列的组成列）
      2. 被 `WHERE col = value` 批量改写/删除命中的（见 routers/tags 的改名与按账号清空）
    """
    return String(length).with_variant(
        _mysql.VARCHAR(length, collation=BIN_COLLATION), "mysql"
    )


# --- 跨方言的幂等插入/更新（业务代码在运行期用，传 session.get_bind() 探方言）------------
# 背景：SQLite 的 `insert(...).on_conflict_*` 只能在 SQLite 上编译，直接用会在 MySQL 上抛
# UnsupportedCompilationError。这里按方言产出等价语句：SQLite 走 ON CONFLICT，MySQL 走
# INSERT IGNORE / ON DUPLICATE KEY UPDATE。

def insert_or_ignore(bind, table, values: dict, index_elements: list[str]):
    """「插入；唯一键冲突则忽略」（幂等插入）。返回可 session.execute 的 statement。
    SQLite → ON CONFLICT DO NOTHING；MySQL → INSERT IGNORE。index_elements 仅 SQLite 用（指明冲突目标）。"""
    if is_mysql(bind):
        return _mysql_insert(table).values(**values).prefix_with("IGNORE")
    return _sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=index_elements)


def upsert(bind, table, values: dict, index_elements: list[str], update: dict):
    """「插入；唯一键冲突则更新 update 指定的列」。返回可 session.execute 的 statement。
    SQLite → ON CONFLICT DO UPDATE；MySQL → ON DUPLICATE KEY UPDATE。"""
    if is_mysql(bind):
        return _mysql_insert(table).values(**values).on_duplicate_key_update(**update)
    return _sqlite_insert(table).values(**values).on_conflict_do_update(
        index_elements=index_elements, set_=update
    )


# --- 软删/空值感知唯一约束的方言翻译（迁移里调用）---------------------------------

def emit_active_unique(
    op,
    *,
    table: str,
    index_name: str,
    gen_col: str,
    mysql_expr: str,
    sqlite_columns: str,
    sqlite_where: str,
) -> None:
    """建「活跃行唯一」约束。

    - table         表名
    - index_name    唯一索引名（两方言一致，便于 drop）
    - gen_col       MySQL 侧生成列列名（SQLite 不建列）
    - mysql_expr    MySQL 生成列表达式；活跃行算出唯一键、其余算出 NULL
                    （NULL 不参与唯一 → 等价于「仅活跃行唯一」）
    - sqlite_columns 部分索引的列/表达式列表，如 "order_no, COALESCE(platform, '')"
    - sqlite_where  部分索引 WHERE 条件，如 "order_no IS NOT NULL AND is_delete = 0"
    """
    if is_mysql(op.get_bind()):
        # 生成列必须显式带 COLLATE：不写就继承表默认的 utf8mb4_0900_ai_ci，于是这条
        # 「活跃行唯一」在 MySQL 上变成大小写/重音不敏感，与 SQLite 部分索引的逐字节比较
        # 不等价（'jf-2606a' 与 'JF-2606A' 在 SQLite 共存、在 MySQL 撞 1062）。
        op.execute(
            f"ALTER TABLE `{table}` ADD COLUMN `{gen_col}` VARCHAR(512) "
            f"COLLATE {bin_collation(op.get_bind())} "
            f"GENERATED ALWAYS AS ({mysql_expr}) STORED"
        )
        op.execute(f"CREATE UNIQUE INDEX `{index_name}` ON `{table}` (`{gen_col}`)")
    else:
        op.execute(
            f'CREATE UNIQUE INDEX "{index_name}" ON "{table}" ({sqlite_columns}) '
            f"WHERE {sqlite_where}"
        )


def drop_active_unique(op, *, table: str, index_name: str, gen_col: str) -> None:
    """撤销 emit_active_unique 建的约束（含 MySQL 侧的生成列）。"""
    if is_mysql(op.get_bind()):
        op.execute(f"DROP INDEX `{index_name}` ON `{table}`")
        op.execute(f"ALTER TABLE `{table}` DROP COLUMN `{gen_col}`")
    else:
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
