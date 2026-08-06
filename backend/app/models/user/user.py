"""用户 / 登录。"""

import datetime as dt
from typing import Optional

from sqlmodel import Field, SQLModel

from ...db.dialect import BinStr, UtcDateTime
from ..base import utcnow


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(max_length=64, unique=True, index=True, sa_type=BinStr(64))
    password_hash: str = Field(max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=128)
    is_active: bool = Field(default=True)
    created_at: dt.datetime = Field(default_factory=utcnow, sa_type=UtcDateTime())
