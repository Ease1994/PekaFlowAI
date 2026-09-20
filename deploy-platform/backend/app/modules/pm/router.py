"""项目经理工作台与业务确认 API。不改 /approvals 现有技术审批接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_user,
    require_project_visible,
    visible_project_ids,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.pipeline.models import Release
from app.modules.pm import service
from app.modules.project.models import Project

router = APIRouter(prefix="/pm", tags=["项目经理"])


def _can_manage_project(db: Session, current: CurrentUser, project_id: int) -> bool:
    return current.is_admin or check_permission(db, current, "project", project_id, "update")


def _can_act_as_pm(db: Session, current: CurrentUser, project_id: int) -> bool:
    return current.is_admin or service.is_project_pm(db, current.id, project_id)


@router.get("/me", summary="当前用户是不是某个项目的项目经理")
def pm_me(db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    ids = service.pm_project_ids(db, current.id)
    return R.ok({"is_pm": bool(ids) or current.is_admin, "project_ids": ids})


@router.get("/workbench", summary="项目经理工作台")
def workbench(
    project_id: int | None = None,
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(
        service.workbench(
            db,
            current_id=current.id,
            is_admin=current.is_admin,
            project_id=project_id,
            days=days,
        )
    )


@router.get("/directory", summary="给项目经理指派人时的用户检索")
def directory(
    q: str = "",
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if current.is_admin:
        return R.ok(service.search_directory(db, q))
    vis = visible_project_ids(db, current) or set()
    if not any(check_permission(db, current, "project", pid, "update") for pid in vis):
        raise BizException.forbidden("只有项目管理员能检索用户来指定项目经理")
    return R.ok(service.search_directory(db, q))


@router.get("/projects/{project_id}/members", summary="项目经理名单")
def list_members(
    project_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    require_project_visible(db, current, project_id)
    p = db.get(Project, project_id)
    if p is None:
        raise BizException.not_found("项目")
    return R.ok(
        {
            "pm_enabled": bool(getattr(p, "pm_enabled", False)),
            "members": service.serialize_members(db, project_id),
            "can_manage": _can_manage_project(db, current, project_id),
        }
    )


@router.put("/projects/{project_id}/members", summary="指定项目经理")
def put_members(
    project_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = db.get(Project, project_id)
    if p is None:
        raise BizException.not_found("项目")
    if not _can_manage_project(db, current, project_id):
        raise BizException.forbidden("只有项目管理员能指定项目经理")
    user_ids = body.get("user_ids") or []
    if not isinstance(user_ids, list):
        raise BizException.bad_request("user_ids 必须是数组")
    members = service.set_project_pms(db, project_id, [int(x) for x in user_ids])
    return R.ok(members, message="已保存项目经理")


@router.put("/projects/{project_id}/settings", summary="打开或关闭项目经理参与")
def put_project_pm(
    project_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = db.get(Project, project_id)
    if p is None:
        raise BizException.not_found("项目")
    if not _can_manage_project(db, current, project_id):
        raise BizException.forbidden("只有项目管理员能改这个开关")
    if "pm_enabled" in body:
        p.pm_enabled = bool(body.get("pm_enabled"))
    db.commit()
    db.refresh(p)
    return R.ok({"id": p.id, "pm_enabled": bool(p.pm_enabled)})


@router.get("/decisions", summary="业务确认列表")
def list_decisions(
    scope: str = Query("pending"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(
        service.list_decisions(
            db, user_id=current.id, scope=scope, is_admin=current.is_admin
        )
    )


@router.post("/decisions/{decision_id}/decide", summary="项目经理确认或驳回")
def decide(
    decision_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.pipeline import service as pipeline_service

    updated = service.decide_pm(
        db,
        decision_id,
        reviewer_id=current.id,
        approved=bool(body.get("approved", False)),
        comment=str(body.get("comment") or ""),
        is_admin=current.is_admin,
    )
    if updated.status != "queued":
        return R.ok(service.serialize_release_card(db, updated))
    updated, err = pipeline_service.try_execute_release(db, updated.id)
    card = service.serialize_release_card(db, updated)
    if err:
        return R.ok(card, message=f"已确认，但发布未能启动：{err}")
    return R.ok(card, message="已确认")


@router.put("/releases/{release_id}/brief", summary="补业务说明")
def put_brief(
    release_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Release, release_id)
    if r is None:
        raise BizException.not_found("发布")
    from app.modules.pipeline.models import Pipeline

    pipe = db.get(Pipeline, r.pipeline_id)
    if pipe is None:
        raise BizException.not_found("流水线")
    require_project_visible(db, current, pipe.project_id)
    if not (
        _can_act_as_pm(db, current, pipe.project_id)
        or current.id == r.operator_id
        or current.is_admin
    ):
        raise BizException.forbidden("只有发起人或项目经理能改业务说明")
    r = service.update_release_brief(db, r, body)
    return R.ok(service.serialize_release_card(db, r), message="已保存")


@router.get("/releases/{release_id}/announcement", summary="生成可转发的上线通报")
def get_announcement(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Release, release_id)
    if r is None:
        raise BizException.not_found("发布")
    from app.modules.pipeline.models import Pipeline

    pipe = db.get(Pipeline, r.pipeline_id)
    if pipe is None:
        raise BizException.not_found("流水线")
    require_project_visible(db, current, pipe.project_id)
    text = (getattr(r, "announcement_text", "") or "").strip() or service.draft_announcement(db, r)
    return R.ok({"text": text, "announced_at": r.announced_at.isoformat() if r.announced_at else ""})


@router.post("/releases/{release_id}/announce", summary="发送上线通报")
def announce(
    release_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = db.get(Release, release_id)
    if r is None:
        raise BizException.not_found("发布")
    from app.modules.pipeline.models import Pipeline

    pipe = db.get(Pipeline, r.pipeline_id)
    if pipe is None:
        raise BizException.not_found("流水线")
    if not _can_act_as_pm(db, current, pipe.project_id):
        raise BizException.forbidden("只有项目经理能发上线通报")
    extra = body.get("user_ids") or []
    text = service.announce_release(
        db,
        r,
        operator_id=current.id,
        text=str(body.get("text") or ""),
        extra_user_ids=[int(x) for x in extra],
    )
    return R.ok({"text": text}, message="已发送通报")


@router.get("/wecom/callback", summary="企微回调 URL 校验")
def wecom_callback_verify(
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: Session = Depends(get_db),
):
    from app.modules.pm.wecom import verify_callback_echo
    from app.modules.settings import get_all_settings

    cfg = get_all_settings(db)
    token = cfg.get("wecom_callback_token") or ""
    aes_key = cfg.get("wecom_encoding_aes_key") or ""
    if not token or not aes_key:
        raise BizException.bad_request("未配置企微回调 Token / EncodingAESKey")
    try:
        plain = verify_callback_echo(token, aes_key, msg_signature, timestamp, nonce, echostr)
    except Exception as e:  # noqa: BLE001
        raise BizException.bad_request(f"校验失败：{e}")
    return PlainTextResponse(plain)


@router.post("/wecom/callback", summary="企微回调（卡片点击目前回平台页处理）")
async def wecom_callback_event(request: Request):
    # 审批闭环走平台页 + 企微 OAuth，这里只接住企微的事件推送，避免回调 404。
    await request.body()
    return {"errcode": 0, "errmsg": "ok"}
