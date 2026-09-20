"""权限申请路由（独立模块，记录落 MySQL）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.access import service
from app.modules.audit.context import current_source

router = APIRouter(tags=["权限申请"])


@router.get("/access/me", summary="当前用户已有的权限和角色")
def my_access(db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    return R.ok(service.my_entitlements(db, current))


@router.get("/access/catalog", summary="申请用项目/分组/流水线/角色目录")
def access_catalog(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(service.catalog(db))


@router.post("/access/applications", summary="申请资源权限或项目角色")
def create_application(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    pipeline_id = int(body.get("pipeline_id") or 0)
    project_id = int(body.get("project_id") or 0)
    group_id = int(body.get("group_id") or 0)
    role_id = int(body.get("role_id") or 0)
    env = str(body.get("env") or "").strip()
    project_keyword = str(body.get("project") or body.get("keyword") or "").strip()
    role_name = str(body.get("role") or body.get("role_name") or "").strip()
    reason = str(body.get("reason") or "")
    actions = body.get("actions")
    if current_source() not in {"ai", "web", "api"}:
        raise BizException.bad_request("不支持的申请入口")
    if role_id or role_name:
        return R.ok(
            service.apply_role(
                db,
                current,
                role_id=role_id or None,
                project_id=project_id or None,
                keyword=project_keyword,
                role_name=role_name,
                reason=reason,
            )
        )
    if group_id or (env and (project_id or project_keyword)):
        if not project_id and project_keyword:
            proj = service.resolve_project(db, keyword=project_keyword)
            project_id = proj.id
        return R.ok(
            service.apply_group_execute(
                db,
                current,
                group_id=group_id or None,
                project_id=project_id or None,
                env=env,
                reason=reason,
                actions=actions,
            )
        )
    if project_id or (project_keyword and not pipeline_id):
        return R.ok(
            service.apply_project_execute(
                db,
                current,
                project_id=project_id or None,
                keyword=project_keyword,
                reason=reason,
                actions=actions,
            )
        )
    if not pipeline_id:
        raise BizException.bad_request("缺少 pipeline_id 或 project_id")
    return R.ok(service.apply_execute(db, current, pipeline_id, reason, actions=actions))


@router.get("/access/applications", summary="权限申请列表")
def list_applications(
    scope: str = Query(default="mine", description="mine / pending / all"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(service.list_applications(db, current, scope))


@router.get("/access/applications/{application_id}", summary="权限申请详情")
def get_application(
    application_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(service.get_application(db, current, application_id))


@router.patch("/access/applications/{application_id}", summary="修改待审批申请（拟授权限）")
def patch_application(
    application_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    body = body or {}
    actions = body.get("actions")
    reason = body.get("reason")
    return R.ok(
        service.update_pending(
            db,
            current,
            application_id,
            actions=actions,
            reason=None if reason is None else str(reason),
        )
    )


@router.post("/access/applications/{application_id}/approve", summary="通过权限申请")
def approve_application(
    application_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    comment = str((body or {}).get("comment") or "")
    actions = (body or {}).get("actions")
    return R.ok(service.review(db, current, application_id, True, comment, actions=actions))


@router.post("/access/applications/{application_id}/reject", summary="驳回权限申请")
def reject_application(
    application_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    comment = str((body or {}).get("comment") or "")
    return R.ok(service.review(db, current, application_id, False, comment))


@router.post("/access/applications/{application_id}/cancel", summary="撤销待审批申请")
def cancel_application(
    application_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(service.cancel(db, current, application_id))
