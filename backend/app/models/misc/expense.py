"""杂项支出。"""

from typing import Optional

from sqlmodel import Field

from ...db.dialect import BinStr
from ..base import LedgerBase


class MiscExpense(LedgerBase, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)                       # 名称
    # 分类。**BinStr（二进制排序规则）**：它是一根「键列」——`tags` 的改名/删除按
    # `WHERE category = value` 批量精确匹配。ai_ci 下 'EMS' 与 'ems' 相等，
    # 改一个会把另一个也改掉（见迁移 e5f6a7b8c0d1）。
    category: Optional[str] = Field(default=None, max_length=64, sa_type=BinStr(64))
