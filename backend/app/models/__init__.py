"""数据模型包（按页面功能解耦到子目录）。

历史上所有表都挤在单个 models.py；现按页面/功能拆到子目录，但对外保持**同一套扁平导入**：
    from app.models import Order, ShipmentOrder, ...
仍可用——业务代码无需改动。所有表在此集中导入，保证 import app.models 即把全部表注册进
同一个 SQLModel.metadata（Alembic/关系解析都依赖这一点）。

目录结构：
    base.py         共通基类 LedgerBase / 枚举 / utcnow / compute_money
    user/           用户登录
    order/          商品订单账本（订单/订单行/暂存）
    shipment/       集运订单
    misc/           杂项支出
    fx/             汇率缓存
    config/         系统配置（设置/列布局/标签/插件）

方言差异（SQLite 部分索引 ↔ MySQL 生成列）集中在 app/db/dialect.py 翻译。
"""

from .base import (
    EXCLUDED_STATUSES,
    ORDER_STATUS_RANK,
    LedgerBase,
    ShipmentStatus,
    Source,
    StagingStatus,
    OrderStatus,
    order_status_rank,
    utcnow,
)
from .config.layout import ColumnLayout
from .config.plugin import PluginConfig
from .config.setting import Setting
from .config.tag import TagOption
from .fx.rate import FxRate
from .misc.expense import MiscExpense
from .shipment.order import ShipmentOrder
from .order.item import OrderItem
from .order.order import Order
from .order.staging import StagingItem, OrderStaging
from .user.user import User

__all__ = [
    # 基础/枚举/工具
    "LedgerBase",
    "Source",
    "OrderStatus",
    "ShipmentStatus",
    "StagingStatus",
    "EXCLUDED_STATUSES",
    "ORDER_STATUS_RANK",
    "order_status_rank",
    "utcnow",
    # 表
    "User",
    "Order",
    "OrderItem",
    "OrderStaging",
    "StagingItem",
    "ShipmentOrder",
    "MiscExpense",
    "FxRate",
    "Setting",
    "ColumnLayout",
    "TagOption",
    "PluginConfig",
]
