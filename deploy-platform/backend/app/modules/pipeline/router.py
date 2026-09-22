"""流水线编排路由（核心，参考蓝盾）。"""
from __future__ import annotations

import json as _json
import time
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_admin,
    get_current_user,
    require_pipeline_visible,
    require_project_visible,
    visible_pipeline_ids,
    visible_project_ids,
)
from app.core.response import BizException, R
from app.db.session import SessionLocal, get_db
from app.modules.agent.task_service import mask_secrets
from app.modules.pipeline import service
from app.modules.pipeline import source_ref_service
from app.modules.pipeline.models import Pipeline, Release, UserPipelinePref, UserViewPref
from app.modules.pipeline.schemas import PipelineGraph

router = APIRouter(tags=["流水线编排"])

# 保存流水线会立刻同步这一条的定时队列；后台每 30s 只扫 trigger_type=cron 的行做兜底。


# 列表绝不序列化 YAML：defer 之后一旦 getattr 就会再打一条懒加载。
_PIPELINE_LIST_OMIT = frozenset({"yaml"})


def _pipeline_caps(
    db: Session,
    current: CurrentUser,
    p: Pipeline,
    *,
    group=None,
    include_yaml: bool = True,
) -> dict:
    """列表/详情附带当前用户对该流水线的操作权，供前端隐藏无权限按钮。"""
    from app.core.response import _serialize
    from app.modules.project.models import Group

    d = _serialize(p, omit=None if include_yaml else _PIPELINE_LIST_OMIT)
    d["can_update"] = check_permission(db, current, "pipeline", p.id, "update")
    d["can_delete"] = check_permission(db, current, "pipeline", p.id, "delete")
    d["can_execute"] = check_permission(db, current, "pipeline", p.id, "execute")
    d["can_create"] = check_permission(db, current, "project", p.project_id, "create")
    d["can_approve"] = check_permission(db, current, "group", p.group_id, "approve")
    # 能不能把这条线设成豁免审批。前端据此置灰选项，后端保存时还会再校验一遍
    d["can_exempt_approval"] = check_permission(
        db, current, "pipeline", p.id, "approval_exempt", allow_wildcard=False
    )

    g = group if group is not None else db.get(Group, p.group_id)
    d["group_type"] = g.type if g is not None else ""
    # 这里必须和 _approval_gate 用同一个判定，否则页面显示「免审批」、
    # 点下去却卡在待审批
    d["approval_required"] = bool(g is not None) and service.approval_required_for(g, p)
    d["group_approval_required"] = bool(g is not None and g.approval_required)
    d["allow_self_approval"] = bool(g is not None and g.allow_self_approval)
    d["allow_emergency_bypass"] = service.can_emergency_bypass(db, g)
    from app.modules.pipeline.variables import pipeline_version_stamp

    d["version_label"] = pipeline_version_stamp(p)
    # 执行弹窗要不要出「发布清单」：有 yaml 时精确看 pack-incremental，
    # 列表 defer yaml，退回 uses_deploy_manifest 列，避免把 YAML 再打一遍
    if include_yaml:
        from app.modules.deploy.service import (
            is_manifest_placeholder,
            pack_incremental_manifest_of,
        )

        configured = pack_incremental_manifest_of(p)
        d["uses_pack_incremental"] = configured is not None
        d["deploy_manifest_default"] = (
            "" if configured is None or is_manifest_placeholder(configured) else configured
        )
    else:
        d["uses_pack_incremental"] = bool(getattr(p, "uses_deploy_manifest", False))
        d["deploy_manifest_default"] = ""
    return d


def _attach_pipeline_caps(
    db: Session, current: CurrentUser, pipelines: list[Pipeline], *, include_yaml: bool
) -> list[dict]:
    """批量附权限：分组一次查出，跳审总闸走请求内缓存。"""
    from app.modules.project.models import Group

    gids = {p.group_id for p in pipelines}
    groups = (
        {g.id: g for g in db.scalars(select(Group).where(Group.id.in_(gids))).all()}
        if gids
        else {}
    )
    service.platform_allows_emergency_bypass(db)
    return [
        _pipeline_caps(
            db, current, p, group=groups.get(p.group_id), include_yaml=include_yaml
        )
        for p in pipelines
    ]


# ============================================================
# 流水线 CRUD
# ============================================================
@router.get("/pipelines", summary="流水线列表（按项目隔离）")
def list_pipelines(
    project_id: int | None = Query(None),
    group_id: int | None = Query(None),
    q: str | None = Query(None, description="按名称模糊搜索，授权联动下拉用"),
    limit: int | None = Query(None, ge=1, le=100, description="最多返回条数，避免一次倒出几万条"),
    deploy_manifest: bool = Query(
        False, description="只列会消费发布清单的流水线（发布提交页用）"
    ),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    visible = visible_pipeline_ids(db, current)
    if visible is not None and project_id:
        proj_ok = visible_project_ids(db, current)
        if proj_ok is not None and project_id not in proj_ok:
            raise BizException.forbidden(f"无权限访问项目 #{project_id}")
    pipelines = service.list_pipelines(
        db,
        project_id,
        group_id,
        uses_deploy_manifest=True if deploy_manifest else None,
        q=q,
        limit=limit,
    )
    if visible is not None:
        pipelines = [p for p in pipelines if p.id in visible]
    out = _attach_pipeline_caps(db, current, pipelines, include_yaml=False)
    _enrich_user_view(db, current, out)
    return R.ok(out)


@router.post("/pipelines/export", summary="导出项目与流水线配置（仅管理员）")
def export_catalog(
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """导出项目、环境分组和流水线 YAML。不带发布记录、制品、凭证。仅管理员。"""
    from app.modules.pipeline import transfer

    body = body or {}
    return R.ok(
        transfer.export_catalog(
            db,
            current,
            project_ids=body.get("project_ids") or None,
            pipeline_ids=body.get("pipeline_ids") or None,
        )
    )


@router.post("/pipelines/import/preview", summary="预览导入：列出重名流水线（仅管理员）")
def preview_import(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """只读比对。有冲突时前端列出，让用户逐条选覆盖、新建 _copy 或跳过。仅管理员。"""
    from app.modules.pipeline import transfer

    return R.ok(transfer.preview_import(db, current, body.get("bundle")))


@router.post("/pipelines/import", summary="导入项目与流水线（仅管理员）")
def apply_import(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_admin),
):
    """按预览结果落地。重名流水线必须在 decisions 给出 overwrite、copy 或 skip。仅管理员。"""
    from app.modules.pipeline import transfer

    return R.ok(
        transfer.apply_import(
            db,
            current,
            body.get("bundle"),
            body.get("decisions") or {},
        )
    )


_TRIGGER_LABEL = {
    "manual": "手动触发",
    "webhook": "Webhook",
    "cron": "定时触发",
    "ai": "AI",
    "rebuild": "Rebuild",
    "rollback": "回滚",
    "deploy_request": "发布提交",
    "sub_pipeline": "子流水线",
}


def _enrich_user_view(db: Session, current: CurrentUser, rows: list[dict]) -> None:
    """给流水线列表逐条补上「当前用户」的收藏/个人分组，以及最近一次执行。

    收藏、最近执行都批量查一次，别在列表里对每条流水线各查一遍。
    """
    from sqlalchemy import func

    from app.modules.auth.models import User

    ids = [r["id"] for r in rows]
    prefs: dict[int, UserPipelinePref] = {}
    last_rel: dict[int, object] = {}
    operators: dict[int, str] = {}
    creators: dict[int, str] = {}
    if ids:
        for p in db.scalars(
            select(UserPipelinePref).where(
                UserPipelinePref.user_id == current.id,
                UserPipelinePref.pipeline_id.in_(ids),
            )
        ).all():
            prefs[p.pipeline_id] = p
        latest_ids = [
            rid
            for _, rid in db.execute(
                select(Release.pipeline_id, func.max(Release.id))
                .where(Release.pipeline_id.in_(ids))
                .group_by(Release.pipeline_id)
            ).all()
            if rid
        ]
        if latest_ids:
            from types import SimpleNamespace

            rel_rows = db.execute(
                select(
                    Release.id,
                    Release.pipeline_id,
                    Release.build_number,
                    Release.status,
                    Release.trigger_by,
                    Release.operator_id,
                    Release.error_message,
                    Release.started_at,
                    Release.created_at,
                ).where(Release.id.in_(latest_ids))
            ).all()
            for row in rel_rows:
                last_rel[row.pipeline_id] = SimpleNamespace(
                    id=row.id,
                    pipeline_id=row.pipeline_id,
                    build_number=row.build_number,
                    status=row.status,
                    trigger_by=row.trigger_by,
                    operator_id=row.operator_id,
                    error_message=row.error_message or "",
                    started_at=row.started_at,
                    created_at=row.created_at,
                    logs="",
                )
            op_ids = {rel.operator_id for rel in last_rel.values() if rel.operator_id}
            if op_ids:
                for u in db.scalars(select(User).where(User.id.in_(op_ids))).all():
                    operators[u.id] = u.display_name or u.username
        creator_ids = {int(r["created_by"]) for r in rows if r.get("created_by")}
        if creator_ids:
            for u in db.scalars(select(User).where(User.id.in_(creator_ids))).all():
                creators[u.id] = u.display_name or u.username
    for r in rows:
        pref = prefs.get(r["id"])
        r["starred"] = bool(pref and pref.starred)
        r["folder"] = (pref.folder if pref else "") or ""
        r["creator_name"] = creators.get(int(r["created_by"] or 0), "") if r.get("created_by") else ""
        rel = last_rel.get(r["id"])
        if rel is None:
            r["last_run_at"] = None
            r["last_release"] = None
            continue
        ts = rel.started_at or rel.created_at
        r["last_run_at"] = ts.isoformat() if ts else None
        r["last_release"] = {
            "id": rel.id,
            "build_number": rel.build_number or rel.id,
            "status": rel.status,
            "trigger_by": rel.trigger_by,
            "trigger_label": _TRIGGER_LABEL.get(rel.trigger_by, rel.trigger_by or ""),
            "operator_name": operators.get(rel.operator_id or 0, "") if rel.operator_id else "",
            "error_summary": service.release_error_summary(rel, max_len=200),
        }


@router.put("/pipelines/{pipeline_id}/pref", summary="设置当前用户对某流水线的收藏/个人分组")
def set_pipeline_pref(
    pipeline_id: int,
    body: dict = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """收藏（⭐置顶）和个人分组都是「当前用户」私有的，看得到这条流水线就能设。

    body: {starred?: bool, folder?: str}，只传哪个改哪个。folder 传空串=移出分组。
    """
    require_pipeline_visible(db, current, pipeline_id)
    payload = body or {}
    pref = db.scalar(
        select(UserPipelinePref).where(
            UserPipelinePref.user_id == current.id,
            UserPipelinePref.pipeline_id == pipeline_id,
        )
    )
    if pref is None:
        pref = UserPipelinePref(user_id=current.id, pipeline_id=pipeline_id)
        db.add(pref)
    if "starred" in payload:
        pref.starred = bool(payload.get("starred"))
    db.commit()
    if "folder" in payload:
        # 走目录接口：移进去时把空组也建上，移出时空串即可
        service.set_pipeline_folder(
            db, current.id, pipeline_id, str(payload.get("folder") or "")
        )
        db.refresh(pref)
    return R.ok({"pipeline_id": pipeline_id, "starred": pref.starred, "folder": pref.folder})


@router.get("/view-pref/{scope}", summary="读取当前用户的视图偏好（排序/筛选）")
def get_view_pref(
    scope: str,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = db.scalar(
        select(UserViewPref).where(
            UserViewPref.user_id == current.id, UserViewPref.scope == scope
        )
    )
    try:
        data = _json.loads(row.data_json) if row and row.data_json else {}
    except _json.JSONDecodeError:
        data = {}
    return R.ok(data if isinstance(data, dict) else {})


@router.put("/view-pref/{scope}", summary="保存当前用户的视图偏好（排序/筛选）")
def set_view_pref(
    scope: str,
    body: dict = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """整段覆盖存一小段 JSON。作用域全局，换项目对不上的筛选值由前端自行忽略。"""
    row = db.scalar(
        select(UserViewPref).where(
            UserViewPref.user_id == current.id, UserViewPref.scope == scope
        )
    )
    text = _json.dumps(body or {}, ensure_ascii=False)[:8000]
    if row is None:
        row = UserViewPref(user_id=current.id, scope=scope, data_json=text)
        db.add(row)
    else:
        row.data_json = text
    db.commit()
    return R.ok()


@router.get("/personal-folders", summary="当前用户在该项目下的个人分组")
def list_personal_folders(
    project_id: int = Query(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """个人分组是「我的工作篮」，不占项目 create 权限。能看见这个项目就能建。"""
    require_project_visible(db, current, project_id)
    return R.ok(service.list_personal_folders(db, current.id, project_id))


@router.post("/personal-folders", summary="新建个人分组（可为空组）")
def create_personal_folder(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    project_id = int(body.get("project_id") or 0)
    if not project_id:
        raise BizException.bad_request("缺少 project_id")
    require_project_visible(db, current, project_id)
    names = service.create_personal_folder(db, current.id, project_id, str(body.get("name") or ""))
    return R.ok(names)


@router.put("/personal-folders", summary="重命名个人分组")
def rename_personal_folder(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    project_id = int(body.get("project_id") or 0)
    if not project_id:
        raise BizException.bad_request("缺少 project_id")
    require_project_visible(db, current, project_id)
    names = service.rename_personal_folder(
        db,
        current.id,
        project_id,
        str(body.get("from") or ""),
        str(body.get("to") or ""),
    )
    return R.ok(names)


@router.delete("/personal-folders", summary="删除个人分组（流水线只是移出，不会被删）")
def delete_personal_folder(
    project_id: int = Query(...),
    name: str = Query(...),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    require_project_visible(db, current, project_id)
    names = service.delete_personal_folder(db, current.id, project_id, name)
    return R.ok(names)


@router.get("/pipelines/recycle-bin", summary="回收站（已删除的流水线）")
def list_recycled_pipelines(
    project_id: int | None = Query(None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """注意：这条路由必须排在 /pipelines/{pipeline_id} 前面，否则会被路径参数吃掉。"""
    from datetime import timedelta

    from app.core.response import _serialize

    # 口径和流水线列表保持一致：项目「可见」即可（只授了流水线权限的人也能看到所属项目），
    # 至于回收站里具体哪几条能操作，下面按 delete 权限逐条过滤。
    if project_id:
        require_project_visible(db, current, project_id)
    items = service.list_pipelines(db, project_id, None, deleted=True)

    out = []
    for p in items:
        # 恢复和彻底删除都要 delete 权限，没权限的条目列出来也只是干看着
        if not check_permission(db, current, "pipeline", p.id, "delete"):
            continue
        d = _serialize(p, omit=_PIPELINE_LIST_OMIT)
        expire_at = (
            p.deleted_at + timedelta(days=service.RECYCLE_RETENTION_DAYS)
            if p.deleted_at
            else None
        )
        d["expire_at"] = expire_at.isoformat() if expire_at else None
        d["days_left"] = max(0, (expire_at - datetime.now()).days) if expire_at else None
        d["can_delete"] = True
        out.append(d)
    return R.ok(out)


@router.get("/pipelines/{pipeline_id}", summary="流水线详情")
def get_pipeline(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id)
    require_pipeline_visible(db, current, pipeline_id)
    return R.ok(_pipeline_caps(db, current, p))


@router.post("/pipelines", summary="创建流水线（带 YAML 校验）")
def create_pipeline(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if not check_permission(db, current, "project", body["project_id"], "create"):
        raise BizException.forbidden(f"无权限：在项目 #{body['project_id']} 创建流水线")
    # 新建时就想设免审批的，同样要有豁免权限——否则「先建成免审批的再用」就是条绕路
    can_exempt = check_permission(
        db, current, "project", body["project_id"], "approval_exempt", allow_wildcard=False
    )
    return R.ok(service.create_pipeline(db, body, current.id, can_exempt=can_exempt))


@router.put("/pipelines/{pipeline_id}", summary="更新流水线")
def update_pipeline(
    pipeline_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id)
    if not check_permission(db, current, "pipeline", pipeline_id, "update"):
        raise BizException.forbidden(f"无权限：更新流水线 #{pipeline_id}")
    can_exempt = check_permission(
        db, current, "pipeline", pipeline_id, "approval_exempt", allow_wildcard=False
    )
    return R.ok(
        service.update_pipeline(
            db,
            pipeline_id,
            body,
            is_admin=current.is_admin,
            can_exempt=can_exempt,
            operator_id=current.id,
        )
    )


@router.delete("/pipelines/{pipeline_id}", summary="删除流水线（进回收站）")
def delete_pipeline(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    service.get_pipeline(db, pipeline_id)
    if not check_permission(db, current, "pipeline", pipeline_id, "delete"):
        raise BizException.forbidden(f"无权限：删除流水线 #{pipeline_id}")
    p = service.delete_pipeline(db, pipeline_id, operator_id=current.id)
    return R.ok({
        "id": p.id,
        "status": p.status,
        "retention_days": service.RECYCLE_RETENTION_DAYS,
    })


@router.post("/pipelines/{pipeline_id}/restore", summary="从回收站恢复流水线")
def restore_pipeline(
    pipeline_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id, allow_deleted=True)
    if not check_permission(db, current, "pipeline", pipeline_id, "delete"):
        raise BizException.forbidden(f"无权限：恢复流水线 #{pipeline_id}")
    return R.ok(service.restore_pipeline(db, pipeline_id, (body or {}).get("name")))


@router.delete("/pipelines/{pipeline_id}/purge", summary="彻底删除（不可恢复）")
def purge_pipeline(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    service.get_pipeline(db, pipeline_id, allow_deleted=True)
    if not check_permission(db, current, "pipeline", pipeline_id, "delete"):
        raise BizException.forbidden(f"无权限：彻底删除流水线 #{pipeline_id}")
    service.purge_pipeline(db, pipeline_id)
    return R.ok()


@router.post("/pipelines/{pipeline_id}/duplicate", summary="复制流水线（复用整份 yaml）")
def duplicate_pipeline_endpoint(
    pipeline_id: int,
    body: dict | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """复制流水线：复用整份 YAML（编排/stages/jobs/steps/triggers/variables 一次完成）。
    name 默认自动加 _copy 后缀（项目内唯一，重复会自动 _2/_3…）。
    """
    src = service.get_pipeline(db, pipeline_id)
    if not check_permission(db, current, "project", src.project_id, "create"):
        raise BizException.forbidden(f"无权限：在项目 #{src.project_id} 复制流水线")
    body = body or {}
    return R.ok(
        service.duplicate_pipeline(
            db,
            pipeline_id,
            new_name=body.get("name"),
            new_group_id=body.get("group_id"),
            new_description=body.get("description"),
            operator_id=current.id,
            is_admin=current.is_admin,
            folder=body.get("folder"),
        )
    )


# ============================================================
# 可视化编排（核心 API）
# ============================================================
@router.get("/pipelines/{pipeline_id}/graph", summary="获取流水线编排图（前端可视化用）")
def get_graph(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id)
    require_pipeline_visible(db, current, pipeline_id)
    return R.ok(service.get_graph(db, pipeline_id))


@router.get("/pipelines/{pipeline_id}/next-run", summary="查询下次定时触发时间")
def get_next_run(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id)
    require_pipeline_visible(db, current, pipeline_id)
    from app.modules.pipeline.scheduler import get_next_run_time

    t = get_next_run_time(pipeline_id)
    return R.ok({"next_run_at": t.isoformat() if t else None})


@router.put("/pipelines/{pipeline_id}/graph", summary="保存可视化编排结果")
def save_graph(
    pipeline_id: int,
    graph: PipelineGraph,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = service.get_pipeline(db, pipeline_id)
    if not check_permission(db, current, "pipeline", pipeline_id, "update"):
        raise BizException.forbidden(f"无权限：更新流水线 #{pipeline_id} 的编排")
    return R.ok(service.save_graph(db, pipeline_id, graph))


# ============================================================
# 发布任务
# ============================================================
@router.get("/releases", summary="发布列表（按项目隔离 + 多维过滤）")
def list_releases(
    pipeline_id: int | None = Query(None),
    status: str | None = Query(None),
    trigger_by: str | None = Query(None),
    operator_id: int | None = Query(None),
    project_id: int | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    admin_only: bool = Query(False, description="仅管理员（true 时绕过项目可见性看全部）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if admin_only and not current.is_admin:
        # 非管理员传 admin_only=true → 拒绝（防止普通用户绕过可见性看全平台）
        from app.core.response import BizException
        raise BizException.forbidden("发布管理仅对管理员开放")

    page_args = {"page": page, "page_size": page_size}
    if admin_only and current.is_admin:
        # 管理员视角不加可见性限制，但依旧分页
        rels, total = service.list_releases(
            db, pipeline_id, status, trigger_by, operator_id, project_id, date_from, date_to,
            **page_args,
        )
    elif pipeline_id:
        # 回收站里的流水线也要能翻历史发布记录
        service.get_pipeline(db, pipeline_id, allow_deleted=True)
        require_pipeline_visible(db, current, pipeline_id)
        rels, total = service.list_releases(
            db, pipeline_id=pipeline_id, status=status, trigger_by=trigger_by,
            operator_id=operator_id, project_id=project_id, date_from=date_from, date_to=date_to,
            **page_args,
        )
    else:
        rels, total = service.list_releases(
            db, None, status, trigger_by, operator_id, project_id, date_from, date_to,
            visible_ids=visible_pipeline_ids(db, current), **page_args,
        )

    return R.ok({
        "items": service.decorate_releases(db, rels),
        "total": total,
        "page": page,
        "page_size": page_size,
    })


@router.get("/releases/statistics", summary="发布统计概览")
def statistics(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    return R.ok(service.statistics(db))


@router.get("/releases/{release_id}", summary="发布详情")
def get_release(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    r = service.get_release(db, release_id)
    # 流水线进了回收站也要能翻历史记录，只读接口放行
    p = service.get_pipeline(db, r.pipeline_id, allow_deleted=True)
    require_pipeline_visible(db, current, p.id)
    return R.ok(r)


@router.post("/releases", summary="创建发布（自动感知分组审批）")
def create_release(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    # 发起发布需要对该流水线有 execute 权限
    if not check_permission(db, current, "pipeline", body["pipeline_id"], "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{body['pipeline_id']} 发起发布")
    from app.modules.deploy.service import apply_execute_manifest
    from app.modules.pipeline.sub_pipeline import merge_params

    pipe = service.get_pipeline(db, body["pipeline_id"])
    run_params = merge_params(pipe, body.get("run_params") or body.get("params"))
    # 和 /execute 一样始终按清单规则合一次：没有增量插件则原样返回。
    # skip_approval 不能从 HTTP 读，那是子流水线服务端证明父子关系才用的。
    run_params = apply_execute_manifest(pipe, run_params, body.get("deploy_manifest"))
    release = service.create_release(
        db,
        pipeline_id=body["pipeline_id"],
        version=body.get("version", ""),
        strategy=body.get("strategy", "rolling"),
        trigger_by=body.get("trigger_by", "manual"),
        operator_id=current.id,
        source_ref=body.get("source_ref"),  # 代码版本（commit/branch/tag），Rebuild 用
        run_params=run_params,
        emergency_bypass=bool(body.get("emergency_bypass", False)),
        emergency_bypass_reason=str(body.get("emergency_bypass_reason") or ""),
        brief={
            "business_summary": body.get("business_summary") or "",
            "impact_scope": body.get("impact_scope") or "",
            "iteration_tag": body.get("iteration_tag") or "",
            "planned_window": body.get("planned_window") or "",
            "audience": body.get("audience") or "",
            "need_user_notice": bool(body.get("need_user_notice", False)),
        },
    )
    # SHA 给 Rebuild 和变更列表用，问 Git 托管不能挡创建接口返回
    if not (release.source_ref or "").strip():
        source_ref_service.fill_source_ref_later(release.id)
    # 测试分组（queued）→ 立即执行；生产分组（pending）→ 走审批，审批通过后再执行
    if release.status == "queued":
        release, err = service.try_execute_release(db, release.id)
        if err:
            return R.ok(release, message=f"发布未能启动：{err}")
    return R.ok(release)


@router.post("/releases/{release_id}/approve", summary="审批发布（生产分组强制审批）")
def approve_release(
    release_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    release = service.get_release(db, release_id)
    # 审批需要对该分组有 approve 权限
    if not check_permission(db, current, "group", release.group_id, "approve"):
        raise BizException.forbidden(f"无权限：审批分组 #{release.group_id} 的发布")
    release = service.approve_release(
        db,
        release_id,
        body.get("approved", False),
        body.get("comment", ""),
        reviewer_id=current.id,
    )
    if release.status != "queued":
        return R.ok(release)
    # 同 /approvals/{id}/decide：审批已落库，执行失败不能报成审批失败
    release, err = service.try_execute_release(db, release.id)
    if err:
        return R.ok(release, message=f"审批已通过，但发布未能启动：{err}")
    return R.ok(release)


@router.get("/releases/{release_id}/rollback-preview", summary="回滚预览：会撤销什么、有什么风险")
def rollback_preview(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """回滚前先让人看清楚要动什么。

    这是往生产写盘的操作，「点了之后会发生什么」必须在点之前就说明白，
    而不是等跑完了去日志里翻。
    """
    from app.modules.deployment import service as deployment_service

    release = service.get_release(db, release_id)
    require_pipeline_visible(db, current, release.pipeline_id)
    rows, warnings, reason = deployment_service.check_undoable(db, release_id)
    rolling = len(rows) > 1
    return R.ok({
        "can_rollback": bool(rows),
        "reason": reason,
        "summary": deployment_service.describe(rows) if rows else "",
        "warnings": warnings,
        "rolling": rolling,
        # 弹窗标题用流水线构建号，避免和列表上的 #N 对不上
        "build_number": release.build_number or release.id,
        # 多台时按执行顺序列：后发的先撤，和滚动撤销 Job 一致
        "items": [
            deployment_service.preview_item(db, r)
            for r in (reversed(rows) if rolling else rows)
        ],
    })


@router.post("/releases/{release_id}/rollback", summary="一键回滚")
def rollback_release(
    release_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    release = service.get_release(db, release_id)
    # 回滚需要对该流水线有 execute 权限
    if not check_permission(db, current, "pipeline", release.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：回滚流水线 #{release.pipeline_id} 的发布")
    payload = body or {}
    from app.modules.deployment.service import normalize_image_tags

    return R.ok(
        service.rollback_release(
            db,
            release_id,
            current.id,
            emergency_bypass=bool(payload.get("emergency_bypass")),
            emergency_bypass_reason=str(payload.get("emergency_bypass_reason") or ""),
            image_tags=normalize_image_tags(payload.get("image_tags")),
        )
    )


@router.post("/releases/{release_id}/execute", summary="执行发布（本地引擎）")
def execute_release(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.core.deps import check_permission
    release = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{release.pipeline_id} 执行发布")
    release, err = service.try_execute_release(db, release_id)
    if err:
        return R.ok(release, message=f"发布未能启动：{err}")
    return R.ok(release)


@router.post("/releases/{release_id}/cancel", summary="取消发布（执行卡死/主动取消）")
def cancel_release(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.core.deps import check_permission
    release = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：取消流水线 #{release.pipeline_id} 的发布")
    return R.ok(service.cancel_release(db, release_id, current.id))


@router.post("/releases/{release_id}/rebuild", summary="Rebuild 重新发布（蓝盾风格，用当前 yaml 重建发布）")
def rebuild_release(
    release_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """用原 release 的 source_ref（commit/branch/tag）重建发布（trigger_by=rebuild），并自动执行。"""
    from app.core.deps import check_permission
    src = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", src.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：Rebuild 流水线 #{src.pipeline_id} 的发布")
    payload = body or {}
    new_release = service.rebuild_release(
        db,
        release_id,
        current.id,
        emergency_bypass=bool(payload.get("emergency_bypass")),
        emergency_bypass_reason=str(payload.get("emergency_bypass_reason") or ""),
        deploy_manifest=payload.get("deploy_manifest") if "deploy_manifest" in payload else None,
    )
    return R.ok(new_release)


@router.post("/pipelines/{pipeline_id}/execute", summary="一键执行流水线（执行记录页右上角「执行」按钮）")
def quick_execute_pipeline(
    pipeline_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """手动触发一次流水线执行：创建 release（测试分组直接跑，生产分组进审批）并返回。

    语义：执行记录页右上角「执行」按钮，就是"现在跑一次这条流水线"。
    """
    from datetime import datetime
    from app.core.deps import check_permission

    p = service.get_pipeline(db, pipeline_id)
    if not check_permission(db, current, "pipeline", pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：执行流水线 #{pipeline_id}")

    version = f"manual-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    payload = body or {}
    # 执行前参数面板填的值：与变量默认值合并后存进 release，执行时替换到步骤参数里
    from app.modules.deploy.service import apply_execute_manifest
    from app.modules.pipeline.sub_pipeline import merge_params

    run_params = merge_params(p, payload.get("run_params") or payload.get("params"))
    # 发布清单：有提取增量包步骤才写入 DEPLOY_MANIFEST，没有该插件则忽略
    run_params = apply_execute_manifest(p, run_params, payload.get("deploy_manifest"))
    release = service.create_release(
        db, pipeline_id=pipeline_id, version=version, strategy="rolling",
        trigger_by="manual", operator_id=current.id,
        run_params=run_params,
        emergency_bypass=bool(payload.get("emergency_bypass", False)),
        emergency_bypass_reason=str(payload.get("emergency_bypass_reason") or ""),
    )
    # 测试分组 → queued 直接执行；生产分组 → pending 等审批
    if release.status == "queued":
        release, err = service.try_execute_release(db, release.id)
        if err:
            return R.ok(release, message=f"发布未能启动：{err}")
    return R.ok(release)


@router.get("/pipelines/{pipeline_id}/start-params", summary="子流水线启动参数（执行时展示的变量）")
def get_pipeline_start_params(
    pipeline_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.modules.pipeline.sub_pipeline import list_start_params

    p = service.get_pipeline(db, pipeline_id)
    require_pipeline_visible(db, current, pipeline_id)
    if not check_permission(db, current, "pipeline", pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：查看流水线 #{pipeline_id} 的启动参数")
    return R.ok(list_start_params(p))


@router.post("/pipelines/{pipeline_id}/run", summary="启动流水线（可带子流水线父级与启动参数）")
def run_pipeline(
    pipeline_id: int,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """同步语义由调用方决定（本接口只负责创建并排队执行）。

    插件在 Agent 上轮询；控制台/联调可直接调本接口。
    """
    from app.modules.pipeline.sub_pipeline import start_sub_pipeline

    payload = body or {}
    release = start_sub_pipeline(
        db,
        user=current,
        pipeline_id=pipeline_id,
        project_id=payload.get("project_id") or payload.get("projectId"),
        params=payload.get("params") or {},
        parent_pipeline_id=payload.get("parent_pipeline_id") or payload.get("parentPipelineId"),
        parent_release_id=payload.get("parent_release_id") or payload.get("parentReleaseId"),
    )
    return R.ok(release)


@router.get("/releases/{release_id}/commits", summary="代码变更（本次发布 vs 上次发布的 commit 区间）")
def get_release_commits(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """返回本次发布的 commit 区间：起点=同 pipeline 上一次发布 source_ref，终点=本次 source_ref。

    用于「代码变更」Tab，展示这次发布相比上次多了哪些 commit（author/title/time）。
    """
    from app.core.deps import check_permission
    release = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "read"):
        raise BizException.forbidden(f"无权限：查看流水线 #{release.pipeline_id} 代码变更")
    return R.ok(source_ref_service.fetch_commits_between(db, release))


@router.get("/releases/{release_id}/logs", summary="获取执行日志")
def get_release_logs(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.core.deps import check_permission
    release = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "read"):
        raise BizException.forbidden(f"无权限：查看流水线 #{release.pipeline_id} 日志")
    return R.ok(service.get_release_logs(db, release_id))


@router.get("/releases/{release_id}/sequence", summary="执行序列明细（stage→job→step 树）")
def get_release_sequence(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """返回发布记录的 stage→job→step 树（含每个 task 的状态、耗时、流水线全局变量）。"""
    from app.core.deps import check_permission
    from app.modules.agent.models import BuildTask
    from app.modules.pipeline.schemas import parse_yaml

    release = service.get_release(db, release_id)
    if not check_permission(db, current, "pipeline", release.pipeline_id, "read"):
        raise BizException.forbidden(f"无权限：查看流水线 #{release.pipeline_id} 执行序列")

    pipeline = service.get_pipeline(db, release.pipeline_id, allow_deleted=True)

    try:
        run_params = _json.loads(release.run_params_json or "{}")
    except Exception:
        run_params = {}

    # 流水线级全局变量定义（按 pipeline 隔离，每个流水线各自一套）
    # value 用执行期上下文渲染过：默认值里写了 ${{BK_CI_BUILD_NUM}} 时，
    # 这里显示的是本次构建真正用的值，而不是占位符本身
    pipeline_variables: list[dict] = []
    try:
        from app.modules.pipeline.variables import build_context

        definition = parse_yaml(pipeline.yaml)
        variables = definition.pipeline.variables or []
        resolved = build_context(
            db, release, pipeline, variables,
            run_params if isinstance(run_params, dict) else {},
        )
        pipeline_variables = [
            {
                "name": v.name,
                "type": v.type,
                "default_value": v.default_value,
                "value": resolved.get(v.name, v.default_value),
                "description": v.description,
            }
            for v in variables
        ]
    except Exception:
        pass

    # Release 总耗时
    def _duration(start, end):
        if not start or not end:
            return None
        return round((end - start).total_seconds(), 1)

    now = datetime.now()
    total_duration = _duration(release.started_at, release.finished_at or (now if release.started_at else None))

    tasks = db.scalars(
        select(BuildTask)
        .where(BuildTask.release_id == release_id)
        .order_by(BuildTask.id)
    ).all()

    def _fmt_duration(seconds: float | None) -> str | None:
        """把秒数格式化成 01:55 / 01:23:45。"""
        if seconds is None or seconds < 0:
            return None
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _step_status(step_order: int, max_reported: int, task_status: str,
                     task_started_at, task_finished_at) -> str:
        """没拿到 Agent 上报时，推断这个 step 的状态。

        推断只依据「Agent 已经报到第几步了」，不猜时间。早先按耗时比例估算，
        结果是运行中的 Job 里所有步骤都被算成成功——页面上明明还没轮到的步骤先挂了绿勾，
        比不显示状态更糟：它会让人以为那一步已经跑过了。
        """
        if not task_started_at:
            return "pending"
        if task_finished_at:
            return task_status  # success / failed
        # Job 还在跑：报到哪儿算哪儿，后面的一律 pending
        return "success" if step_order < max_reported else "pending"

    # 构造 stage → jobs → steps 树
    stage_map: dict[str, dict] = {}
    for t in tasks:
        stage = stage_map.setdefault(t.stage_name, {"name": t.stage_name, "jobs": {}, "order": len(stage_map)})
        try:
            steps_raw = _json.loads(t.steps_json or "[]")
        except Exception:
            steps_raw = []
        try:
            reported = {
                r.get("index"): r
                for r in _json.loads(t.step_results_json or "[]")
                if isinstance(r, dict)
            }
        except Exception:
            reported = {}

        total_steps = len(steps_raw)
        job_duration = _duration(t.started_at, t.finished_at or (now if t.started_at else None))
        max_reported = max(reported) if reported else -1
        steps = []
        for i, s in enumerate(steps_raw):
            got = reported.get(i)
            if got:
                # Agent 逐步上报的真实数据
                s_status = got.get("status") or "pending"
                s_duration = got.get("duration")
                s_started = got.get("started_at")
            else:
                # 老版本 Agent 没有步骤上报，只能顺着 Job 推断
                s_status = _step_status(i, max_reported, t.status, t.started_at, t.finished_at)
                # Job 耗时不是步骤耗时。只有整个 Job 就一步时两者才相等，
                # 否则每一步都标上总耗时，看起来像每步都跑了这么久
                s_duration = job_duration if total_steps == 1 else None
                s_started = None
            steps.append({
                "order": i,
                # 同一个 Job 可能被拆成 Agent 段和平台段两条 task，
                # 日志要按步骤自己所在的 task 去取，不能统一用 job 的第一条
                "task_id": t.id,
                # 页面上的序号会按整个 Job 重排，但日志里的步骤标记用的是 task 内序号
                "log_index": i,
                # 用户起的步骤名。这次发布之前建的任务没存过 name，留空让前端退回插件名
                "name": s.get("name") or "",
                "plugin": s.get("plugin", ""),
                # 步骤参数里注入过解密后的 git 凭证，页面上会把它整个展示出来
                "with": mask_secrets(s.get("with", {})),
                "status": s_status,
                "started_at": s_started,
                "duration": s_duration,
                "duration_label": _fmt_duration(s_duration) if s_duration is not None else None,
            })

        job = stage["jobs"].get(t.job_id)
        if job is None:
            stage["jobs"][t.job_id] = {
                "task_id": t.id,
                "id": t.job_id,
                "name": t.job_name,
                "agent": t.agent_tag,
                "status": t.status,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                "duration": job_duration,
                "duration_label": _fmt_duration(job_duration),
                "steps": steps,
            }
            continue

        # 同一个 Job 的后续分段：步骤接着排，Job 的状态与耗时按整体重算
        base = len(job["steps"])
        for s in steps:
            s["order"] = base + s["order"]
        job["steps"].extend(steps)
        if t.finished_at:
            job["finished_at"] = t.finished_at.isoformat()
        if t.status in ("failed", "timeout", "cancelled") or job["status"] in ("success", "pending"):
            job["status"] = t.status
        job["duration"] = (job["duration"] or 0) + (job_duration or 0)
        job["duration_label"] = _fmt_duration(job["duration"])

    live_stages = [
        {
            "name": s["name"],
            "status": _aggregate_stage_status(list(s["jobs"].values())),
            "jobs": list(s["jobs"].values()),
        }
        for s in stage_map.values()
    ]
    # 画布和列表都以编排定义为底。只返回已落库任务时，尚未派发的 Job
    # 会在开跑瞬间从树上消失，看起来像步骤丢了。回滚用自己的计划，不能拿 YAML 顶替。
    planned = _plan_preview_stages(release) or _planned_stages(pipeline)
    stages = _merge_sequence_stages(planned, live_stages)

    canvas_layout: dict = {}
    try:
        from app.modules.pipeline.graph import attach_stage_ids, canvas_layout_of, yaml_stage_ids

        definition = parse_yaml(pipeline.yaml)
        canvas_layout = canvas_layout_of(definition)
        stages = attach_stage_ids(stages, yaml_stage_ids(definition))
    except Exception:
        for i, s in enumerate(stages):
            s.setdefault("id", f"stage-{i + 1}")

    return R.ok({
        "release_id": release_id,
        "build_number": release.build_number or release.id,
        "pipeline_id": pipeline.id,
        "pipeline_name": pipeline.name,
        "version": release.version,
        "source_ref": release.source_ref,
        "trigger_by": release.trigger_by,
        "status": release.status,
        # 启动失败写在 error_message，步骤树此时全是 pending 预览。
        # 明细页必须用这个字段展示原因，不能从阶段状态去猜。
        "error": service.release_error_summary(release, max_len=2000, db=db),
        "created_at": release.created_at.isoformat() if release.created_at else None,
        "started_at": release.started_at.isoformat() if release.started_at else None,
        "finished_at": release.finished_at.isoformat() if release.finished_at else None,
        "total_duration": total_duration,
        "total_duration_label": _fmt_duration(total_duration),
        "is_running": release.status in ("running", "queued", "pending", "assigned"),
        "pipeline_variables": pipeline_variables,
        "canvas_layout": canvas_layout,
        "stages": stages,
    })


def _merge_sequence_stages(planned: list[dict], live: list[dict]) -> list[dict]:
    """把已开跑的 Job 状态盖到编排树上，未开跑的 Job 保持待执行。

    匹配顺序：同名阶段里先对 Job id，再对 Job 名。对不上的计划 Job 保持待执行，
    任务里多出来的 Job（拆段、回滚补步）接在该阶段末尾。
    """
    if not planned:
        return live
    if not live:
        return planned

    remaining_stages = list(live)

    def take_stage(name: str) -> dict | None:
        """从剩余实况里取出同名阶段，避免两个同名阶段抢同一份任务。"""
        for i, stage in enumerate(remaining_stages):
            if (stage.get("name") or "") == name:
                return remaining_stages.pop(i)
        return None

    def take_job(pool: list[dict], planned_job: dict) -> dict | None:
        """从该阶段剩余任务里取出与计划 Job 对应的那一条。"""
        job_id = planned_job.get("id") or ""
        name = planned_job.get("name") or ""
        if job_id:
            for i, job in enumerate(pool):
                if (job.get("id") or "") == job_id:
                    return pool.pop(i)
        if name:
            for i, job in enumerate(pool):
                if (job.get("name") or "") == name:
                    return pool.pop(i)
        return None

    merged: list[dict] = []
    for planned_stage in planned:
        live_stage = take_stage(planned_stage.get("name") or "")
        if live_stage is None:
            merged.append(planned_stage)
            continue
        pool = list(live_stage.get("jobs") or [])
        jobs = [take_job(pool, job) or job for job in (planned_stage.get("jobs") or [])]
        jobs.extend(pool)
        merged.append({
            "id": planned_stage.get("id"),
            "name": planned_stage.get("name"),
            "status": _aggregate_stage_status(jobs),
            "jobs": jobs,
        })
    merged.extend(remaining_stages)
    return merged


def _plan_preview_stages(release) -> list[dict]:
    """回滚这类带专属计划的发布，等审批时展示它自己的步骤。"""
    jobs_raw = service.plan_jobs_of(release)
    if not jobs_raw:
        return []
    # 计划不止回滚在用，审批人看到的阶段名得是这次真要干的事
    meta = service.plan_meta_of(release)
    stage_name = str(meta.get("name") or "") or "回滚"
    default_job_name = str(meta.get("job_name") or "") or "撤销上次发布"
    jobs = []
    for i, job in enumerate(jobs_raw):
        jobs.append({
            "task_id": None,
            "id": job.get("id") or f"undo-{i}",
            "name": job.get("name") or default_job_name,
            "agent": job.get("agent") or "any",
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "duration": None,
            "duration_label": None,
            "steps": [
                {
                    "order": si,
                    "task_id": None,
                    "log_index": si,
                    "name": s.get("name") or "",
                    "plugin": s.get("plugin", ""),
                    "with": mask_secrets(s.get("with") or {}),
                    "status": "pending",
                    "started_at": None,
                    "duration": None,
                    "duration_label": None,
                }
                for si, s in enumerate(job.get("steps") or [])
                if isinstance(s, dict)
            ],
        })
    return [{"id": "stage-1", "name": stage_name, "status": "pending", "jobs": jobs}] if jobs else []


def _planned_stages(pipeline) -> list[dict]:
    """按流水线定义生成「待执行」的 stage→job→step 树。

    构建任务是发布真正开跑时才落库的，在那之前只能拿定义顶上，
    让人至少知道这次发布会做哪些事。
    """
    from app.modules.pipeline.schemas import parse_yaml

    try:
        definition = parse_yaml(pipeline.yaml)
    except Exception:
        return []

    stages = []
    for si, stage in enumerate(definition.pipeline.stages):
        jobs = []
        for ji, job in enumerate(stage.jobs):
            jobs.append({
                "task_id": None,
                "id": job.id or f"job-{ji}",
                "name": job.name or job.id or f"Job {ji + 1}",
                "agent": job.agent,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "duration": None,
                "duration_label": None,
                "steps": [
                    {
                        "order": i,
                        "task_id": None,
                        "log_index": i,
                        "name": s.name or "",
                        "plugin": s.plugin,
                        "with": s.with_ or {},
                        "status": "pending",
                        "started_at": None,
                        "duration": None,
                        "duration_label": None,
                    }
                    for i, s in enumerate(job.steps)
                ],
            })
        stages.append({
            "id": f"stage-{si + 1}",
            "name": stage.name or f"Stage {si + 1}",
            "status": "pending",
            "jobs": jobs,
        })
    return stages


def _aggregate_stage_status(jobs: list[dict]) -> str:
    """聚合 stage 状态：任一 job failed → stage failed；全部 success → success；否则 running/pending。"""
    statuses = {j["status"] for j in jobs}
    if "failed" in statuses or "timeout" in statuses:
        return "failed"
    if statuses == {"success"}:
        return "success"
    if "running" in statuses or "assigned" in statuses:
        return "running"
    return "pending"


@router.post("/releases/{release_id}/logs/stream-token", summary="换取发布日志 SSE 短时票")
def issue_release_log_stream_token(
    release_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """用登录 JWT 换 60 秒 stream_token。EventSource 只能把票放在 URL 上。"""
    from app.modules.agent.stream_token import STREAM_TTL_SECONDS, issue_stream_token

    release = service.get_release(db, release_id)
    require_pipeline_visible(db, current, release.pipeline_id)
    return R.ok(
        {"stream_token": issue_stream_token(current, release_id=release_id), "expires_in": STREAM_TTL_SECONDS}
    )


@router.get("/releases/{release_id}/logs/stream", summary="实时日志流（SSE，带 token 鉴权）")
def stream_release_logs(
    release_id: int,
    stream_token: str = Query("", description="短时票，由 /logs/stream-token 签发"),
    offset: int = Query(0, description="从第几行开始推送（断线续传）"),
):
    """SSE 流式推送日志，前端用 EventSource 订阅。只接受 60 秒 stream_token。"""
    from app.modules.agent.stream_token import user_from_stream_token

    user = user_from_stream_token(stream_token, release_id=release_id)

    with SessionLocal() as db:
        release = service.get_release(db, release_id)
        if not check_permission(db, user, "pipeline", release.pipeline_id, "read"):
            raise BizException.forbidden(f"无权限：查看流水线 #{release.pipeline_id} 日志")

    def event_generator():
        from app.modules.agent.log_store import MAX_FETCH_LINES, create_log_store
        from app.modules.agent.models import BuildTask
        from app.modules.settings import get_all_settings

        with SessionLocal() as db:
            store = create_log_store(SessionLocal, lambda: get_all_settings(db))

        # 每个任务各记一个游标，只取新增的行。
        # 老实现每秒调一次 get_release_logs：把整次发布所有任务的日志全查回来、
        # 拼成一个大字符串、再 split 一遍，然后只用末尾几行。一次几万行的构建
        # 就是每秒几十 MB 的分配，还是按连接数翻倍——这是最容易把进程撑爆的地方。
        sent: dict[int, int] = {}
        headed: set[int] = set()
        skip = max(offset, 0)  # 断线续传：跳过已经看过的行

        def emit(payload: dict) -> str:
            return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"

        while True:
            with SessionLocal() as db:
                rows = db.execute(
                    select(
                        BuildTask.id, BuildTask.stage_name, BuildTask.job_name, BuildTask.status
                    )
                    .where(BuildTask.release_id == release_id)
                    .order_by(BuildTask.id)
                ).all()
                status = db.scalar(select(Release.status).where(Release.id == release_id)) or ""

            for tid, stage_name, job_name, tstatus in rows:
                batch: list[str] = []
                if tid not in headed:
                    headed.add(tid)
                    batch.append(f"════════ ⏵ {stage_name} / {job_name} ({tstatus}) ════════")
                seen = sent.get(tid, 0)
                chunk = store.get(tid, start=seen, limit=MAX_FETCH_LINES) or []
                if chunk:
                    batch.extend(chunk)
                    sent[tid] = seen + sum(1 for ln in chunk if not str(ln).startswith("…"))
                if not batch:
                    continue
                if skip:
                    dropped = min(skip, len(batch))
                    skip -= dropped
                    batch = batch[dropped:]
                    if not batch:
                        continue
                # 一行一个事件的话，几千行历史会触发浏览器几千次 onmessage
                yield emit({"lines": batch})

            if status in ("success", "failed", "cancelled", "rejected"):
                yield f"event: done\ndata: {_json.dumps({'status': status}, ensure_ascii=False)}\n\n"
                break
            yield ": keepalive\n\n"
            time.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 关闭缓冲
        },
    )
