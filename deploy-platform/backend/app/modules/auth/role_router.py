"""项目级角色管理路由（角色跟项目绑定）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.auth.models import Role, User, UserRole

router = APIRouter(tags=["角色管理"])

# 角色可选权限：资源类型 → 可分配操作（前端据此渲染 checkbox）
RESOURCE_ACTIONS = {
    "project": ["read", "create", "update", "delete", "approval_exempt"],
    "group": ["read", "create", "update", "delete", "approve", "approval_exempt"],
    "pipeline": ["read", "create", "update", "delete", "execute", "approval_exempt"],
    "repository": ["read", "create", "update", "delete"],
    "credential": ["read", "create", "delete"],
}


def _role_to_dict(r: Role, db: Session) -> dict:
    try:
        permissions = json.loads(r.permissions or "{}")
    except Exception:
        permissions = {}
    users = db.scalars(
        select(User).join(UserRole, UserRole.user_id == User.id).where(UserRole.role_id == r.id)
    ).all()
    return {
        "id": r.id,
        "project_id": r.project_id,
        "name": r.name,
        "description": r.description,
        "permissions": permissions,
        "users": [{"id": u.id, "username": u.username, "display_name": u.display_name} for u in users],
    }


def _require_project_manage(db: Session, user: CurrentUser, project_id: int) -> None:
    """角色管理需要该项目的 update 权限（或管理员）。"""
    if not check_permission(db, user, "project", project_id, "update"):
        raise BizException.forbidden(f"无权限：管理项目 #{project_id} 的角色")


@router.get("/roles", summary="项目角色列表")
def list_roles(
    project_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    roles = db.scalars(
        select(Role).where(Role.project_id == project_id).order_by(Role.id)
    ).all()
    return R.ok([_role_to_dict(r, db) for r in roles])


@router.get("/roles/resource-actions", summary="角色可选权限清单")
def resource_actions():
    return R.ok(RESOURCE_ACTIONS)


@router.post("/roles", summary="创建角色")
def create_role(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    project_id = body["project_id"]
    _require_project_manage(db, current, project_id)
    r = Role(
        project_id=project_id,
        name=body["name"],
        description=body.get("description", ""),
        permissions=json.dumps(body.get("permissions", {}), ensure_ascii=False),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return R.ok(_role_to_dict(r, db))


@router.put("/roles/{role_id}", summary="更新角色（含权限）")
def update_role(
    role_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Role, role_id)
    if r is None:
        raise BizException.not_found("角色")
    _require_project_manage(db, current, r.project_id)
    if "name" in body and body["name"]:
        r.name = body["name"]
    if "description" in body:
        r.description = body["description"]
    if "permissions" in body:
        r.permissions = json.dumps(body["permissions"], ensure_ascii=False)
    db.commit()
    db.refresh(r)
    return R.ok(_role_to_dict(r, db))


@router.delete("/roles/{role_id}", summary="删除角色")
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Role, role_id)
    if r is None:
        raise BizException.not_found("角色")
    _require_project_manage(db, current, r.project_id)
    # 删除角色时清理关联的 UserRole
    for ur in db.query(UserRole).filter(UserRole.role_id == role_id).all():
        db.delete(ur)
    db.delete(r)
    db.commit()
    return R.ok()


@router.post("/roles/{role_id}/users", summary="给角色批量分配用户")
def assign_role_users(
    role_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Role, role_id)
    if r is None:
        raise BizException.not_found("角色")
    _require_project_manage(db, current, r.project_id)
    user_ids = [int(i) for i in body.get("user_ids", [])]
    existing = {
        ur.user_id
        for ur in db.query(UserRole).filter(UserRole.role_id == role_id).all()
    }
    created = 0
    for uid in user_ids:
        if uid not in existing:
            db.add(UserRole(user_id=uid, role_id=role_id))
            existing.add(uid)
            created += 1
    db.commit()
    return R.ok({"created": created})


@router.delete("/roles/{role_id}/users/{user_id}", summary="移除用户角色")
def remove_role_user(
    role_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Role, role_id)
    if r is None:
        raise BizException.not_found("角色")
    _require_project_manage(db, current, r.project_id)
    ur = db.query(UserRole).filter(
        UserRole.role_id == role_id, UserRole.user_id == user_id
    ).first()
    if ur:
        db.delete(ur)
        db.commit()
    return R.ok()


@router.get("/users/{user_id}/roles", summary="查询用户的角色（跨项目）")
def get_user_roles(
    user_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    roles = (
        db.query(Role)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user_id)
        .order_by(Role.project_id)
        .all()
    )
    return R.ok([_role_to_dict(r, db) for r in roles])
