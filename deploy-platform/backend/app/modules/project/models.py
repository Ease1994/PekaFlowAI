"""项目与分组模块的数据模型。"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")
    # 项目经理功能总开关。默认关：没人用就不会多出一道确认。
    pm_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class Group(Base, TimestampMixin):
    """环境分组（避开 SQL 关键字 group，表名为 grp）。"""

    __tablename__ = "grp"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(16), default="test")  # prod/test/dev/staging
    description: Mapped[str] = mapped_column(Text, default="")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_emergency_bypass: Mapped[bool] = mapped_column(Boolean, default=False)
    change_window: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 或人话窗口，如 工作日 22:00-23:00
    # 可选的业务确认闸。默认关：生产审批仍只走现有 approve。
    # 只有项目 pm_enabled 且这里打开、且已经指定了项目经理时，才会多等一步确认。
    pm_approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")
