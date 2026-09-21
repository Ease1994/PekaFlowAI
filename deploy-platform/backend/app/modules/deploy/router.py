"""发布计划路由。

计划可以挂当前用户可见的任意流水线。Windows 增量必须带文件清单，其它类型可以空着。
触发走现有 create_release，不另开一套调度。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_user,
    require_project_visible,
    require_pipeline_visible,
    visible_project_ids,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.deploy import service
from app.modules.deploy.models import DeployRequest

router = APIRouter(tags=["发布提交"])

STATUS_LABELS = {
    "draft": "草稿",
    "submitted": "待发布",
    "releasing": "发布中",
    "released": "已发布",
    "release_failed": "发布失败",
    "rejected": "已驳回",
    "closed": "已关闭",
}

# 发布中时，单子上显示流水线到底走到哪一步了
RELEASE_STAGE_LABELS = {
    "pending": "等待审批",
    "queued": "排队中",
    "assigned": "已派发",
    "running": "执行中",
    "success": "执行成功",
    "failed": "执行失败",
    "rejected": "审批驳回",
    "cancelled": "已取消",
    "rolling_back": "回滚中",
    "rolled_back": "已回滚",
}


def _to_dict(
    db: Session, req: DeployRequest, current: CurrentUser, *, brief: bool = False
) -> dict:
    from app.modules.auth.models import User
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.project.models import Project

    # 发布是异步的，读之前先按关联 release 对一次账，避免一直显示「发布中」
    req = service.sync_release_status(db, req)

    project = db.get(Project, req.project_id)
    pipeline = db.get(Pipeline, req.pipeline_id) if req.pipeline_id else None
    uses_manifest = bool(pipeline) and (
        bool(getattr(pipeline, "uses_deploy_manifest", False))
        or service.consumes_deploy_manifest(pipeline)
    )
    creator = db.get(User, req.created_by) if req.created_by else None
    can_release = bool(
        req.pipeline_id
        and check_permission(db, current, "pipeline", req.pipeline_id, "execute")
    )
    can_edit = req.created_by == current.id or current.is_admin or can_release
    release = db.get(Release, req.release_id) if req.release_id else None
    manifest_count = len([ln for ln in (req.manifest or "").split("\n") if ln.strip()])
    return {
        "id": req.id,
        "project_id": req.project_id,
        "project_name": project.name if project else "",
        "pipeline_id": req.pipeline_id,
        "pipeline_name": pipeline.name if pipeline else "",
        "uses_deploy_manifest": uses_manifest,
        "title": req.title,
        "repo": req.repo,
        "source_ref": req.source_ref,
        # 清单动辄上千行，列表里几百张单子每张都带一份就是几十 MB 的响应。
        # 列表只要个条数，正文留给详情接口
        "changelog": "" if brief else req.changelog,
        "manifest": "" if brief else req.manifest,
        "manifest_count": manifest_count,
        "status": req.status,
        "status_label": STATUS_LABELS.get(req.status, req.status),
        "release_id": req.release_id,
        "release_status": release.status if release else None,
        "release_status_label": (
            RELEASE_STAGE_LABELS.get(release.status, release.status) if release else None
        ),
        "release_build_number": (release.build_number or release.id) if release else None,
        "release_version": release.version if release else None,
        "release_started_at": (
            release.started_at.isoformat() if release and release.started_at else None
        ),
        "release_finished_at": (
            release.finished_at.isoformat() if release and release.finished_at else None
        ),
        "reject_reason": req.reject_reason,
        "created_by": req.created_by,
        "created_by_name": creator.username if creator else "",
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "updated_at": req.updated_at.isoformat() if req.updated_at else None,
        "can_release": can_release,
        "can_edit": can_edit,
        "can_delete": can_edit and req.status != "releasing",
        "business_summary": getattr(req, "business_summary", "") or "",
        "impact_scope": getattr(req, "impact_scope", "") or "",
        "iteration_tag": getattr(req, "iteration_tag", "") or "",
        "planned_window": getattr(req, "planned_window", "") or "",
        "audience": getattr(req, "audience", "") or "",
        "need_user_notice": bool(getattr(req, "need_user_notice", False)),
    }


def _require_editable(db: Session, req: DeployRequest, current: CurrentUser) -> None:
    """提交人自己能改，有该流水线执行权限的（发布人员）也能改。"""
    if current.is_admin or req.created_by == current.id:
        return
    if req.pipeline_id and check_permission(
        db, current, "pipeline", req.pipeline_id, "execute"
    ):
        return
    from app.modules.pm.service import is_project_pm

    if is_project_pm(db, current.id, req.project_id):
        return
    raise BizException.forbidden("只有提交人或该流水线的发布人员能操作这张单子")


@router.get("/deploy-requests", summary="发布提交单列表")
def list_deploy_requests(
    project_id: int | None = None,
    status: str = "",
    mine: bool = False,
    limit: int = Query(200, ge=1, le=1000, description="最多返回多少条（按最新排序）"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if project_id:
        require_project_visible(db, current, project_id)
    # 可见性放进 SQL。原先是先把全表捞出来再在 Python 里 filter，
    # 只能看一个项目的人也要先把别人的单子全读一遍
    allowed = None if project_id else visible_project_ids(db, current)
    items = service.list_requests(
        db,
        project_id=project_id,
        status=status,
        creator_id=current.id if mine else None,
        allowed_project_ids=allowed,
        limit=limit,
    )
    return R.ok([_to_dict(db, r, current, brief=True) for r in items])


@router.get("/deploy-requests/{request_id}", summary="发布提交单详情")
def get_deploy_request(
    request_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    return R.ok(_to_dict(db, req, current))


def _bind_pipeline(db: Session, current: CurrentUser, pipeline_id) -> None:
    """计划可以挂当前用户可见的任意流水线，不限于增量包。"""
    if not pipeline_id:
        return
    require_pipeline_visible(db, current, int(pipeline_id))


@router.post("/deploy-requests", summary="提交发布单")
def create_deploy_request(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    project_id = int(body.get("project_id") or 0)
    if not project_id:
        raise BizException.bad_request("请选择项目")
    require_project_visible(db, current, project_id)
    _bind_pipeline(db, current, body.get("pipeline_id"))
    req = service.create_request(
        db,
        project_id=project_id,
        pipeline_id=body.get("pipeline_id") or None,
        title=body.get("title", ""),
        repo=body.get("repo", ""),
        source_ref=body.get("source_ref", ""),
        changelog=body.get("changelog", ""),
        manifest=body.get("manifest", ""),
        status=body.get("status", "submitted"),
        operator_id=current.id,
        extra=body,
    )
    return R.ok(_to_dict(db, req, current), message="已提交")


@router.put("/deploy-requests/{request_id}", summary="修改发布提交单")
def update_deploy_request(
    request_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    _require_editable(db, req, current)
    if "pipeline_id" in body:
        _bind_pipeline(db, current, body.get("pipeline_id"))
    req = service.update_request(db, request_id, body)
    return R.ok(_to_dict(db, req, current), message="已保存")


@router.post("/deploy-requests/{request_id}/reject", summary="驳回发布提交单")
def reject_deploy_request(
    request_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    if not req.pipeline_id or not check_permission(
        db, current, "pipeline", req.pipeline_id, "execute"
    ):
        raise BizException.forbidden("只有该流水线的发布人员能驳回")
    req = service.reject_request(db, request_id, body.get("reason", ""))
    return R.ok(_to_dict(db, req, current), message="已驳回")


@router.post("/deploy-requests/{request_id}/reopen", summary="重新提交被驳回的单子")
def reopen_deploy_request(
    request_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    _require_editable(db, req, current)
    req = service.reopen_request(db, request_id)
    return R.ok(_to_dict(db, req, current), message="已重新提交")


@router.delete("/deploy-requests/{request_id}", summary="删除发布计划")
def delete_deploy_request(
    request_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    _require_editable(db, req, current)
    service.delete_request(db, request_id)
    return R.ok({"ok": True}, message="已删除")


@router.post("/deploy-requests/{request_id}/release", summary="按提交单触发发布")
def release_deploy_request(
    request_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """把单子上的清单和更新日志作为执行参数交给流水线，走的还是标准发布流程。"""
    from app.modules.pipeline import service as pipeline_service

    req = service.get_request(db, request_id)
    require_project_visible(db, current, req.project_id)
    req = service.sync_release_status(db, req)
    if req.status not in service.RELEASABLE_STATUSES:
        raise BizException.bad_request(
            f"当前状态（{STATUS_LABELS.get(req.status, req.status)}）不能发布"
        )
    if not req.pipeline_id:
        raise BizException.bad_request("这张单子还没选发布流水线")
    if not check_permission(db, current, "pipeline", req.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{req.pipeline_id} 发起发布")

    pipe = pipeline_service.get_pipeline(db, req.pipeline_id)
    # 清单与执行弹窗同一套失败关闭：空草稿不能写成 DEPLOY_MANIFEST="" 再开跑
    params = service.run_params_for(req, pipe, body.get("run_params"))
    from app.modules.pm.service import brief_from_request

    release = pipeline_service.create_release(
        db,
        pipeline_id=req.pipeline_id,
        version=body.get("version", ""),
        strategy=body.get("strategy", "rolling"),
        trigger_by="deploy_request",
        operator_id=current.id,
        source_ref=req.source_ref or None,
        run_params=params,
        emergency_bypass=bool(body.get("emergency_bypass", False)),
        emergency_bypass_reason=str(body.get("emergency_bypass_reason") or ""),
        brief=brief_from_request(req),
    )
    # 先把单子和 release 关联上，再去拉执行。反过来的话，执行一抛异常这行就跑不到，
    # 单子记不下自己发出去的是哪一条，页面上只剩一个「提交中」，
    # 而那条已经失败的 release 谁也找不着——单子状态本来是靠 release 对账回填的
    service.mark_releasing(db, request_id, release.id)

    # queued 表示无需审批，可以直接开跑。少了这一步，release 会一直停在「排队中」，
    # 构建任务永远不会生成——页面上看着像发起成功了，实际什么都没执行
    if release.status == "queued":
        release, err = pipeline_service.try_execute_release(db, release.id)
        if err:
            return R.ok(
                {
                    "release_id": release.id,
                    "status": release.status,
                    "error": err,
                },
                message=f"已按提交单发起，但发布未能启动：{err}",
            )

    return R.ok(
        {"release_id": release.id, "status": release.status},
        message="已按提交单发起发布" if release.status != "pending" else "已发起，等待审批",
    )
