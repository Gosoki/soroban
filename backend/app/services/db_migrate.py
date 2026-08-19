"""数据库迁移服务（供「数据库」页调用；SQLite↔MySQL 双向）。

流程：测试连通 →（MySQL 目标才）建库(utf8mb4) → 目标建 schema(Alembic) → 整表覆盖拷数据。
切换（写配置 + 热切换）由 dbadmin 路由完成，且**不清空源库**——故切换可逆、可反复迁移。

拷贝按外键依赖顺序、保留自增主键；只写模型声明的真实列——MySQL 的「活跃唯一」生成列不在
模型里，插入时由 MySQL 自算，绝不手写。
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from urllib.parse import quote_plus

import pymysql
from sqlalchemy import BigInteger, Integer, SmallInteger, or_
from sqlalchemy import func as sa_func
from sqlmodel import Session, delete, select

from ..models import (
    ColumnLayout,
    FxRate,
    MiscExpense,
    OrderItem,
    PluginConfig,
    PluginRecord,
    Setting,
    ShipmentOrder,
    StagingItem,
    TagOption,
    Order,
    OrderStaging,
    User,
)

log = logging.getLogger("soroban.db.migrate")

# 按外键依赖排序：被引用的表在前（拷贝用正序，清空用逆序）。
MIGRATION_ORDER = [
    User, ShipmentOrder, Order, OrderItem, OrderStaging, StagingItem,
    MiscExpense, FxRate, Setting, ColumnLayout, TagOption, PluginConfig, PluginRecord,
]

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class CopyFailed(RuntimeError):
    """整表拷贝在某张表上失败。独立成一个类型，是为了跟 barrier.hold 抛的 RuntimeError
    （「已有另一项维护操作在进行」→ 409）区分开——两者语义完全不同，混在一起会让
    「拷贝失败」被当成「有人在同时迁移」报给用户。"""

# 表名 → 界面上的说法。指纹差异与迁移体检都要向用户报表名，共用一份免得两处漂移。
_TABLE_LABELS = {
    "orders": "商品订单", "orderstaging": "暂存订单", "shipmentorder": "集运订单",
    "miscexpense": "杂项支出", "orderitem": "订单物品", "stagingitem": "暂存物品",
    "user": "用户", "fxrate": "汇率", "tagoption": "标签", "pluginconfig": "插件配置",
    "setting": "系统设置", "columnlayout": "列布局", "pluginrecord": "插件数据",
}


def build_mysql_url(host: str, port: int, user: str, password: str, database: str) -> str:
    """密码/用户名做 URL 编码（含 @ : / 等特殊字符）；强制 utf8mb4。"""
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{int(port)}/{database}?charset=utf8mb4"
    )


def test_connection(host: str, port: int, user: str, password: str, database=None):
    """(ok, version_or_error)。database 传 None 时只连到服务器（不选库）。"""
    try:
        conn = pymysql.connect(
            host=host, port=int(port), user=user, password=password,
            database=database, connect_timeout=8,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            ver = cur.fetchone()[0]
        conn.close()
        return True, ver
    except Exception as e:                                  # noqa: BLE001
        return False, str(e)


MIN_MYSQL = (8, 0)


def unsupported_server(version: str) -> str:
    """这台服务端能不能用。能用返回空串，不能用返回一句人话原因。

    **为什么必须在门口拦**：不拦的话，迁移会跑到一半才炸，而 DDL 在 MySQL 上是
    **隐式提交**的——前面几条已经落地、后面的没跑，库停在一个半升级态，
    既不是旧版也不是新版，`alembic upgrade` 再跑一次也未必能自愈。
    「一开始就拒绝」和「炸在中途」对用户的代价差着一个数量级。

    两条判据都来自真实的建表语句，不是猜的：
      · MariaDB 没有 `utf8mb4_0900_bin`（那是 MySQL 8.0 引入的 NO PAD 规则）。
        `dialect.BinStr` 硬写了它 → `ERROR 1273: Unknown collation`。
      · MySQL 5.7 更早就过不去：`ALTER TABLE … RENAME COLUMN` 是 8.0 才有的语法，
        迁移链里用到它。所以「只修 BinStr」既买不到 5.7、也买不到 MariaDB。

    README 曾承诺「MariaDB 会自动回退」。回退逻辑确实在 `bin_collation()` 里，
    但 `BinStr` 这条路径绕过了它——承诺是假的。与其补一条从未在真机验证过的兼容分支，
    不如把话说准：需要 MySQL 8.0+。
    """
    v = (version or "").strip()
    low = v.lower()
    if "mariadb" in low:
        return (f"检测到 MariaDB（{v}）。soroban 需要 MySQL 8.0 或更新版本："
                "唯一键用的 utf8mb4_0900_bin 排序规则 MariaDB 没有，"
                "迁移会在中途失败并把库留在半升级状态。")
    m = re.match(r"(\d+)\.(\d+)", v)
    if not m:
        return ""                       # 认不出版本号就放行——不该因为格式没见过而挡住人
    if (int(m.group(1)), int(m.group(2))) < MIN_MYSQL:
        return (f"检测到 MySQL {v}。soroban 需要 8.0 或更新版本："
                "迁移用到了 8.0 才有的 RENAME COLUMN 语法与 utf8mb4_0900_bin 排序规则，"
                "在旧版上会中途失败并把库留在半升级状态。")
    return ""


def ensure_database(host: str, port: int, user: str, password: str, database: str) -> None:
    """建库（utf8mb4），已存在则跳过。库名做白名单校验，防注入。"""
    if not _DB_NAME_RE.match(database):
        raise ValueError(f"非法数据库名：{database!r}（仅允许字母/数字/下划线）")
    conn = pymysql.connect(host=host, port=int(port), user=user, password=password, connect_timeout=8)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        conn.close()


def source_fingerprint(engine) -> str:
    """源库的「变更指纹」：各业务表的行数 + 最大 updated_at，序列化成 JSON。

    用途：`migrate` 拷完记一份，`switch` 前再算一次做对比——两者不同就说明**迁移之后源库又被
    改过**，那些改动不会跟着过去，切换后就静默消失了。指纹只需要「能发现变了」，不需要精确
    到哪一行，所以行数 + 最新时间戳足够便宜也足够灵敏（增/删看行数，改看时间戳）。

    刻意不含 Setting/ColumnLayout：前者压根没在用，后者是界面偏好、丢了也无所谓，
    把它们算进来只会让「拖一下列宽」也触发变更告警。"""
    noise = {"setting", "columnlayout"}
    out: dict[str, list] = {}
    with Session(engine) as s:
        for model in MIGRATION_ORDER:
            name = model.__tablename__
            if name in noise:
                continue
            count = s.exec(select(sa_func.count()).select_from(model)).one()
            newest = None
            if "updated_at" in model.__table__.columns:
                newest = s.exec(select(sa_func.max(model.updated_at))).one()
            out[name] = [int(count), str(newest) if newest is not None else None]
    return json.dumps(out, sort_keys=True)


def describe_fingerprint_diff(before: str, after: str) -> list[str]:
    """把两份指纹的差异说成人话，如 ["订单 +3 条", "集运订单 有改动"]。相同则返回空列表。"""
    labels = _TABLE_LABELS
    try:
        a, b = json.loads(before), json.loads(after)
    except (TypeError, ValueError):
        return ["无法比对（指纹损坏）"]
    out = []
    for table in sorted(set(a) | set(b)):
        (ca, ta), (cb, tb) = a.get(table, [0, None]), b.get(table, [0, None])
        label = labels.get(table, table)
        if cb != ca:
            out.append(f"{label} {cb - ca:+d} 条")
        elif tb != ta:
            out.append(f"{label} 有改动")
    return out


def is_target_empty(engine) -> bool:
    """目标业务表是否全空（防止对已有数据的库重复导入）。"""
    with Session(engine) as s:
        for model in MIGRATION_ORDER:
            if s.exec(select(model)).first():
                return False
    return True


# MySQL 的 INT 是 4 字节有符号；SQLite 没有这个限制，历史脏行能静默躺在里面。
_INT32_MIN, _INT32_MAX = -2147483648, 2147483647


def target_rows(engine) -> dict[str, int]:
    """目标库各业务表**现有**行数（只回非空的，键是中文表名）。

    给「迁移会删掉什么」这句话提供事实。`is_target_empty` 只回是/否，
    而用户要决定的是「值不值得覆盖」——那需要知道对面有多少东西。
    """
    out: dict[str, int] = {}
    with Session(engine) as s:
        for model in MIGRATION_ORDER:
            n = int(s.exec(select(sa_func.count()).select_from(model)).one() or 0)
            if n:
                out[_TABLE_LABELS.get(model.__tablename__, model.__tablename__)] = n
    return out


def preflight(src_engine, dst_engine) -> list[str]:
    """拷贝前体检：找出**源库里合法、目标库会拒收**的行，说人话报给用户。

    为什么需要：SQLite 是弱约束——VARCHAR(n) 只是亲和性、不检查长度，NUMERIC(12,2) 照单全收
    任意大的数；MySQL 在 STRICT_TRANS_TABLES 下则分别抛 1406 Data too long 与
    1264 Out of range（实测 MySQL 9.7 默认 sql_mode 就含 STRICT_TRANS_TABLES）。
    而 `replace_data` 是**单事务**，任何一行踩雷都会让整次迁移回滚，用户只看到一句
    「数据拷贝失败：<SQLAlchemy 原始串>」，既不知道是哪张表哪一行，也不知道该怎么办。

    入口层的校验（schemas 的长度卡口、models.guard_cny 的金额卡口）只保证**今后**写不进脏行，
    对早已躺在库里的历史数据无能为力——那正是这里要捞出来的。

    注意：排序规则导致的唯一键冲突**不在这里检查**。迁移 f2a3b4c5d6e7 已把键列改成
    utf8mb4_0900_bin，MySQL 侧的唯一性与 SQLite 的 BINARY 逐字节等价，那类冲突不再可能发生。

    返回人话描述的问题列表；空列表 = 可以拷。目标不是 MySQL 时直接返回空（SQLite 什么都收）。
    """
    if dst_engine.dialect.name not in ("mysql", "mariadb"):
        return []

    src_is_sqlite = src_engine.dialect.name == "sqlite"
    # SQLite 的 length() 按字符数；MySQL 的 LENGTH() 按字节，得用 CHAR_LENGTH() 才可比
    # ——VARCHAR(n) 的 n 数的是字符。用错函数会把纯中文的合法值误报成超长。
    strlen = sa_func.length if src_is_sqlite else sa_func.char_length

    problems: list[str] = []
    with Session(src_engine) as s:
        for model in MIGRATION_ORDER:
            label = _TABLE_LABELS.get(model.__tablename__, model.__tablename__)
            # 主键不一定叫 id（ColumnLayout 的主键是 table_name），取表自己声明的那个
            pk = list(model.__table__.primary_key.columns)[0]
            for col in model.__table__.columns:
                limit = getattr(col.type, "length", None)
                if limit:                                   # 定长字符串列
                    for key, n, val in s.exec(
                        select(pk, strlen(col), col).where(strlen(col) > limit).limit(3)
                    ).all():
                        problems.append(
                            f"{label} #{key} 的「{col.name}」有 {n} 个字符，"
                            f"超过上限 {limit}：{str(val)[:40]}…"
                        )
                    continue
                # **整数列**：SQLite 是弱类型，`jpy_override` 之类能静默存下超过 2^31 的值；
                # MySQL 的 INT 是 4 字节，插入时抛 1264 Out of range。
                # 入口层的 `_bounded_jpy`（schemas.py）只保证**今后**写不进越界值，
                # 对那道卡口存在**之前**就躺在库里的历史行无能为力——那正是 preflight 的职责。
                # （`config.py` 里那条「SQLite 与 MySQL 的整数范围发散」的说明就是指这个。）
                if isinstance(col.type, Integer) and not isinstance(col.type, (BigInteger, SmallInteger)):
                    for key, val in s.exec(
                        select(pk, col).where(or_(col > _INT32_MAX, col < _INT32_MIN)).limit(3)
                    ).all():
                        problems.append(
                            f"{label} #{key} 的「{col.name}」= {val}，"
                            f"超出目标库 INT 能存的范围（{_INT32_MIN} ~ {_INT32_MAX}）"
                        )
                    continue
                precision = getattr(col.type, "precision", None)
                scale = getattr(col.type, "scale", None)
                if precision is not None and scale is not None:   # DECIMAL 列
                    ceiling = Decimal(10) ** (precision - scale)
                    for key, val in s.exec(
                        select(pk, col).where(sa_func.abs(col) >= ceiling).limit(3)
                    ).all():
                        problems.append(
                            f"{label} #{key} 的「{col.name}」= {val}，"
                            f"超出 DECIMAL({precision},{scale}) 能存的范围"
                        )
    return problems


def replace_data(src_engine, dst_engine) -> dict:
    """把源库业务数据**整体覆盖**到目标库（支持任意方向：SQLite↔MySQL）。

    先按逆外键序清空目标各表，再按正序整表拷贝——故切换可逆、可反复迁移。保留自增主键；
    只写模型声明的真实列（MySQL 的「活跃唯一」生成列不在模型里，插入时由 MySQL 自算）。
    调用方须保证 dst 不是当前正在使用的库（否则会清空线上数据）。返回每表行数。"""
    counts: dict[str, int] = {}
    with Session(src_engine) as src, Session(dst_engine) as dst:
        # 单一事务：清空 + 整表拷贝一次性提交。中途任一表失败则整体回滚，目标库退回迁移前状态，
        # 绝不留下「前几张表已拷、后几张表还空」的半成品——否则 is_target_empty 仍会判非空、
        # 让「切换到此库」把线上切到缺表的库上。
        # 1) 逆依赖序清空目标（子表先删，避免外键冲突）
        for model in reversed(MIGRATION_ORDER):
            dst.exec(delete(model))
        # 2) 正依赖序整表拷贝（父表先插；flush 让父行在同事务内可见，满足子表外键，但不提交）
        for model in MIGRATION_ORDER:
            cols = list(model.__table__.columns.keys())
            rows = src.exec(select(model)).all()
            for r in rows:
                dst.add(model(**{c: getattr(r, c) for c in cols}))
            # 带上表名再抛：原先失败只有一句 SQLAlchemy 原始串，用户既不知道卡在哪张表，
            # 也无从判断该去清理什么。preflight 会拦掉绝大多数，这里兜住漏网的。
            try:
                dst.flush()
            except Exception as e:                          # noqa: BLE001
                label = _TABLE_LABELS.get(model.__tablename__, model.__tablename__)
                raise CopyFailed(f"拷贝「{label}」表时失败：{e}") from e
            counts[model.__tablename__] = len(rows)
            log.info("迁移 %s：%d 行", model.__tablename__, len(rows))
        dst.commit()                      # 全部成功才一次性提交
    return counts
