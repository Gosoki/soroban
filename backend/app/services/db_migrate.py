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
from urllib.parse import quote_plus

import pymysql
from sqlmodel import Session, delete, select

from ..database import build_engine
from ..models import (
    ColumnLayout,
    FxRate,
    MiscExpense,
    OrderItem,
    PluginConfig,
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
    MiscExpense, FxRate, Setting, ColumnLayout, TagOption, PluginConfig,
]

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


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
    from sqlalchemy import func as sa_func

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
    labels = {"orders": "商品订单", "orderstaging": "暂存订单", "shipmentorder": "集运订单",
              "miscexpense": "杂项支出", "orderitem": "订单物品", "stagingitem": "暂存物品",
              "user": "用户", "fxrate": "汇率", "tagoption": "标签", "pluginconfig": "插件配置"}
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
            dst.flush()
            counts[model.__tablename__] = len(rows)
            log.info("迁移 %s：%d 行", model.__tablename__, len(rows))
        dst.commit()                      # 全部成功才一次性提交
    return counts
