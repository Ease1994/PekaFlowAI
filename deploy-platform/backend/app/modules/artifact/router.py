"""制品路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_user,
    visible_pipeline_ids,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.artifact import service, storage
from app.modules.artifact.models import Artifact

router = APIRouter(tags=["制品管理"])

SORT_FIELDS = {
    "size": Artifact.size_bytes,
    "created_at": Artifact.created_at,
    "name": Artifact.name,
}


def _to_public(a: Artifact) -> dict:
    return {
        "id": a.id,
        "pipeline_id": a.pipeline_id,
        "release_id": a.release_id,
        "name": a.name,
        "type": a.type,
        "version": a.version,
        "git_commit": a.git_commit,
        "sha256": a.sha256,
        "size_bytes": a.size_bytes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _ownership(db: Session, artifacts: list[Artifact]) -> dict[int, dict]:
    """一次性查出这批制品的项目/流水线/环境归属，避免逐行查库。"""
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.project.models import Group, Project

    pipeline_ids = {a.pipeline_id for a in artifacts}
    release_ids = {a.release_id for a in artifacts if a.release_id}
    pipelines = {
        p.id: p
        for p in db.scalars(select(Pipeline).where(Pipeline.id.in_(pipeline_ids))).all()
    } if pipeline_ids else {}
    groups = {
        g.id: g
        for g in db.scalars(
            select(Group).where(Group.id.in_({p.group_id for p in pipelines.values()}))
        ).all()
    } if pipelines else {}
    projects = {
        p.id: p
        for p in db.scalars(
            select(Project).where(Project.id.in_({p.project_id for p in pipelines.values()}))
        ).all()
    } if pipelines else {}
    releases = {
        r.id: r
        for r in db.scalars(select(Release).where(Release.id.in_(release_ids))).all()
    } if release_ids else {}

    out: dict[int, dict] = {}
    for a in artifacts:
        p = pipelines.get(a.pipeline_id)
        g = groups.get(p.group_id) if p else None
        proj = projects.get(p.project_id) if p else None
        rel = releases.get(a.release_id) if a.release_id else None
        out[a.id] = {
            # 流水线可能已被删除，这时制品仍列出来，但要让人看出它没了归属
            "pipeline_name": p.name if p else "",
            "project_id": proj.id if proj else None,
            "project_name": proj.name if proj else "",
            "group_name": g.name if g else "",
            "env": g.type if g else "",
            "build_number": (rel.build_number or rel.id) if rel else None,
        }
    return out


@router.get("/artifacts", summary="制品列表")
def list_artifacts(
    pipeline_id: int | None = None,
    release_id: int | None = None,
    project_id: int | None = None,
    type: str = "",
    keyword: str = "",
    env: str = "",
    sort: str = "created_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """只列出用户有权查看的流水线的制品；管理员不受限。"""
    visible = visible_pipeline_ids(db, current)
    if visible is not None and not visible:
        return R.ok({"items": [], "total": 0, "total_bytes": 0, "page": page,
                     "page_size": page_size})

    stmt = select(Artifact)
    if visible is not None:
        stmt = stmt.where(Artifact.pipeline_id.in_(visible))
    if pipeline_id:
        stmt = stmt.where(Artifact.pipeline_id == pipeline_id)
    if release_id:
        stmt = stmt.where(Artifact.release_id == release_id)
    if type:
        stmt = stmt.where(Artifact.type == type)
    if keyword:
        stmt = stmt.where(Artifact.name.like(f"%{keyword}%"))
    if project_id or env:
        from app.modules.pipeline.models import Pipeline
        from app.modules.project.models import Group

        sub = select(Pipeline.id)
        if project_id:
            sub = sub.where(Pipeline.project_id == project_id)
        if env:
            sub = sub.where(
                Pipeline.group_id.in_(select(Group.id).where(Group.type == env))
            )
        stmt = stmt.where(Artifact.pipeline_id.in_(sub))

    # 聚合必须走子查询自己的列，直接引用 Artifact.size_bytes 会和子查询做笛卡尔积
    sub = stmt.subquery()
    total, total_bytes = db.execute(
        select(func.count(), func.sum(sub.c.size_bytes)).select_from(sub)
    ).one()

    column = SORT_FIELDS.get(sort, Artifact.created_at)
    stmt = stmt.order_by(column.asc() if order == "asc" else column.desc())
    # 同值时按 id 兜底，否则翻页会出现重复或漏项
    stmt = stmt.order_by(Artifact.id.desc())
    rows = list(
        db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    )

    own = _ownership(db, rows)
    return R.ok({
        "items": [{**_to_public(a), **own.get(a.id, {})} for a in rows],
        "total": int(total or 0),
        "total_bytes": int(total_bytes or 0),
        "page": page,
        "page_size": page_size,
    })


@router.get("/artifacts/summary", summary="制品占用概览")
def artifacts_summary(
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    data = service.storage_summary(db, visible_pipeline_ids(db, current))
    data["policy"] = {
        "prod_retention_days": service.prod_retention_days_from_settings(db),
        "test_retention_days": service.TEST_RETENTION_DAYS,
    }
    return R.ok(data)


@router.post("/artifacts", summary="登记制品（CI 上报）")
def create_artifact(body: dict, db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    if not check_permission(db, current, "pipeline", body["pipeline_id"], "execute"):
        raise BizException.forbidden(f"无权限：对流水线 #{body['pipeline_id']} 登记制品")
    a = Artifact(
        pipeline_id=body["pipeline_id"],
        release_id=body.get("release_id"),
        name=body["name"],
        type=body.get("type", "jar"),
        version=body.get("version", ""),
        git_commit=body.get("git_commit", ""),
        storage_key=body.get("storage_key", ""),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return R.ok(_to_public(a))


@router.post("/artifacts/batch-delete", summary="批量删除制品")
def batch_delete_artifacts(
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """删制品要对所属流水线有 execute 权限——能发布的人才谈得上清理它的产物。"""
    ids = [int(i) for i in (body.get("ids") or []) if str(i).isdigit()]
    if not ids:
        raise BizException.bad_request("请选择要删除的制品")

    rows = list(db.scalars(select(Artifact).where(Artifact.id.in_(ids))).all())
    if not rows:
        raise BizException.not_found("制品")

    denied = sorted(
        {
            a.pipeline_id
            for a in rows
            if not check_permission(db, current, "pipeline", a.pipeline_id, "execute")
        }
    )
    if denied:
        raise BizException.forbidden(
            "无权限删除流水线 " + "、".join(f"#{i}" for i in denied) + " 的制品"
        )

    freed = sum(a.size_bytes or 0 for a in rows)
    removed = service.delete_artifacts(db, rows)
    return R.ok({"removed": removed, "freed_bytes": freed}, message=f"已删除 {removed} 个制品")


@router.delete("/artifacts/{artifact_id}", summary="删除单个制品")
def delete_artifact(
    artifact_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    a = db.get(Artifact, artifact_id)
    if a is None:
        raise BizException.not_found("制品")
    if not check_permission(db, current, "pipeline", a.pipeline_id, "execute"):
        raise BizException.forbidden(f"无权限：删除流水线 #{a.pipeline_id} 的制品")
    service.delete_artifacts(db, [a])
    return R.ok({"removed": 1}, message="已删除")


@router.get("/artifacts/{artifact_id}/download", summary="下载制品")
def download_artifact(
    artifact_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    a = db.get(Artifact, artifact_id)
    if a is None:
        raise BizException.not_found("制品")
    if not check_permission(db, current, "pipeline", a.pipeline_id, "read"):
        raise BizException.forbidden(f"无权限：下载流水线 #{a.pipeline_id} 的制品")
    path = storage.resolve(a.storage_key)
    return FileResponse(path, filename=a.name, media_type="application/octet-stream")


# ============================================================
# 插件回调：构建机上的打包插件用任务级凭证上传产物
# ============================================================
@router.post("/plugin-api/artifacts/upload", summary="插件上传制品（任务级凭证）")
async def plugin_upload_artifact(
    file: UploadFile = File(...),
    type: str = Form("zip"),
    version: str = Form(""),
    meta: str = Form("{}"),
    db: Session = Depends(get_db),
    x_task_token: str = Header(default=""),
):
    """制品归属由凭证推出来，插件传什么 pipeline_id / release_id 都改不了。"""
    from app.modules.agent.router import _task_from_token

    task = _task_from_token(db, x_task_token)
    data = await file.read()
    name = storage.safe_filename(file.filename or "artifact.zip")
    storage_key, sha = storage.save(task.release_id, name, data)

    try:
        extra = json.loads(meta or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except json.JSONDecodeError:
        extra = {}

    a = Artifact(
        pipeline_id=task.pipeline_id,
        release_id=task.release_id,
        name=name,
        type=type or "zip",
        version=version or "",
        storage_key=storage_key,
        sha256=sha,
        size_bytes=len(data),
        extra_meta=json.dumps(extra, ensure_ascii=False),
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return R.ok({"artifact_id": a.id, "sha256": sha, "size_bytes": len(data)})
