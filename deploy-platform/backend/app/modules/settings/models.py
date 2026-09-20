"""平台配置表。"""
from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class PlatformSetting(Base, TimestampMixin):
    """平台级配置项（key-value）。"""

    __tablename__ = "platform_setting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(String(256), default="")
