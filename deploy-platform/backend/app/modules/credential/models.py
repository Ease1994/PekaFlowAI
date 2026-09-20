"""凭证模块的数据模型。"""
from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Credential(Base, TimestampMixin):
    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # None=全局凭证
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="token")  # token/ssh/password
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    iv: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
