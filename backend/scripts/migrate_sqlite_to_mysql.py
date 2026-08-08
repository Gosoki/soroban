"""一次性数据迁移：把 SQLite（soroban.db）整库搬到 MySQL。

⚠️ **本脚本已被应用内「数据库」页取代**（见 README「数据库」章 / app/services/db_migrate.py），
   保留仅供命令行批处理场景。两点差异务必注意：
   1. 它**不会**切换 soroban 实际使用的后端——那由控制库里的 app_db_config 决定，只能在
      「数据库」页切（或 `python -m tools.use_local_db` 切回本地）。改 .env 的 DATABASE_URL
      **不再有任何切换效果**，那个值现在只用来定位恒为 SQLite 的控制库。
   2. 因此下面第 2 步「把 .env 指向 MySQL」只是借 alembic 的配置通道给目标库建表，
      建完记得改回去，否则控制库位置也会跟着变。

前置：
1. MySQL 已建库（utf8mb4）：CREATE DATABASE soroban CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
2. 目标库已建好 schema：先把 .env 的 DATABASE_URL 指向 MySQL，跑一次
   `cd backend && python -m alembic upgrade head`（会按方言建表 + 生成列）。
3. 装好驱动：pip install PyMySQL

用法（在 backend/ 下）：
    python -m scripts.migrate_sqlite_to_mysql \
        --src  sqlite:///./soroban.db \
        --dst  "mysql+pymysql://soroban:pass@127.0.0.1:3306/soroban?charset=utf8mb4"
    # --dst 省略时用 .env 里的 DATABASE_URL

要点：
- 按外键依赖顺序逐表搬运，保留自增主键（MySQL 接受显式 id，AUTO_INCREMENT 自动跟进）。
- 只写模型声明的真实列；MySQL 侧的「活跃唯一」生成列不在模型里，插入时由 MySQL 自算，绝不手写。
- 幂等保护：目标表若已有数据则中止（避免重复导入）；确认要重跑请先清空目标库。
"""
from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, SQLModel, create_engine, select

from app import models  # noqa: F401  注册全部表
from app.config import settings
from app.models import (
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

# 按外键依赖排序：被引用的表在前。
MIGRATION_ORDER = [
    User,           # 被 ledger.payer_id 引用
    ShipmentOrder,  # 被 orders.shipment_order_id 引用
    Order,    # 被 orderitem / orderstaging 引用
    OrderItem,
    OrderStaging,
    StagingItem,
    MiscExpense,
    FxRate,
    Setting,
    ColumnLayout,
    TagOption,
    PluginConfig,
    PluginRecord,
]


def _copy_table(model, src: Session, dst: Session) -> int:
    cols = list(model.__table__.columns.keys())
    rows = src.exec(select(model)).all()
    for r in rows:
        dst.add(model(**{c: getattr(r, c) for c in cols}))
    dst.flush()          # 只下发本表、不提交：整轮搬完再一次性 commit，失败可整体回滚
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="sqlite:///./soroban.db", help="源 SQLite 连接串")
    ap.add_argument("--dst", default=settings.DATABASE_URL, help="目标 MySQL 连接串（默认取 .env）")
    args = ap.parse_args()

    if not args.dst.startswith(("mysql", "mariadb")):
        sys.exit(f"目标应为 MySQL 连接串，当前为：{args.dst}")

    src_engine = create_engine(args.src, connect_args={"check_same_thread": False})
    dst_engine = create_engine(args.dst, pool_pre_ping=True)

    # 幂等保护：目标非空则中止
    with Session(dst_engine) as dst:
        for model in MIGRATION_ORDER:
            if dst.exec(select(model)).first():
                sys.exit(f"目标表 {model.__tablename__} 已有数据，中止（如需重跑请先清空目标库）")

    total = 0
    # 单一事务：任一表失败则整体回滚，目标库保持全空，重跑时幂等保护不会因半拷贝而卡住。
    with Session(src_engine) as src, Session(dst_engine) as dst:
        for model in MIGRATION_ORDER:
            n = _copy_table(model, src, dst)
            total += n
            print(f"  {model.__tablename__:<16} {n:>6} 行")
        dst.commit()
    print(f"完成，共迁移 {total} 行。请抽查 MySQL 数据，然后在应用内「数据库」页点「切换」生效")
    print("（改 .env 的 DATABASE_URL 不会切换后端——见本文件顶部说明）")


if __name__ == "__main__":
    main()
