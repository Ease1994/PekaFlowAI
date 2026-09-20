"""审批模块的数据模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Approval(Base, TimestampMixin):
    __tablename__ = "approval"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    approver_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected/cancelled
    comment: Mapped[str] = mapped_column(Text, default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
