"""代码仓库路由（按项目隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_user,
    visible_project_ids,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.credential.models import Credential
from app.modules.repository.models import Repository, derive_alias

router = APIRouter(tags=["代码仓库"])


def _to_dict(r: Repository, db: Session) -> dict:
    cred_name = None
    if r.credential_id:
        c = db.get(Credential, r.credential_id)
        cred_name = c.name if c else None
    return {
        "id": r.id,
        "project_id": r.project_id,
        "name": r.name,
        "alias": r.alias,
        "url": r.url,
        "provider": r.provider,
        "default_branch": r.default_branch,
        "credential_id": r.credential_id,
        "credential_name": cred_name,
    }


def _check_credential_scope(db: Session, cred_id: int | None, project_id: int) -> None:
    """校验凭证存在且属于该项目或全局。"""
    if not cred_id:
        return
    c = db.get(Credential, cred_id)
    if c is None:
        raise BizException.bad_request("凭证不存在")
    if c.project_id not in (None, project_id):
        raise BizException.forbidden("不能关联其他项目的凭证")


@router.get("/repositories", summary="仓库列表（按项目隔离）")
def list_repositories(
    project_id: int | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    visible = visible_project_ids(db, current)
    stmt = select(Repository).order_by(Repository.id.desc())
    if project_id:
        # 指定项目 → 校验可见性
        if visible is not None and project_id not in visible:
            raise BizException.forbidden(f"无权限访问项目 #{project_id}")
        stmt = stmt.where(Repository.project_id == project_id)
    elif visible is not None:
        # 未指定 → 只返回可见项目下的仓库
        if not visible:
            return R.ok([])
        stmt = stmt.where(Repository.project_id.in_(visible))
    repos = db.scalars(stmt).all()
    return R.ok([_to_dict(r, db) for r in repos])


@router.post("/repositories", summary="关联代码仓库（自动派生别名）")
def create_repository(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    project_id = body["project_id"]
    if not check_permission(db, current, "project", project_id, "create"):
        raise BizException.forbidden(f"无权限：在项目 #{project_id} 创建代码库")
    _check_credential_scope(db, body.get("credential_id"), project_id)
    url = body["url"]
    # 别名：未传则从 URL 自动派生（蓝盾规范：group/project）
    alias = body.get("alias") or derive_alias(url)
    # 名称：未传则用别名（前端表单不再录入名称）
    name = (body.get("name") or alias).strip()[:128]
    r = Repository(
        project_id=project_id,
        name=name,
        alias=alias,
        url=url,
        provider=body.get("provider", "gitlab"),
        default_branch=body.get("default_branch", "master"),
        credential_id=body.get("credential_id"),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return R.ok(_to_dict(r, db))


@router.put("/repositories/{repo_id}", summary="更新代码仓库（URL 变更时刷新别名）")
def update_repository(
    repo_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Repository, repo_id)
    if r is None:
        raise BizException.not_found("代码仓库")
    if not check_permission(db, current, "project", r.project_id, "update"):
        raise BizException.forbidden(f"无权限：更新项目 #{r.project_id} 的代码库")
    url_changed = False
    if "name" in body and body["name"]:
        r.name = body["name"]
    if "url" in body and body["url"] and body["url"] != r.url:
        r.url = body["url"]
        url_changed = True
    if "provider" in body:
        r.provider = body["provider"]
    if "default_branch" in body:
        r.default_branch = body["default_branch"]
    if "credential_id" in body:
        _check_credential_scope(db, body["credential_id"], r.project_id)
        r.credential_id = body["credential_id"]
    # 别名：URL 变更时自动重新派生；也可手动指定
    if "alias" in body and body["alias"]:
        r.alias = body["alias"]
    elif url_changed:
        r.alias = derive_alias(r.url)
    db.commit()
    db.refresh(r)
    return R.ok(_to_dict(r, db))


@router.delete("/repositories/{repo_id}", summary="删除仓库")
def delete_repository(
    repo_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Repository, repo_id)
    if r is None:
        raise BizException.not_found("代码仓库")
    if not check_permission(db, current, "project", r.project_id, "delete"):
        raise BizException.forbidden(f"无权限：删除项目 #{r.project_id} 的代码库")
    db.delete(r)
    db.commit()
    return R.ok()
