"""代码仓库模块的数据模型。"""
from __future__ import annotations

import re

from sqlalchemy import Integer, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


def derive_alias(url: str) -> str:
    """从 Git URL 派生别名（蓝盾规范：group/project）。

    支持：https/http、ssh://、git@host:、裸路径。
    """
    if not url:
        return ""
    u = url.strip()
    m = re.match(r"https?://[^/]+/(.+?)(?:\.git)?/?$", u)
    if m:
        return m.group(1)
    m = re.match(r"(?:ssh://[^@/]+@[^/:]+(?::\d+)?/|[\w-]+@[^:]+:)(.+?)(?:\.git)?/?$", u)
    if m:
        return m.group(1)
    return ""


class Repository(Base, TimestampMixin):
    __tablename__ = "repository"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(256), default="")  # 从 URL 自动派生的 group/project
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="gitlab")  # gitlab/github
    default_branch: Mapped[str] = mapped_column(String(128), default="master")
    credential_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


@event.listens_for(Repository, "before_insert")
def _auto_derive_alias(mapper, connection, target):
    """插入前自动从 url 派生 alias（蓝盾规范）。"""
    if not target.alias and target.url:
        target.alias = derive_alias(target.url)


@event.listens_for(Repository, "before_update")
def _auto_derive_alias_on_update(mapper, connection, target):
    """更新前若 alias 为空且 url 变化，重新派生。"""
    if not target.alias and target.url:
        target.alias = derive_alias(target.url)
