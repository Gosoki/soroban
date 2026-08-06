"""标签选项（列头可管理的下拉集：如淘宝账号、收货人）。"""

from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from ...db.dialect import BinStr


class TagOption(SQLModel, table=True):
    __table_args__ = (
        Index("ix_tagoption_field_value", "field", "value", unique=True),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # 唯一索引 (field, value) 的两根柱子 → 必须逐字节比较，否则 MySQL 的 _ci 会把
    # 'Alice'/'alice'、'ヤマダ'/'やまだ' 判成重复（SQLite 上它们合法共存）。
    field: str = Field(max_length=32, index=True, sa_type=BinStr(32))   # platform_account / recipient
    value: str = Field(max_length=128, sa_type=BinStr(128))
    color: Optional[int] = Field(default=None)   # 调色盘序号（0..N-1），建标签时分配、之后不变（稳定不撞色）
