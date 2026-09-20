"""审批路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.approval.models import Approval
from app.modules.auth.models import User
from app.modules.pipeline import service as pipeline_service
from app.modules.pipeline.models import Pipeline, Release
from app.modules.project.models import Group, Project

router = APIRouter(tags=["审批管理"])


class _Refs:
    """一次性把审批列表要用到的关联对象查出来。

    逐行 db.get(Release/Pipeline/Group/Project/User) 是典型 N+1：
    一页几百条审批就是上千次单行查询。
    """

    def __init__(self, db: Session, rows: list[Approval]):
        def by_id(model, ids: set[int]):
            if not ids:
                return {}
            return {o.id: o for o in db.scalars(select(model).where(model.id.in_(ids))).all()}

        self.releases = by_id(Release, {r.release_id for r in rows if r.release_id})
        orig_ids = {
            rel.rollback_of_release_id
            for rel in self.releases.values()
            if rel.rollback_of_release_id
        }
        missing = orig_ids - set(self.releases)
        if missing:
            self.releases.update(by_id(Release, missing))
        self.originals = {oid: self.releases[oid] for oid in orig_ids if oid in self.releases}
        rel = self.releases.values()
        self.pipelines = by_id(Pipeline, {r.pipeline_id for r in rel if r.pipeline_id})
        self.groups = by_id(Group, {r.group_id for r in rel if r.group_id})
        self.projects = by_id(
            Project, {p.project_id for p in self.pipelines.values() if p.project_id}
        )
        user_ids = {r.approver_id for r in rows if r.approver_id}
        user_ids |= {r.operator_id for r in rel if r.operator_id}
        self.users = by_id(User, user_ids)

    def user_label(self, user_id: int | None) -> str:
        if not user_id:
            return ""
        u = self.users.get(user_id)
        if u is None:
            return f"#{user_id}"
        return u.display_name or u.username


def _can_decide(db: Session, current: CurrentUser, row: Approval, release: Release | None) -> bool:
    """和 decide() 同一套口径：前端按这个画按钮，避免发起人点了才 403。"""
    if row.status != "pending":
        return False
    if release is None or release.status != "pending":
        return False
    if current.is_admin:
        return True
    from app.modules.approval.service import can_self_approve_release

    if can_self_approve_release(db, current.id, release):
        return True
    if row.approver_id != current.id:
        return False
    return check_permission(db, current, "group", release.group_id, "approve")


def _release_action(release: Release | None) -> tuple[str, str]:
    """审批列表上的操作类型：发布、回滚、Rebuild。"""
    if release is None:
        return "release", "发布"
    trigger = (release.trigger_by or "").strip()
    if trigger == "rollback" or release.rollback_of_release_id:
        return "rollback", "回滚"
    if trigger == "rebuild":
        return "rebuild", "Rebuild"
    return "release", "发布"


def _undo_summary(release: Release | None) -> str:
    """回滚单在创建时写进 plan_json 的一句话，Git 拉不到 commit 时也能看出撤的是什么。"""
    if release is None or not (release.plan_json or "").strip():
        return ""
    try:
        data = json.loads(release.plan_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("summary") or "").strip()


def _headline_for(row: Approval, refs: _Refs, headlines: dict[int, dict]) -> dict:
    """当前单的 commit 说明；回滚单自己没有标题时用被撤销那次的。"""
    info = dict(headlines.get(row.release_id) or {})
    if info.get("title"):
        return info
    release = refs.releases.get(row.release_id)
    orig_id = release.rollback_of_release_id if release is not None else None
    if not orig_id:
        return info
    orig = headlines.get(orig_id) or {}
    return {
        "short_id": info.get("short_id") or orig.get("short_id") or "",
        "title": orig.get("title") or "",
    }


def _to_row(
    row: Approval,
    refs: _Refs,
    *,
    can_decide: bool,
    sibling_rows: list[Approval] | None = None,
    headline: dict | None = None,
) -> dict:
    release = refs.releases.get(row.release_id)
    pipeline = refs.pipelines.get(release.pipeline_id) if release is not None else None
    group = refs.groups.get(release.group_id) if release is not None else None
    project = refs.projects.get(pipeline.project_id) if pipeline is not None else None
    requester_id = release.operator_id if release is not None else None
    from app.modules.approval.service import or_sign_approver_ids

    pool_ids = or_sign_approver_ids(sibling_rows or [row])
    approver_labels = [refs.user_label(uid) for uid in pool_ids]
    approver_text = "、".join(label for label in approver_labels if label)
    action, action_label = _release_action(release)
    original = None
    if release is not None and release.rollback_of_release_id:
        original = refs.originals.get(release.rollback_of_release_id)
    info = headline or {}
    source_ref = release.source_ref if release is not None else ""
    return {
        "id": row.id,
        "release_id": row.release_id,
        "build_number": (release.build_number or release.id) if release is not None else None,
        "approver_id": row.approver_id,
        "approver": approver_text or refs.user_label(row.approver_id),
        "approver_ids": pool_ids,
        "requester_id": requester_id,
        "requester": refs.user_label(requester_id),
        "status": row.status,
        "comment": row.comment,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "pipeline_id": pipeline.id if pipeline is not None else None,
        "pipeline_name": pipeline.name if pipeline is not None else "",
        "project_name": project.name if project is not None else "",
        "group_name": group.name if group is not None else "",
        "group_type": group.type if group is not None else "",
        "release_status": release.status if release is not None else "",
        "version": release.version if release is not None else "",
        "source_ref": source_ref,
        "commit_short": info.get("short_id") or ((source_ref or "")[:8]),
        "commit_title": info.get("title") or "",
        "action": action,
        "action_label": action_label,
        "rollback_of_release_id": release.rollback_of_release_id if release is not None else None,
        "rollback_of_build_number": (
            (original.build_number or original.id) if original is not None else None
        ),
        "undo_summary": _undo_summary(release) if action == "rollback" else "",
        "trigger_by": release.trigger_by if release is not None else "",
        # 只有审批人池里就发起人一个人时才叫自审
        "is_self_approval": bool(requester_id and pool_ids == [requester_id]),
        "can_decide": can_decide,
    }


@router.get("/approvals/pending", summary="审批列表（待我审批 / 我发起的 / 全部）")
def pending_approvals(
    scope: str = Query("pending", description="pending=待我审批, mine=我发起的, all=全部（管理员）"),
    status: str = Query("", description="按审批单状态过滤，留空返回全部"),
    limit: int = Query(200, ge=1, le=1000, description="最多返回多少条（按最新排序）"),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    stmt = select(Approval).order_by(Approval.id.desc())
    if scope == "mine":
        release_ids = select(Release.id).where(Release.operator_id == current.id)
        stmt = stmt.where(Approval.release_id.in_(release_ids))
    elif scope == "all":
        if not current.is_admin:
            raise BizException.forbidden("无权限：查看全部审批记录")
    else:
        from app.modules.approval.service import decidable_pending_query

        stmt = decidable_pending_query(reviewer_id=current.id, is_admin=current.is_admin)

    if status:
        stmt = stmt.where(Approval.status == status)
    # 或签会给每个审批人各写一行，先多取一些再按发布折叠，避免同一张单占满一页
    fetch_limit = min(max(limit * 8, limit), 2000)
    rows = list(db.scalars(stmt.limit(fetch_limit)).all())
    from app.modules.approval.service import approvals_by_release, fold_or_sign_rows

    rows = fold_or_sign_rows(rows, reviewer_id=current.id, limit=limit)
    siblings = approvals_by_release(db, {row.release_id for row in rows})
    ref_rows = [item for group in siblings.values() for item in group] or rows
    refs = _Refs(db, ref_rows)
    from app.modules.pipeline.source_ref_service import headlines_for_releases

    listed = [refs.releases.get(row.release_id) for row in rows]
    try:
        headlines = headlines_for_releases(
            db, [rel for rel in list(refs.releases.values()) + listed if rel is not None]
        )
    except Exception:  # noqa: BLE001
        headlines = {}
    return R.ok(
        [
            _to_row(
                row,
                refs,
                can_decide=_can_decide(db, current, row, refs.releases.get(row.release_id)),
                sibling_rows=siblings.get(row.release_id) or [row],
                headline=_headline_for(row, refs, headlines),
            )
            for row in rows
        ]
    )


@router.post("/approvals/{approval_id}/decide", summary="审批决定")
def decide(
    approval_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    a = db.get(Approval, approval_id)
    if a is None:
        raise BizException.not_found("审批")

    # 权限校验 1：必须是审批人本人（或管理员）
    if not current.is_admin and a.approver_id != current.id:
        raise BizException.forbidden("无权限：该审批单不属于你")

    # 权限校验 2：对该发布所属分组有 approve 权限
    release = db.get(Release, a.release_id)
    if release is not None and not current.is_admin:
        if not check_permission(db, current, "group", release.group_id, "approve"):
            raise BizException.forbidden(f"无权限：审批分组 #{release.group_id} 的发布")
    updated = pipeline_service.approve_release(
        db,
        a.release_id,
        body.get("approved", False),
        body.get("comment", ""),
        reviewer_id=current.id,
    )
    if updated.status == "pending":
        return R.ok(updated, message="技术审批已通过，还要等项目经理确认后才会执行")
    if updated.status != "queued":
        return R.ok(updated)
    # 审批这一步已经落库了，拉起执行是它之后的事，失败不能报成「审批失败」
    updated, err = pipeline_service.try_execute_release(db, updated.id)
    if err:
        return R.ok(updated, message=f"审批已通过，但发布未能启动：{err}")
    return R.ok(updated)
