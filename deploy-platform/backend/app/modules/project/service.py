"""项目删除：有流水线就不能删，空项目连默认环境分组一起拿掉。"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.auth.models import Permission, Role, UserRole
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project


def delete_project(db: Session, project_id: int) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise BizException.not_found("项目")

    names = list(
        db.scalars(
            select(Pipeline.name).where(Pipeline.project_id == project_id).order_by(Pipeline.id).limit(5)
        ).all()
    )
    if names:
        total = int(
            db.scalar(select(func.count(Pipeline.id)).where(Pipeline.project_id == project_id)) or 0
        )
        shown = "、".join(f"「{name}」" for name in names)
        extra = f" 等共 {total} 条" if total > len(names) else ""
        raise BizException.bad_request(
            f"项目下还有流水线{extra}：{shown}，不能删除。"
            "请先到项目里把流水线删进回收站并彻底删除（回收站里的也算）。"
        )

    _purge_project_shell(db, project_id)
    db.delete(project)
    db.commit()


def _purge_project_shell(db: Session, project_id: int) -> None:
    """流水线已经清空后，把项目自带的空壳一并清掉，避免留下孤儿分组和授权。"""
    group_ids = list(db.scalars(select(Group.id).where(Group.project_id == project_id)).all())
    role_ids = list(db.scalars(select(Role.id).where(Role.project_id == project_id)).all())
    if role_ids:
        db.execute(delete(UserRole).where(UserRole.role_id.in_(role_ids)))
        db.execute(delete(Role).where(Role.id.in_(role_ids)))
    db.execute(
        delete(Permission).where(
            Permission.resource_type == "project",
            Permission.resource_id == project_id,
        )
    )
    if group_ids:
        db.execute(
            delete(Permission).where(
                Permission.resource_type == "group",
                Permission.resource_id.in_(group_ids),
            )
        )
        db.execute(delete(Group).where(Group.id.in_(group_ids)))
    from app.modules.pm.models import ProjectMember

    db.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id))
