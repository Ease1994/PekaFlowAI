"""Harness 组件、不可变版本、依赖、运行实例和生命周期审计。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class HarnessComponent(Base, TimestampMixin):
    __tablename__ = "harness_component"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_harness_component_kind_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="api")
    source_ref: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="installed", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    versions: Mapped[list[HarnessVersion]] = relationship(
        "HarnessVersion",
        back_populates="component",
        cascade="all, delete-orphan",
        foreign_keys="HarnessVersion.component_id",
    )
    runtimes: Mapped[list[HarnessRuntime]] = relationship(
        "HarnessRuntime", back_populates="component", cascade="all, delete-orphan"
    )
    audits: Mapped[list[HarnessLifecycleAudit]] = relationship(
        "HarnessLifecycleAudit", back_populates="component"
    )


class HarnessVersion(Base, TimestampMixin):
    """发布后只新增不更新的组件版本。"""

    __tablename__ = "harness_version"
    __table_args__ = (
        UniqueConstraint("component_id", "version", name="uq_harness_version_component_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("harness_component.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    package_uri: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)

    component: Mapped[HarnessComponent] = relationship(
        "HarnessComponent", back_populates="versions", foreign_keys=[component_id]
    )
    dependencies: Mapped[list[HarnessDependency]] = relationship(
        "HarnessDependency",
        back_populates="version_record",
        cascade="all, delete-orphan",
        foreign_keys="HarnessDependency.version_id",
    )


class HarnessDependency(Base):
    __tablename__ = "harness_dependency"
    __table_args__ = (
        UniqueConstraint("version_id", "dependency_key", name="uq_harness_dependency_version_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("harness_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dependency_key: Mapped[str] = mapped_column(String(192), nullable=False, index=True)
    version_range: Mapped[str] = mapped_column(String(64), nullable=False, default="*")
    optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolved_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("harness_version.id", ondelete="SET NULL"), nullable=True, index=True
    )

    version_record: Mapped[HarnessVersion] = relationship(
        "HarnessVersion", back_populates="dependencies", foreign_keys=[version_id]
    )


class HarnessRuntime(Base, TimestampMixin):
    __tablename__ = "harness_runtime"
    __table_args__ = (
        UniqueConstraint("component_id", "scope_key", name="uq_harness_runtime_component_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("harness_component.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("harness_version.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scope_key: Mapped[str] = mapped_column(String(192), nullable=False, default="global")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="stopped", index=True)
    health_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    health_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    component: Mapped[HarnessComponent] = relationship(
        "HarnessComponent", back_populates="runtimes"
    )


class HarnessToolInvocation(Base, TimestampMixin):
    """工具调用观测。第三方工具出问题时要能立刻说清是谁、跑了多久、错在哪。"""

    __tablename__ = "harness_tool_invocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    component_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="builtin")
    isolation: Mapped[str] = mapped_column(String(16), nullable=False, default="in-process")
    call_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class HarnessLifecycleAudit(Base, TimestampMixin):
    __tablename__ = "harness_lifecycle_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    component_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("harness_component.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    runtime_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    component_key: Mapped[str] = mapped_column(String(192), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(24), default="")
    to_status: Mapped[str] = mapped_column(String(24), default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="api")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")

    component: Mapped[HarnessComponent | None] = relationship(
        "HarnessComponent", back_populates="audits"
    )
