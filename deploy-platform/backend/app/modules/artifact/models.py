"""制品模块的数据模型。"""
from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 产出这个制品的那次发布；部署步骤靠它找到本次要发的包
    release_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(32), default="jar")  # jar/war/docker/zip/iis-package
    version: Mapped[str] = mapped_column(String(64), default="")
    git_commit: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str] = mapped_column(String(512), default="")  # 落盘路径（后续可换对象存储）
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 注意：`metadata` 是 SQLAlchemy 保留名，故属性命名为 extra_meta，列名仍为 metadata
    extra_meta: Mapped[str] = mapped_column("metadata", Text, default="{}")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
