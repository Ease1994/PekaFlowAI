"""研发商店路由（插件 + 模板）：浏览 / 上传 / 安装 / Agent 下载包。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    bearer_scheme,
    check_permission,
    get_current_admin,
    get_current_user,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.pipeline.plugins import PLUGIN_META
from app.modules.store import draft_service, plugin_service
from app.modules.store.models import PipelineTemplate, Plugin

router = APIRouter(tags=["研发商店"])


def _authed(
    request: Request,
    db: Session,
    agent_token: str,
    credentials: HTTPAuthorizationCredentials | None,
) -> bool:
    """插件包下载：构建机用 X-Agent-Token，控制台用 JWT，两者认一个即可。"""
    if agent_token:
        from app.modules.agent.tokens import find_agent_by_presented_token

        if find_agent_by_presented_token(db, agent_token) is not None:
            return True
    if credentials is not None:
        try:
            get_current_user(request, credentials, db)
            return True
        except BizException:
            return False
    return False


@router.get("/store/plugins/template", summary="下载流水线插件包开发模板")
def download_plugin_template(_: CurrentUser = Depends(get_current_user)):
    from app.modules.harness import templates

    return templates.download("rp-pipeline-plugin-template.zip", templates.plugin_package())


@router.get("/store/plugins", summary="插件列表")
def list_plugins(
    category: str | None = None,
    installed: bool | None = Query(default=None, description="仅已安装（编排器选用）"),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    stmt = select(Plugin).order_by(Plugin.category, Plugin.name)
    if category:
        stmt = stmt.where(Plugin.category == category)
    if installed is True:
        stmt = stmt.where(Plugin.installed.is_(True), Plugin.enabled.is_(True))
    rows = [p for p in db.scalars(stmt).all() if plugin_service.visible_in_store(p)]
    if installed is True:
        rows = [p for p in rows if plugin_service.is_runnable_job_plugin(p)]
    return R.ok([plugin_service.serialize_plugin(p) for p in rows])


@router.post("/store/plugins/upload", summary="上传插件 zip（含 task.json）")
async def upload_plugin(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    data = await file.read()
    plugin = plugin_service.upload_zip(db, data, file.filename or "")
    return R.ok(plugin, message="已上传，请点击安装后即可在流水线中使用")


@router.post("/store/plugins/{plugin_id}/install", summary="安装插件")
def install_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    """把仓库里已审核的 zip 标成已安装。这是全站开关：构建机会拉包执行，所以只有管理员能开。

    编排器选用已安装插件仍走项目/流水线 execute。上传 zip、草稿发布也仅管理员。
    """
    return R.ok(plugin_service.install_plugin(db, plugin_id), message="已安装，可立即在流水线中选用")


@router.post("/store/plugins/{plugin_id}/uninstall", summary="卸载第三方插件（保留包，编排器不再展示）")
def uninstall_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(plugin_service.uninstall_plugin(db, plugin_id), message="已卸载")


@router.delete("/store/plugins/{plugin_id}", summary="从仓库删除第三方插件（含安装包）")
def delete_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    plugin_service.delete_plugin(db, plugin_id)
    return R.ok(message="已从仓库删除")


@router.get("/store/plugins/{name}/package", summary="下载插件 zip（Agent 执行 / 控制台二次开发）")
def download_plugin_package(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """Agent 用 X-Agent-Token 拉已安装包；控制台用 JWT 下载源码做二次开发。"""
    if not _authed(request, db, x_agent_token, credentials):
        raise BizException.unauthorized("缺少认证令牌")
    if x_agent_token:
        from app.modules.agent.tokens import find_agent_by_presented_token

        if find_agent_by_presented_token(db, x_agent_token) is not None:
            path = plugin_service.get_package_file(db, name)
            return FileResponse(path, filename=path.name, media_type="application/zip")
    filename, data = plugin_service.read_plugin_package(db, name)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================
# 插件草稿（AI 生成的插件先落这里，人工审过才进仓库）
# ============================================================
@router.get("/store/plugin-drafts", summary="插件草稿列表")
def list_plugin_drafts(
    status: str | None = Query(default=None, description="pending/published/rejected"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    created_by = None if current.is_admin else current.id
    drafts = draft_service.list_drafts(db, status, created_by=created_by)
    out = []
    for d in drafts:
        draft_service.sync_trial_status(db, d)
        out.append(draft_service.serialize(d))
    return R.ok(out)


@router.get("/store/plugin-drafts/{draft_id}", summary="插件草稿详情（含源码与体检结果）")
def get_plugin_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    draft = draft_service.get_draft(db, draft_id)
    draft_service.assert_draft_access(draft, current)
    draft_service.sync_trial_status(db, draft)
    return R.ok(draft_service.serialize(draft, with_files=True))


@router.post("/store/plugin-drafts/{draft_id}/trial", summary="在指定流水线上试跑草稿插件")
def trial_plugin_draft(
    draft_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    pipeline_id = body.get("pipeline_id")
    if not pipeline_id:
        raise BizException.bad_request("请选择一条用于试跑的流水线")
    draft = draft_service.get_draft(db, draft_id)
    draft_service.assert_draft_access(draft, current)
    if not check_permission(db, current, "pipeline", int(pipeline_id), "execute"):
        raise BizException.forbidden(f"无权限：执行流水线 #{pipeline_id}")
    release = draft_service.start_trial(
        db,
        draft_id,
        pipeline_id=int(pipeline_id),
        agent_tag=str(body.get("agent_tag") or "linux"),
        params=body.get("params") or {},
        operator_id=current.id,
    )
    return R.ok(release, message="已提交试跑，日志在该流水线的执行记录里")


@router.post("/store/plugin-drafts/{draft_id}/publish", summary="发布草稿到插件仓库（仍需再点安装）")
def publish_plugin_draft(
    draft_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    payload = body or {}
    plugin = draft_service.publish_draft(
        db,
        draft_id,
        reviewer_id=current.id,
        acknowledge_high=bool(payload.get("acknowledge_high")),
        comment=str(payload.get("comment") or ""),
    )
    return R.ok(plugin, message="已发布到插件仓库，点击安装后才会出现在编排器")


@router.post("/store/plugin-drafts/{draft_id}/reject", summary="驳回插件草稿")
def reject_plugin_draft(
    draft_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    comment = str(body.get("comment") or "").strip()
    if not comment:
        raise BizException.bad_request("驳回要写明原因")
    draft = draft_service.reject_draft(db, draft_id, reviewer_id=current.id, comment=comment)
    return R.ok(draft_service.serialize(draft), message="已驳回")


@router.delete("/store/plugin-drafts/{draft_id}", summary="删除插件草稿")
def delete_plugin_draft(
    draft_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    draft = draft_service.get_draft(db, draft_id)
    draft_service.assert_draft_access(draft, current)
    draft_service.delete_draft(db, draft_id)
    return R.ok(message="已删除草稿")


@router.get("/store/plugin-drafts/{draft_id}/package", summary="下载草稿插件 zip（试跑用）")
def download_draft_package(
    draft_id: int,
    request: Request,
    db: Session = Depends(get_db),
    x_agent_token: str = Header(default=""),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """试跑时构建机按这个地址取包；草稿还没进仓库，所以现打。"""
    if not _authed(request, db, x_agent_token, credentials):
        raise BizException.unauthorized("缺少认证令牌")

    draft = draft_service.get_draft(db, draft_id)
    via_agent = False
    if x_agent_token:
        from app.modules.agent.tokens import find_agent_by_presented_token

        via_agent = find_agent_by_presented_token(db, x_agent_token) is not None
    if not via_agent:
        if credentials is None:
            raise BizException.unauthorized("缺少认证令牌")
        current = get_current_user(request, credentials, db)
        draft_service.assert_draft_access(draft, current)
    data = draft_service.pack_draft(draft)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{draft.name}-draft{draft.id}.zip"'},
    )


@router.get("/store/plugin-meta", summary="插件元数据（图标/显示名，前端编排用）")
def plugin_meta(_: CurrentUser = Depends(get_current_user)):
    return R.ok([
        {"plugin": k, "display_name": v[0], "icon": v[1], "category": v[2]}
        for k, v in PLUGIN_META.items()
    ])


@router.get("/store/templates", summary="流水线模板列表")
def list_templates(db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(list(db.scalars(select(PipelineTemplate).order_by(PipelineTemplate.id.desc())).all()))


@router.get("/store/templates/{template_id}", summary="模板详情")
def get_template(template_id: int, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_user)):
    return R.ok(db.get(PipelineTemplate, template_id))
