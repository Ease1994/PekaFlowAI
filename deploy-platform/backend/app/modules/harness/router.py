"""Harness 组件、版本、依赖、运行时、技能、工具与生命周期 API。"""
# ruff: noqa: B008
from __future__ import annotations

import io
import json
import zipfile

from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, assert_visible_menu, get_current_admin, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.harness import broker, lifecycle, packaging, runner, skills, templates, tools
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessVersion,
)
from app.modules.harness.packages import Manifest

router = APIRouter(prefix="/harness", tags=["Harness"])


class InstallRequest(BaseModel):
    manifest: Manifest
    enable: bool = False
    source_ref: str = ""
    package_uri: str = ""
    package_sha256: str = ""


class ScopeRequest(BaseModel):
    scope_key: str = "global"


class EnabledRequest(BaseModel):
    enabled: bool
    scope_key: str = "global"


class BrokerRequest(BaseModel):
    capability: str
    params: dict = {}


def _publisher_id(db: Session, row: HarnessComponent) -> int | None:
    """当前版本的上传人。全员技能用它判断谁能删除，和个人技能的 owner 不是一回事。"""
    if not row.current_version_id:
        return None
    version = db.get(HarnessVersion, row.current_version_id)
    return version.created_by if version is not None else None


def _component_view(db: Session, row: HarnessComponent) -> dict:
    version = db.get(HarnessVersion, row.current_version_id) if row.current_version_id else None
    runtime = db.scalar(
        select(HarnessRuntime)
        .where(HarnessRuntime.component_id == row.id)
        .order_by(HarnessRuntime.id.desc())
    )
    metadata = {}
    if version is not None:
        try:
            metadata = (json.loads(version.manifest_json) or {}).get("metadata") or {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
    owner = skills.personal_owner_id(row, metadata if isinstance(metadata, dict) else {})
    return {
        "id": row.id,
        "key": f"{row.kind}:{row.name}",
        "kind": row.kind,
        "name": row.name,
        "display_name": row.display_name,
        "description": row.description,
        "version": version.version if version else "",
        "manifest": json.loads(version.manifest_json) if version else {},
        "source": row.source,
        "source_ref": row.source_ref,
        "owner_user_id": owner,
        "publisher_id": version.created_by if version is not None else None,
        "status": row.status,
        "enabled": row.enabled,
        "health_status": runtime.health_status if runtime else "unknown",
        "health_message": runtime.health_message if runtime else "",
        "current_version_id": row.current_version_id,
        "installed_at": row.installed_at,
        "enabled_at": row.enabled_at,
        "last_health_at": runtime.last_health_at if runtime else None,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _component_metadata(db: Session, row: HarnessComponent) -> dict:
    """当前版本 manifest 里的 metadata。个人技能的 owner_user_id 可能只写在这里。"""
    if not row.current_version_id:
        return {}
    version = db.get(HarnessVersion, row.current_version_id)
    if version is None:
        return {}
    try:
        raw = json.loads(version.manifest_json) or {}
    except (TypeError, json.JSONDecodeError):
        return {}
    meta = raw.get("metadata") if isinstance(raw, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _visible_component(db: Session, row: HarnessComponent, current: CurrentUser) -> bool:
    """别人的个人技能不出现在组件列表里，详情和下载也同样拦。"""
    return skills.viewer_can_see(
        row,
        viewer_id=current.id,
        is_admin=current.is_admin,
        metadata=_component_metadata(db, row),
    )


def _require_component_visible(db: Session, row: HarnessComponent, current: CurrentUser) -> None:
    """详情/下载/按 id 查版本时，别人的个人技能按不存在处理，避免靠猜 id 偷看。"""
    if not _visible_component(db, row, current):
        raise BizException.not_found("组件")


def _visible_component_ids(db: Session, current: CurrentUser) -> set[int] | None:
    """管理员不过滤；普通人只看得到全局组件和自己的个人技能。"""
    if current.is_admin:
        return None
    return {
        row.id
        for row in db.scalars(select(HarnessComponent)).all()
        if _visible_component(db, row, current)
    }


def _require_component_manage(db: Session, row: HarnessComponent, current: CurrentUser) -> None:
    """启停、卸载、删除。

    管理员可管全部。个人技能只有主人能改。全员技能的发布者可以下架或删除自己共享的包，
    别人不能删。第三方工具含可执行代码，只留给管理员。
    """
    if current.is_admin:
        return
    owner = skills.personal_owner_id(row, _component_metadata(db, row))
    if row.kind == "agent-skill" and owner == current.id:
        return
    if row.kind == "agent-skill" and owner is None and _publisher_id(db, row) == current.id:
        return
    raise BizException.forbidden("只有发布者或管理员可以停用或删除该组件")


def _audit_view(row: HarnessLifecycleAudit) -> dict:
    return {
        "id": row.id,
        "component_id": row.component_id,
        "component_key": row.component_key,
        "extension_id": row.component_id,
        "extension_key": row.component_key,
        "action": row.action,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "success": row.success,
        "message": row.message,
        "actor_name": row.actor_name,
        "source": row.source,
        "created_at": row.created_at,
    }


@router.get("/templates/agent-skill", summary="下载 Agent 技能包开发模板")
def download_skill_template(_: CurrentUser = Depends(get_current_user)):
    return templates.download("release-agent-skill-template.zip", templates.skill_package())


@router.get("/templates/agent-tool", summary="下载第三方 Agent 工具包开发模板")
def download_tool_template(_: CurrentUser = Depends(get_current_user)):
    return templates.download("release-agent-tool-template.zip", templates.tool_package())


@router.get("/components", summary="组件列表")
def components(
    kind: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    stmt = select(HarnessComponent).order_by(HarnessComponent.kind, HarnessComponent.name)
    if kind:
        stmt = stmt.where(HarnessComponent.kind == kind)
    if status:
        stmt = stmt.where(HarnessComponent.status == status)
    rows = [row for row in db.scalars(stmt).all() if _visible_component(db, row, current)]
    return R.ok([_component_view(db, row) for row in rows])


@router.get("/components/{component_id}", summary="组件详情")
def component_detail(
    component_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_visible(db, row, current)
    return R.ok(_component_view(db, row))


@router.get("/components/{component_id}/package", summary="下载已安装技能或工具包")
def download_component_package(
    component_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """技能从 manifest 正文现打 zip；第三方工具返回当初上传的原包。"""
    row = lifecycle.get_component(db, component_id)
    _require_component_visible(db, row, current)
    version = db.get(HarnessVersion, row.current_version_id) if row.current_version_id else None
    if version is None:
        raise BizException.not_found("组件版本")
    if row.kind == "agent-skill":
        filename, data = skills.export_installed_skill(row, version)
        return templates.download(filename, data)
    if row.kind == "agent-tool":
        filename, data = packaging.read_saved_package(version.package_uri)
        return templates.download(filename, data)
    raise BizException.bad_request("该组件没有可下载的开发包")


@router.get("/versions", summary="不可变版本列表")
def versions(
    component_id: int | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if component_id is not None:
        _require_component_visible(db, lifecycle.get_component(db, component_id), current)
    stmt = select(HarnessVersion).order_by(HarnessVersion.id.desc())
    if component_id is not None:
        stmt = stmt.where(HarnessVersion.component_id == component_id)
    rows = list(db.scalars(stmt).all())
    visible = _visible_component_ids(db, current)
    if visible is not None:
        rows = [row for row in rows if row.component_id in visible]
    return R.ok([
        {
            "id": row.id,
            "component_id": row.component_id,
            "version": row.version,
            "manifest": json.loads(row.manifest_json),
            "package_uri": row.package_uri,
            "package_sha256": row.package_sha256,
            "created_by": row.created_by,
            "created_at": row.created_at,
        }
        for row in rows
    ])


@router.get("/dependencies", summary="版本依赖列表")
def dependencies(
    version_id: int | None = None,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    stmt = select(HarnessDependency).order_by(HarnessDependency.id)
    if version_id is not None:
        stmt = stmt.where(HarnessDependency.version_id == version_id)
    return R.ok(list(db.scalars(stmt).all()))


@router.get("/runtimes", summary="运行实例列表")
def runtimes(
    component_id: int | None = None,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    stmt = select(HarnessRuntime).order_by(HarnessRuntime.id.desc())
    if component_id is not None:
        stmt = stmt.where(HarnessRuntime.component_id == component_id)
    return R.ok(list(db.scalars(stmt).all()))


@router.get("/runtime", summary="运行时总览（组件数、隔离状态、活动实例）")
def runtime_overview(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    installed = db.scalars(
        select(HarnessComponent).where(HarnessComponent.status != "uninstalled")
    ).all()
    running = db.scalars(
        select(HarnessRuntime).where(HarnessRuntime.status == "running")
    ).all()
    try:
        isolation = runner.health(db)
        isolation_status, isolation_error = "ready", ""
    except runner.RunnerUnavailable as exc:
        isolation, isolation_status, isolation_error = {}, "unavailable", str(exc)
    return R.ok({
        "status": "degraded" if isolation_status != "ready" else "healthy",
        "loaded_components": len(installed),
        "active_runtimes": len(running),
        "isolation": isolation_status,
        "isolation_error": isolation_error,
        "isolation_detail": isolation,
        "by_kind": {
            kind: sum(1 for row in installed if row.kind == kind)
            for kind in sorted({row.kind for row in installed})
        },
    })


@router.get("/lifecycle", summary="生命周期审计")
def lifecycle_audits(
    component_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if component_id is not None:
        _require_component_visible(db, lifecycle.get_component(db, component_id), current)
    stmt = select(HarnessLifecycleAudit)
    if component_id is not None:
        stmt = stmt.where(HarnessLifecycleAudit.component_id == component_id)
    stmt = stmt.order_by(HarnessLifecycleAudit.id.desc()).limit(500)
    rows = list(db.scalars(stmt).all())
    visible = _visible_component_ids(db, current)
    if visible is not None:
        rows = [row for row in rows if row.component_id in visible]
    return R.ok([_audit_view(row) for row in rows])


@router.get("/events", summary="生命周期事件（前端运行时视图）")
def lifecycle_events(
    component_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return lifecycle_audits(component_id, db, current)


@router.post("/components/install", summary="安装或升级组件")
def install_component(
    body: InstallRequest,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    row = lifecycle.install(
        db,
        body.manifest,
        enable=body.enable,
        source_ref=body.source_ref,
        package_uri=body.package_uri,
        package_sha256=body.package_sha256,
        actor_id=current.id,
        actor_name=current.username,
    )
    return R.ok(_component_view(db, row), message="Harness 组件已安装")


@router.post("/components/upload", summary="上传并安装技能或工具包")
async def upload_component(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """技能包直接启用并进全员目录；第三方工具必须签名且隔离可用，安装后仍需管理员启用。

    技能库上传 = 共享：所有人的助手都能用。删除只有发布者或管理员。
    个人技能仍走助手确认卡，不走这个入口。工具含可执行代码，只有管理员能上传。
    """
    assert_visible_menu(db, current, "skills")
    data = await file.read()
    filename = (file.filename or "").lower()
    if filename and not filename.endswith(".zip"):
        raise BizException.bad_request("请上传 zip 包")
    kind = _detect_kind(data)
    if kind == "agent-skill":
        row = skills.install_package(
            db, data, actor_id=current.id, actor_name=current.username
        )
        return R.ok(_component_view(db, row), message="技能已共享给全部人员，可立即使用")
    if not current.is_admin:
        raise BizException.forbidden("第三方工具包只能由管理员上传")
    row = packaging.install_tool_package(
        db, data, actor_id=current.id, actor_name=current.username
    )
    return R.ok(_component_view(db, row), message="工具已安装，确认后可启用")


def _detect_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = {name.replace("\\", "/") for name in archive.namelist()}
    except zipfile.BadZipFile as exc:
        raise BizException.bad_request("不是合法的 zip 包") from exc
    if any(name.endswith(skills.BODY_NAME) for name in names):
        return "agent-skill"
    if any(name.endswith(packaging.SIGNATURE_NAME) for name in names):
        return "agent-tool"
    raise BizException.bad_request(
        f"无法识别包类型：技能包需要 {skills.BODY_NAME}，工具包需要 {packaging.SIGNATURE_NAME}"
    )


@router.post("/components/{component_id}/enable", summary="启用组件")
def enable_component(
    component_id: int,
    body: ScopeRequest | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.set_enabled(
        db, component_id, True, scope_key=(body.scope_key if body else "global"),
        actor_id=current.id, actor_name=current.username,
    )
    return R.ok(_component_view(db, row), message="已启用")


@router.post("/components/{component_id}/disable", summary="停用组件")
def disable_component(
    component_id: int,
    body: ScopeRequest | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.set_enabled(
        db, component_id, False, scope_key=(body.scope_key if body else "global"),
        actor_id=current.id, actor_name=current.username,
    )
    return R.ok(_component_view(db, row), message="已停用")


@router.post("/components/{component_id}/enabled", summary="设置组件启停状态")
def set_component_enabled(
    component_id: int,
    body: EnabledRequest,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.set_enabled(
        db, component_id, body.enabled, scope_key=body.scope_key,
        actor_id=current.id, actor_name=current.username,
    )
    return R.ok(_component_view(db, row), message="已启用" if body.enabled else "已停用")


@router.post("/components/{component_id}/uninstall", summary="卸载组件")
def uninstall_component(
    component_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.uninstall(
        db, component_id, actor_id=current.id, actor_name=current.username
    )
    return R.ok(_component_view(db, row), message="已卸载")


@router.post("/components/{component_id}/reinstall", summary="重新安装已卸载组件")
def reinstall_component(
    component_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """用库里留下的当前版本重新安装并启用。停用不是卸载，请走启用。"""
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.reinstall(
        db, component_id, actor_id=current.id, actor_name=current.username
    )
    return R.ok(_component_view(db, row), message="已安装并启用")


@router.delete("/components/{component_id}", summary="从仓库删除组件")
def purge_component(
    component_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """管理员可删全局第三方包；普通用户只能删自己装的个人技能。内置工具不在此表。"""
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    lifecycle.purge(db, component_id, actor_id=current.id, actor_name=current.username)
    return R.ok(message="已从技能库删除")


@router.post("/components/{component_id}/health", summary="执行健康检查")
def health_component(
    component_id: int,
    body: ScopeRequest | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = lifecycle.get_component(db, component_id)
    _require_component_manage(db, row, current)
    row = lifecycle.check_health(
        db, component_id, scope_key=(body.scope_key if body else "global"),
        actor_id=current.id, actor_name=current.username,
    )
    return R.ok(_component_view(db, row))


@router.get("/skills", summary="Agent 技能目录")
def list_skills(
    all: bool = Query(default=False, description="含未启用（管理视图）"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(
        skills.catalog(
            db,
            enabled_only=not (all and current.is_admin),
            viewer_id=current.id,
            include_all_personal=current.is_admin,
        )
    )


@router.get("/tools", summary="Agent 工具目录（内置 + 第三方）")
def list_tools(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    return R.ok([item.public() for item in tools.catalog(db)])


@router.get("/tools/{tool_name}/skill-package", summary="下载内置工具对应的技能包")
def download_builtin_tool_skill_package(
    tool_name: str,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    """内置工具不能改代码，导出成技能包后改 name 再上传，用于教模型怎么用。"""
    spec = tools.resolve(db, tool_name)
    if spec is None:
        raise BizException.not_found("工具")
    if spec.source != "builtin":
        raise BizException.bad_request("第三方工具请从该工具行的「下载」获取原 zip")
    filename, data = skills.export_builtin_tool_as_skill(spec)
    return templates.download(filename, data)


@router.get("/tools/metrics", summary="工具调用指标")
def tool_metrics(
    tool_name: str | None = None,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(tools.metrics(db, tool_name))


@router.post("/tools/{tool_name}/trial", summary="试跑工具（管理员）")
def trial_tool(
    tool_name: str,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    payload = body or {}
    return R.ok(
        tools.dispatch(
            db, tool_name, payload.get("arguments") or {}, current, confirmed=True
        )
    )


@router.get("/capabilities", summary="可授予隔离工具的能力清单")
def capabilities(_: CurrentUser = Depends(get_current_user)):
    return R.ok([{"name": name, "description": desc} for name, desc in broker.CAPABILITIES.items()])


@router.post("/broker", summary="隔离 Runner 内工具的受限数据访问入口")
def broker_invoke(
    body: BrokerRequest,
    x_capability_token: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """只认调用级 capability token；没有平台会话，也拿不到 token 以外的任何权限。"""
    return R.ok(broker.invoke(db, x_capability_token, body.capability, body.params))
