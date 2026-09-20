"""认证与权限模块的数据模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    wecom_userid: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 企微 userid 绑定
    source: Mapped[str] = mapped_column(String(16), default="local")  # local / ldap / wecom
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / disabled
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 双因子：enrolled 表示已经扫过码并验证过一次。pending 只在首次绑码那几分钟有值。
    totp_enrolled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str] = mapped_column(String(256), default="")
    totp_pending_secret: Mapped[str] = mapped_column(String(256), default="")
    # 本地账号忘记密码：只存哈希，邮件里的明文 token 用过即废。
    password_reset_hash: Mapped[str] = mapped_column(String(64), default="")
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserMenuDeny(Base):
    """某个用户被取消的资源与工具菜单。

    工作台、系统管理不走这张表。资源菜单默认每个登录用户都有，管理员在菜单管理里关掉时才写一行。
    没有记录 = 该用户仍能看见对应菜单。同一用户同一菜单只保留一行。
    """

    __tablename__ = "user_menu_deny"
    __table_args__ = (UniqueConstraint("user_id", "menu_key", name="uq_user_menu_deny"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 被取消菜单的用户
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 菜单 key，与 MENU_ITEMS.key 一致，例如 artifacts、ai
    menu_key: Mapped[str] = mapped_column(String(64), nullable=False)


class Permission(Base):
    """细粒度 RBAC 授权项 = 主体 × 资源范围 × 操作。"""

    __tablename__ = "permission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(32), default=None)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)  # project/group/pipeline/node
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # create/delete/update/read/execute/approve/*
    effect: Mapped[str] = mapped_column(String(16), default="allow")  # allow/deny


class Role(Base, TimestampMixin):
    """项目级角色（角色跟项目绑定，同一角色名在不同项目下权限可不同）。

    permissions 为 JSON 字符串，格式：{"pipeline":["read","execute"], "repository":["read"], ...}
    资源类型：project/group/pipeline/repository/credential
    操作：read/create/update/delete/execute/approve（"*" 表示全部）
    """

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[str] = mapped_column(Text, default="{}")  # JSON


class UserRole(Base):
    """用户在项目下的角色分配（一个用户可在多个项目下拥有多个角色）。"""

    __tablename__ = "user_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class PermissionApplication(Base, TimestampMixin):
    """权限申请。审批记录永久落库，不删除。

    一张单只申请一种东西：资源权限（整项目 / 环境分组 / 流水线）或项目角色。
    资源申请通过后按 granted_actions 写 Permission；角色申请通过后写 UserRole。
    """

    __tablename__ = "permission_application"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    applicant_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    apply_type: Mapped[str] = mapped_column(String(32), default="execute")
    granted_actions: Mapped[str] = mapped_column(String(64), default="")  # 资源申请通过后实际写入，如 read,execute
    project_name: Mapped[str] = mapped_column(String(128), default="")
    group_name: Mapped[str] = mapped_column(String(64), default="")
    pipeline_name: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/approved/rejected/cancelled
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="web")  # web/ai/api 触发入口，操作人仍是 applicant


class ApiToken(Base, TimestampMixin):
    """用户 API Token（拥有与用户同等权限，供脚本/CI 调用 API）。

    只存 sha256 哈希，明文仅在创建时返回一次。过期的会被自动清理。
    """

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None=永久
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
