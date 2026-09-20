"""项目经理相关表。不改现有 approval / release 的语义。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ProjectMember(Base, TimestampMixin):
    """项目里的业务角色。目前只用 role=pm。

    和 RBAC 的 Permission 分开：项目经理有查看 + 业务确认，没有执行、没有往节点写盘。
    指定时会补一条 project/read，否则工作台对他是空的。
    """

    __tablename__ = "project_member"
    __table_args__ = (UniqueConstraint("project_id", "user_id", "role", name="uq_project_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="pm")


class PmDecision(Base, TimestampMixin):
    """项目经理对某次发布的业务确认，和技术审批 Approval 是两张单。

    只有项目开了「项目经理参与」且该分组开了「需要项目经理确认」才会建行。
    默认不建，所以默认不挡发布。
    """

    __tablename__ = "pm_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reviewer_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected/cancelled
    comment: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
