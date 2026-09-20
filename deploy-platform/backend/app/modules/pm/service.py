"""项目经理工作台与可选业务确认闸。

硬约束：没开开关、没指定项目经理时，这里的任何函数都不能改变发布状态机。
现有生产审批（含应急跳审、自审）仍由 pipeline.service 自己走完。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.approval.models import Approval
from app.modules.auth.models import Permission, User
from app.modules.deploy.models import DeployRequest
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pm.models import PmDecision, ProjectMember
from app.modules.project.models import Group, Project

ROLE_PM = "pm"

BRIEF_FIELDS = (
    "business_summary",
    "impact_scope",
    "iteration_tag",
    "planned_window",
    "audience",
)
_BRIEF_MAX = {
    "business_summary": 10000,
    "impact_scope": 10000,
    "iteration_tag": 64,
    "planned_window": 128,
    "audience": 256,
}


def _user_label(user: User | None, fallback: int | None = None) -> str:
    if user is None:
        return f"#{fallback}" if fallback else ""
    return user.display_name or user.username


def list_project_pms(db: Session, project_id: int) -> list[User]:
    rows = db.scalars(
        select(ProjectMember).where(
            ProjectMember.project_id == int(project_id),
            ProjectMember.role == ROLE_PM,
        )
    ).all()
    if not rows:
        return []
    users = {
        u.id: u
        for u in db.scalars(
            select(User).where(
                User.id.in_([r.user_id for r in rows]),
                User.status == "active",
            )
        ).all()
    }
    return [users[r.user_id] for r in rows if r.user_id in users]


def is_project_pm(db: Session, user_id: int, project_id: int) -> bool:
    return (
        db.scalar(
            select(ProjectMember.id).where(
                ProjectMember.project_id == int(project_id),
                ProjectMember.user_id == int(user_id),
                ProjectMember.role == ROLE_PM,
            )
        )
        is not None
    )


def pm_project_ids(db: Session, user_id: int) -> list[int]:
    return list(
        db.scalars(
            select(ProjectMember.project_id).where(
                ProjectMember.user_id == int(user_id),
                ProjectMember.role == ROLE_PM,
            )
        ).all()
    )


def confirmation_required(db: Session, project: Project | None, group: Group | None) -> bool:
    """这次发布要不要等项目经理确认。默认 False。"""
    if project is None or group is None:
        return False
    if not bool(getattr(project, "pm_enabled", False)):
        return False
    if not bool(getattr(group, "pm_approval_required", False)):
        return False
    return bool(list_project_pms(db, project.id))


def apply_brief(target, brief: dict | None) -> None:
    if not brief:
        return
    for field in BRIEF_FIELDS:
        if field in brief and brief[field] is not None:
            setattr(target, field, str(brief[field] or "")[: _BRIEF_MAX[field]])
    if "need_user_notice" in brief:
        target.need_user_notice = bool(brief.get("need_user_notice"))


def brief_from_request(req: DeployRequest | None) -> dict:
    if req is None:
        return {}
    return {
        "business_summary": getattr(req, "business_summary", "") or "",
        "impact_scope": getattr(req, "impact_scope", "") or "",
        "iteration_tag": getattr(req, "iteration_tag", "") or "",
        "planned_window": getattr(req, "planned_window", "") or "",
        "audience": getattr(req, "audience", "") or "",
        "need_user_notice": bool(getattr(req, "need_user_notice", False)),
        "deploy_request_id": req.id,
    }


def stamp_release_brief(r: Release, brief: dict | None) -> None:
    if not brief:
        return
    apply_brief(r, brief)
    if brief.get("deploy_request_id"):
        r.deploy_request_id = int(brief["deploy_request_id"])


def open_pm_decisions(
    db: Session,
    r: Release,
    project: Project,
    *,
    operator_id: int,
    action_label: str,
    pipeline_name: str,
) -> list[PmDecision]:
    """登记项目经理待确认，并通知。调用方必须已经确认 confirmation_required。"""
    from app.modules.notify import action_path, emit

    pms = list_project_pms(db, project.id)
    rows: list[PmDecision] = []
    for user in pms:
        row = PmDecision(
            release_id=r.id,
            project_id=project.id,
            reviewer_id=user.id,
            status="pending",
            comment="",
        )
        db.add(row)
        rows.append(row)
    if not rows:
        return []
    db.flush()
    summary = (getattr(r, "business_summary", "") or "").strip() or "（尚未填写业务说明）"
    emit(
        db,
        "pm.confirm.pending",
        user_ids=[u.id for u in pms],
        title=f"待确认上线：{project.name} {pipeline_name}",
        content=(
            f"{action_label}等待项目经理确认范围和窗口\n"
            f"项目：{project.name}\n"
            f"流水线：{pipeline_name}\n"
            f"构建号：#{r.build_number or r.id}\n"
            f"业务说明：{summary}\n"
            f"影响范围：{(getattr(r, 'impact_scope', '') or '—')}\n"
            f"计划窗口：{(getattr(r, 'planned_window', '') or '未约')}"
        ),
        link=action_path("pm.confirm.pending", related_id=r.id),
        related_id=r.id,
        dedupe_key=f"pm.confirm.pending:{r.id}",
    )
    return rows


def apply_pm_gate_on_create(
    db: Session,
    r: Release,
    *,
    project: Project | None,
    group: Group | None,
    operator_id: int,
    action_label: str,
    pipeline_name: str,
    emergency: bool,
    skip_approval: bool,
) -> bool:
    """创建发布后尝试挂上业务确认。返回是否因此要把状态留在 pending。

    应急跳审 / 显式 skip 都不挂。没开开关或没指定经理也不挂。
    这样默认路径和改之前完全一样。
    """
    if emergency or skip_approval:
        if emergency and confirmation_required(db, project, group):
            _notify_pm_bypass(db, r, project, operator_id, action_label, pipeline_name)
        return False
    if not confirmation_required(db, project, group) or project is None:
        return False
    rows = open_pm_decisions(
        db,
        r,
        project,
        operator_id=operator_id,
        action_label=action_label,
        pipeline_name=pipeline_name,
    )
    return bool(rows)


def _notify_pm_bypass(
    db: Session,
    r: Release,
    project: Project | None,
    operator_id: int,
    action_label: str,
    pipeline_name: str,
) -> None:
    if project is None:
        return
    from app.modules.notify import emit

    pms = list_project_pms(db, project.id)
    if not pms:
        return
    emit(
        db,
        "pm.confirm.bypassed",
        user_ids=[u.id for u in pms],
        exclude_user_id=operator_id,
        title=f"应急跳审未走业务确认：{project.name} {pipeline_name}",
        content=(
            f"{action_label}已按应急跳审直接进入队列，项目经理未确认范围。\n"
            f"构建号：#{r.build_number or r.id}"
        ),
        link=f"/executions/{r.pipeline_id}/{r.id}",
        related_id=r.id,
        dedupe_key=f"pm.confirm.bypassed:{r.id}",
    )


def pm_still_blocking(db: Session, release_id: int) -> bool:
    return (
        db.scalar(
            select(PmDecision.id).where(
                PmDecision.release_id == int(release_id),
                PmDecision.status == "pending",
            )
        )
        is not None
    )


def tech_still_blocking(db: Session, release_id: int) -> bool:
    return (
        db.scalar(
            select(Approval.id).where(
                Approval.release_id == int(release_id),
                Approval.status == "pending",
            )
        )
        is not None
    )


def cancel_pending_pm(db: Session, release_id: int, comment: str = "") -> None:
    rows = db.scalars(
        select(PmDecision).where(
            PmDecision.release_id == int(release_id),
            PmDecision.status == "pending",
        )
    ).all()
    for row in rows:
        row.status = "cancelled"
        if comment:
            row.comment = comment


def _ensure_project_read(db: Session, user_id: int, project_id: int) -> None:
    exists = db.scalar(
        select(Permission.id).where(
            Permission.user_id == int(user_id),
            Permission.resource_type == "project",
            Permission.resource_id == int(project_id),
            Permission.action.in_(("read", "*")),
            Permission.effect == "allow",
        )
    )
    if exists is not None:
        return
    db.add(
        Permission(
            user_id=int(user_id),
            resource_type="project",
            resource_id=int(project_id),
            action="read",
            effect="allow",
        )
    )


def set_project_pms(db: Session, project_id: int, user_ids: list[int]) -> list[dict]:
    wanted = {int(uid) for uid in user_ids if uid}
    existing = db.scalars(
        select(ProjectMember).where(
            ProjectMember.project_id == int(project_id),
            ProjectMember.role == ROLE_PM,
        )
    ).all()
    have = {row.user_id: row for row in existing}
    for uid, row in list(have.items()):
        if uid not in wanted:
            db.delete(row)
    for uid in wanted:
        if uid not in have:
            user = db.get(User, uid)
            if user is None or (user.status or "active") != "active":
                raise BizException.bad_request(f"用户 #{uid} 不存在或已禁用")
            db.add(ProjectMember(project_id=int(project_id), user_id=uid, role=ROLE_PM))
            _ensure_project_read(db, uid, project_id)
    db.commit()
    return serialize_members(db, project_id)


def serialize_members(db: Session, project_id: int) -> list[dict]:
    pms = list_project_pms(db, project_id)
    return [
        {
            "user_id": u.id,
            "username": u.username,
            "display_name": _user_label(u),
            "email": u.email or "",
            "wecom_bound": bool(u.wecom_userid),
        }
        for u in pms
    ]


def search_directory(db: Session, keyword: str = "", limit: int = 30) -> list[dict]:
    stmt = select(User).where(User.status == "active").order_by(User.id)
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        stmt = stmt.where(
            or_(
                User.username.like(like),
                User.display_name.like(like),
                User.email.like(like),
            )
        )
    rows = db.scalars(stmt.limit(min(max(limit, 1), 50))).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": _user_label(u),
            "email": u.email or "",
            "wecom_bound": bool(u.wecom_userid),
        }
        for u in rows
    ]


def current_blocker(db: Session, r: Release) -> dict:
    """工作台/助手用的「现在卡在谁」。"""
    status = r.status or ""
    if status == "pending":
        pms = db.scalars(
            select(PmDecision).where(PmDecision.release_id == r.id, PmDecision.status == "pending")
        ).all()
        if pms:
            names = [_user_label(db.get(User, row.reviewer_id), row.reviewer_id) for row in pms]
            return {
                "kind": "pm",
                "label": f"待项目经理确认（{'、'.join(names)}）",
                "owner_ids": [row.reviewer_id for row in pms],
            }
        techs = db.scalars(
            select(Approval).where(Approval.release_id == r.id, Approval.status == "pending")
        ).all()
        if techs:
            names = [_user_label(db.get(User, row.approver_id), row.approver_id) for row in techs]
            return {
                "kind": "tech",
                "label": f"待技术审批（{'、'.join(names)}）",
                "owner_ids": [row.approver_id for row in techs],
            }
        return {"kind": "pending", "label": "待审批", "owner_ids": []}
    if status in ("queued", "assigned"):
        return {"kind": "queue", "label": "排队等待构建机", "owner_ids": []}
    if status in ("running", "rolling_back"):
        step = _current_step_label(r)
        return {"kind": "running", "label": step or "执行中", "owner_ids": []}
    if status in ("failed", "rejected"):
        op = db.get(User, r.operator_id) if r.operator_id else None
        return {
            "kind": "failed",
            "label": f"失败，找{_user_label(op, r.operator_id) or '发起人'}",
            "owner_ids": [r.operator_id] if r.operator_id else [],
        }
    if status == "rolled_back":
        return {"kind": "rolled_back", "label": "已回滚", "owner_ids": []}
    if status == "success":
        return {"kind": "done", "label": "已完成", "owner_ids": []}
    return {"kind": status or "unknown", "label": status or "未知", "owner_ids": []}


def _current_step_label(r: Release) -> str:
    raw = r.step_status or ""
    if not raw or raw == "[]":
        return ""
    try:
        steps = json.loads(raw)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(steps, list):
        return ""
    running = [s for s in steps if isinstance(s, dict) and (s.get("status") or "") in ("running", "assigned")]
    if running:
        return str(running[0].get("name") or running[0].get("plugin") or "执行中")
    failed = [s for s in steps if isinstance(s, dict) and (s.get("status") or "") == "failed"]
    if failed:
        return str(failed[0].get("name") or failed[0].get("plugin") or "失败步骤")
    return ""


def decide_pm(
    db: Session,
    decision_id: int,
    *,
    reviewer_id: int,
    approved: bool,
    comment: str,
    is_admin: bool = False,
) -> Release:
    from app.modules.approval.service import cancel_pending_approvals
    from app.modules.notify import emit
    from app.modules.pipeline.service import (
        RELEASE_PENDING,
        RELEASE_QUEUED,
        RELEASE_REJECTED,
        ensure_pipeline_idle,
        get_release,
    )

    row = db.get(PmDecision, int(decision_id))
    if row is None:
        raise BizException.not_found("业务确认")
    if not is_admin and row.reviewer_id != reviewer_id:
        raise BizException.forbidden("这条确认不归你")
    if row.status != "pending":
        raise BizException.bad_request("这条确认已经处理过了")

    r = get_release(db, row.release_id)
    if r.status != RELEASE_PENDING:
        raise BizException.bad_request(f"当前发布状态 {r.status}，不能再确认")

    note = (comment or "").strip()
    if not approved and not note:
        raise BizException.bad_request("驳回时必须填写原因")

    row.status = "approved" if approved else "rejected"
    row.comment = note
    row.decided_at = datetime.now()

    if approved:
        others = db.scalars(
            select(PmDecision).where(
                PmDecision.release_id == r.id,
                PmDecision.status == "pending",
                PmDecision.id != row.id,
            )
        ).all()
        for other in others:
            other.status = "cancelled"
            other.comment = "已由其他项目经理确认"
        if tech_still_blocking(db, r.id):
            r.status = RELEASE_PENDING
        else:
            ensure_pipeline_idle(db, r.pipeline_id, exclude_release_id=r.id)
            r.status = RELEASE_QUEUED
    else:
        r.status = RELEASE_REJECTED
        cancel_pending_approvals(db, release_id=r.id, comment="项目经理已驳回")
        cancel_pending_pm(db, r.id, comment="该发布已被驳回")

    emit(
        db,
        "pm.confirm.reviewed",
        user_ids=[r.operator_id] if r.operator_id else [],
        title=f"业务确认已{'通过' if approved else '驳回'}：#{r.build_number or r.id}",
        content=(
            f"结果：{'通过' if approved else '驳回'}\n"
            f"意见：{note or '-'}"
        ),
        link=f"/executions/{r.pipeline_id}/{r.id}",
        related_id=r.id,
        dedupe_key=f"pm.confirm.reviewed:{r.id}:{'approved' if approved else 'rejected'}",
    )
    db.commit()
    db.refresh(r)
    return r


def draft_announcement(db: Session, r: Release) -> str:
    pipe = db.get(Pipeline, r.pipeline_id)
    group = db.get(Group, r.group_id) if r.group_id else None
    project = db.get(Project, pipe.project_id) if pipe else None
    summary = (getattr(r, "business_summary", "") or "").strip() or (r.version or "（未填写业务说明）")
    impact = (getattr(r, "impact_scope", "") or "").strip() or "—"
    audience = (getattr(r, "audience", "") or "").strip() or "相关业务方"
    notice = "请通知用户。" if getattr(r, "need_user_notice", False) else "本次无需通知最终用户。"
    status_word = {
        "success": "已上线",
        "rolled_back": "已回滚",
        "failed": "发布失败",
        "running": "正在发布",
    }.get(r.status or "", r.status or "")
    return (
        f"【上线通报】{project.name if project else '项目'} / {group.name if group else '环境'}\n"
        f"状态：{status_word}\n"
        f"构建：#{r.build_number or r.id}  版本：{r.version or '-'}\n"
        f"发了什么：{summary}\n"
        f"影响谁：{audience}；范围：{impact}\n"
        f"{notice}"
    )


def announce_release(
    db: Session,
    r: Release,
    *,
    operator_id: int,
    text: str = "",
    extra_user_ids: list[int] | None = None,
) -> str:
    from app.modules.notify import emit

    body = (text or "").strip() or draft_announcement(db, r)
    r.announcement_text = body
    r.announced_at = datetime.now()
    r.announced_by = operator_id
    recipients = set(extra_user_ids or [])
    if r.operator_id:
        recipients.add(int(r.operator_id))
    pipe = db.get(Pipeline, r.pipeline_id)
    if pipe is not None:
        recipients.update(u.id for u in list_project_pms(db, pipe.project_id))
    # 先落通报正文。emit 在没有收件人时会直接返回，不能把这次提交绑在它身上
    db.commit()
    emit(
        db,
        "release.announced",
        user_ids=sorted(recipients),
        title=f"上线通报：#{r.build_number or r.id}",
        content=body,
        link=f"/executions/{r.pipeline_id}/{r.id}",
        related_id=r.id,
        dedupe_key=f"release.announced:{r.id}",
        commit=True,
    )
    return body


def serialize_release_card(db: Session, r: Release) -> dict:
    pipe = db.get(Pipeline, r.pipeline_id)
    group = db.get(Group, r.group_id) if r.group_id else None
    project = db.get(Project, pipe.project_id) if pipe else None
    operator = db.get(User, r.operator_id) if r.operator_id else None
    blocker = current_blocker(db, r)
    return {
        "id": r.id,
        "pipeline_id": r.pipeline_id,
        "pipeline_name": pipe.name if pipe else f"#{r.pipeline_id}",
        "project_id": pipe.project_id if pipe else None,
        "project_name": project.name if project else "",
        "group_id": r.group_id,
        "group_name": group.name if group else "",
        "group_type": group.type if group else "",
        "build_number": r.build_number or r.id,
        "version": r.version or "",
        "source_ref": r.source_ref or "",
        "status": r.status,
        "trigger_by": r.trigger_by,
        "operator_id": r.operator_id,
        "operator_name": _user_label(operator, r.operator_id),
        "business_summary": getattr(r, "business_summary", "") or "",
        "impact_scope": getattr(r, "impact_scope", "") or "",
        "iteration_tag": getattr(r, "iteration_tag", "") or "",
        "planned_window": getattr(r, "planned_window", "") or "",
        "audience": getattr(r, "audience", "") or "",
        "need_user_notice": bool(getattr(r, "need_user_notice", False)),
        "announced_at": r.announced_at.isoformat() if getattr(r, "announced_at", None) else "",
        "blocker": blocker,
        "pm_blocking": pm_still_blocking(db, r.id),
        "tech_blocking": tech_still_blocking(db, r.id),
        "created_at": r.created_at.isoformat() if r.created_at else "",
        "started_at": r.started_at.isoformat() if r.started_at else "",
        "finished_at": r.finished_at.isoformat() if r.finished_at else "",
        "error_message": r.error_message or "",
        "change_window": getattr(group, "change_window", None) or "",
    }


def serialize_decision(db: Session, row: PmDecision) -> dict:
    r = db.get(Release, row.release_id)
    card = serialize_release_card(db, r) if r is not None else {}
    reviewer = db.get(User, row.reviewer_id)
    return {
        "id": row.id,
        "release_id": row.release_id,
        "project_id": row.project_id,
        "reviewer_id": row.reviewer_id,
        "reviewer": _user_label(reviewer, row.reviewer_id),
        "status": row.status,
        "comment": row.comment or "",
        "decided_at": row.decided_at.isoformat() if row.decided_at else "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "release": card,
    }


def list_decisions(
    db: Session,
    *,
    user_id: int,
    scope: str = "pending",
    is_admin: bool = False,
    limit: int = 200,
) -> list[dict]:
    stmt = select(PmDecision).order_by(PmDecision.id.desc())
    if scope == "pending":
        stmt = stmt.where(
            PmDecision.reviewer_id == user_id,
            PmDecision.status == "pending",
            PmDecision.release_id.in_(select(Release.id).where(Release.status == "pending")),
        )
    elif scope == "mine":
        stmt = stmt.where(PmDecision.reviewer_id == user_id)
    elif scope == "all":
        if not is_admin:
            mine_projects = pm_project_ids(db, user_id)
            if not mine_projects:
                return []
            stmt = stmt.where(PmDecision.project_id.in_(mine_projects))
    else:
        stmt = stmt.where(PmDecision.reviewer_id == user_id, PmDecision.status == "pending")
    rows = list(db.scalars(stmt.limit(min(max(limit, 1), 500))).all())
    return [serialize_decision(db, row) for row in rows]


def workbench(
    db: Session,
    *,
    current_id: int,
    is_admin: bool,
    project_id: int | None = None,
    days: int = 7,
) -> dict:
    from app.core.deps import visible_project_ids

    vis = visible_project_ids(db, type("U", (), {"id": current_id, "is_admin": is_admin})())
    if project_id:
        if vis is not None and int(project_id) not in vis:
            raise BizException.forbidden("无权查看该项目")
        project_ids = [int(project_id)]
    elif vis is None:
        project_ids = list(db.scalars(select(Project.id)).all())
    else:
        project_ids = sorted(vis)
    if not project_ids:
        return {
            "projects": [],
            "in_progress": [],
            "planned": [],
            "recent": [],
            "calendar": [],
            "stability": [],
            "pending_mine": [],
            "is_pm": False,
            "pm_project_ids": [],
        }

    pipes = db.execute(
        select(Pipeline.id, Pipeline.project_id).where(Pipeline.project_id.in_(project_ids))
    ).all()
    pipe_ids = [pid for pid, _ in pipes]
    since = datetime.now() - timedelta(days=max(days, 1))

    in_progress: list[Release] = []
    recent: list[Release] = []
    if pipe_ids:
        in_progress = list(
            db.scalars(
                select(Release)
                .where(
                    Release.pipeline_id.in_(pipe_ids),
                    Release.status.in_(
                        ("pending", "queued", "assigned", "running", "rolling_back")
                    ),
                )
                .order_by(Release.id.desc())
                .limit(80)
            ).all()
        )
        recent = list(
            db.scalars(
                select(Release)
                .where(
                    Release.pipeline_id.in_(pipe_ids),
                    Release.status.in_(("success", "failed", "rolled_back", "rejected", "cancelled")),
                    Release.created_at >= since,
                )
                .order_by(Release.id.desc())
                .limit(80)
            ).all()
        )

    planned_reqs = list(
        db.scalars(
            select(DeployRequest)
            .where(
                DeployRequest.project_id.in_(project_ids),
                DeployRequest.status.in_(("draft", "submitted")),
            )
            .order_by(DeployRequest.id.desc())
            .limit(80)
        ).all()
    )
    planned = []
    for req in planned_reqs:
        proj = db.get(Project, req.project_id)
        pipe = db.get(Pipeline, req.pipeline_id) if req.pipeline_id else None
        planned.append(
            {
                "id": req.id,
                "kind": "deploy_request",
                "title": req.title,
                "project_id": req.project_id,
                "project_name": proj.name if proj else "",
                "pipeline_id": req.pipeline_id,
                "pipeline_name": pipe.name if pipe else "",
                "business_summary": getattr(req, "business_summary", "") or req.changelog or "",
                "iteration_tag": getattr(req, "iteration_tag", "") or "",
                "planned_window": getattr(req, "planned_window", "") or "",
                "status": req.status,
                "created_at": req.created_at.isoformat() if req.created_at else "",
            }
        )

    projects = db.scalars(select(Project).where(Project.id.in_(project_ids)).order_by(Project.id)).all()
    my_pm = set(pm_project_ids(db, current_id))
    return {
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "code": p.code,
                "pm_enabled": bool(getattr(p, "pm_enabled", False)),
                "is_pm": p.id in my_pm,
            }
            for p in projects
        ],
        "in_progress": [serialize_release_card(db, r) for r in in_progress],
        "planned": planned,
        "recent": [serialize_release_card(db, r) for r in recent],
        "calendar": _calendar_items(db, project_ids, in_progress + recent, planned_reqs),
        "stability": stability(db, project_ids, days=30),
        "pending_mine": list_decisions(db, user_id=current_id, scope="pending", is_admin=False),
        "is_pm": bool(my_pm),
        "pm_project_ids": sorted(my_pm),
    }


def _calendar_items(
    db: Session,
    project_ids: list[int],
    releases: list[Release],
    requests: list[DeployRequest],
) -> list[dict]:
    items: list[dict] = []
    for r in releases:
        card = serialize_release_card(db, r)
        items.append(
            {
                "id": f"r-{r.id}",
                "date": _calendar_day(getattr(r, "planned_window", "") or "", r.created_at),
                "title": f"{card['project_name']} {card['pipeline_name']} #{card['build_number']}",
                "env": card["group_type"],
                "status": r.status,
                "window": r.planned_window or card.get("change_window") or "",
                "kind": "release",
                "release_id": r.id,
                "pipeline_id": r.pipeline_id,
            }
        )
    for req in requests:
        proj = db.get(Project, req.project_id)
        day = _calendar_day(getattr(req, "planned_window", "") or "", req.created_at)
        items.append(
            {
                "id": f"q-{req.id}",
                "date": day,
                "title": f"{proj.name if proj else ''} {req.title}",
                "env": "",
                "status": req.status,
                "window": getattr(req, "planned_window", "") or "",
                "kind": "request",
                "request_id": req.id,
            }
        )
    groups = db.scalars(select(Group).where(Group.project_id.in_(project_ids))).all()
    for g in groups:
        window = (getattr(g, "change_window", None) or "").strip()
        if not window:
            continue
        proj = db.get(Project, g.project_id)
        items.append(
            {
                "id": f"w-{g.id}",
                "date": "",
                "title": f"{proj.name if proj else ''} / {g.name} 窗口",
                "env": g.type,
                "status": "window",
                "window": window,
                "kind": "window",
            }
        )
    return items


def _calendar_day(window: str, fallback: datetime | None) -> str:
    raw = (window or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return fallback.date().isoformat() if fallback else ""


def stability(db: Session, project_ids: list[int], *, days: int = 30) -> list[dict]:
    """按项目汇总近 N 天发布次数。COUNT 在 SQL 里做，不把 Release 整行拉进内存。"""
    if not project_ids:
        return []
    since = datetime.now() - timedelta(days=max(days, 1))
    by_project: dict[int, list[int]] = {}
    for pid, project_id in db.execute(
        select(Pipeline.id, Pipeline.project_id).where(Pipeline.project_id.in_(project_ids))
    ).all():
        by_project.setdefault(project_id, []).append(pid)
    projects = {
        p.id: p
        for p in db.scalars(select(Project).where(Project.id.in_(project_ids))).all()
    }
    out = []
    for pid, pipe_ids in by_project.items():
        proj = projects.get(pid)
        counts = dict(
            db.execute(
                select(Release.status, func.count(Release.id))
                .where(Release.pipeline_id.in_(pipe_ids), Release.created_at >= since)
                .group_by(Release.status)
            ).all()
        )
        total = sum(counts.values())
        success = int(counts.get("success") or 0)
        failed = int(counts.get("failed") or 0)
        rolled = int(counts.get("rolled_back") or 0)
        out.append(
            {
                "project_id": pid,
                "project_name": proj.name if proj else f"#{pid}",
                "days": days,
                "total": total,
                "success": success,
                "failed": failed,
                "rolled_back": rolled,
                "success_rate": round((success / total) * 100, 1) if total else 0,
                "plain": (
                    f"近 {days} 天发了 {total} 次，成功 {success} 次"
                    + (f"，回滚 {rolled} 次" if rolled else "")
                    + (f"，失败 {failed} 次" if failed else "")
                    + ("。整体还算稳。" if total and rolled == 0 and failed <= 1 else "")
                    + ("。回滚偏多，建议复盘窗口和范围。" if rolled >= 2 else "")
                ),
            }
        )
    out.sort(key=lambda x: x["project_id"])
    return out


def brief_text(db: Session, r: Release) -> str:
    """给业务同步的三句话。"""
    card = serialize_release_card(db, r)
    change = card["business_summary"] or card["version"] or "未写业务说明"
    where = card["blocker"]["label"]
    risk = "可回滚" if r.status in ("success", "failed", "running") else "尚未执行"
    if r.status == "rolled_back":
        risk = "已经回滚"
    return (
        f"{card['project_name']} {card['pipeline_name']} 这次要说的是：{change}。"
        f"现在：{where}。"
        f"影响 {card['impact_scope'] or '未写'}，{risk}。"
    )


def update_release_brief(db: Session, r: Release, body: dict) -> Release:
    apply_brief(r, body)
    db.commit()
    db.refresh(r)
    return r
