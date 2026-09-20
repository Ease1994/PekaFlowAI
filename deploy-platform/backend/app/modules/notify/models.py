"""站内通知（权限申请审核等，落 MySQL）。"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class InAppNotice(Base, TimestampMixin):
    """给指定用户的站内通知，不进 ES。"""

    __tablename__ = "in_app_notice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(64), default="")  # 事件名，如 release.failed
    link: Mapped[str] = mapped_column(String(256), default="")
    related_type: Mapped[str] = mapped_column(String(32), default="")
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
