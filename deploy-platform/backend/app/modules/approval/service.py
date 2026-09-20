"""发布审批服务。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import check_permission, warm_permission_cache
from app.modules.approval.models import Approval
from app.modules.auth.models import User
from app.modules.pipeline.models import Release


def resolve_group_approvers(
    db: Session,
    group_id: int,
    *,
    exclude_user_ids: set[int] | None = None,
) -> list[User]:
    """解析某分组的可审批用户。

    统一走 check_permission，让直接授权、项目角色、管理员兜底口径保持一致。
    """
    excluded = exclude_user_ids or set()
    users = db.scalars(
        select(User).where(User.status == "active").order_by(User.is_admin.desc(), User.id)
    ).all()
    # 逐个 check_permission 会各查一次权限表，几百号人就是几百次查询
    warm_permission_cache(db, [u.id for u in users if u.id not in excluded])
    result: list[User] = []
    for user in users:
        if user.id in excluded:
            continue
        if check_permission(db, user, "group", group_id, "approve"):
            result.append(user)
    return result


def pick_release_approvers(
    db: Session,
    *,
    group_id: int,
    requester_id: int,
    allow_self_approval: bool,
) -> list[User]:
    """挑选本次发布的审批人（或签，谁先批谁算）。

    有其他人时先派给他们。分组开了「允许发起人自审」、且发起人自己有审批权，
    也把他加进池子——开关开着却批不了自己的单，和页面上的预期对不上。
    关掉自审时发起人不会进池子。没人可派时返回空，由上层阻断或走应急跳审。
    """
    others = resolve_group_approvers(db, group_id, exclude_user_ids={requester_id})
    me = None
    if allow_self_approval:
        candidate = db.get(User, requester_id)
        if (
            candidate is not None
            and candidate.status == "active"
            and check_permission(db, candidate, "group", group_id, "approve")
        ):
            me = candidate
    if others and me:
        return others + [me]
    if others:
        return others
    if me:
        return [me]
    return []


def can_self_approve_release(db: Session, user_id: int, release: Release | None) -> bool:
    """分组开了自审、发起人自己有审批权时，可以批自己这张还停着的单。

    已经派给别人的待办也算：开关是后来才按这个理解用的，不能让已交的单卡死。
    """
    if release is None or release.operator_id != user_id:
        return False
    from app.modules.project.models import Group

    group = db.get(Group, release.group_id)
    if group is None or not group.allow_self_approval:
        return False
    user = db.get(User, user_id)
    if user is None or user.status != "active":
        return False
    return check_permission(db, user, "group", release.group_id, "approve")


def create_release_approvals(
    db: Session,
    *,
    release_id: int,
    group_id: int,
    requester_id: int,
    allow_self_approval: bool,
) -> list[Approval]:
    approvers = pick_release_approvers(
        db,
        group_id=group_id,
        requester_id=requester_id,
        allow_self_approval=allow_self_approval,
    )
    rows: list[Approval] = []
    for user in approvers:
        row = Approval(release_id=release_id, approver_id=user.id, status="pending", comment="")
        db.add(row)
        rows.append(row)
    return rows


def decidable_pending_query(*, reviewer_id: int, is_admin: bool):
    """构造「待我审批」查询：发布仍停在 pending、审批单也还活着。

    普通人看派给自己的，以及「开了自审且自己发起的」。管理员能代审任意待办。
    """
    from app.modules.project.models import Group

    pending_release_ids = select(Release.id).where(Release.status == "pending")
    stmt = (
        select(Approval)
        .where(
            Approval.status == "pending",
            Approval.release_id.in_(pending_release_ids),
        )
        .order_by(Approval.id.desc())
    )
    if is_admin:
        return stmt
    self_initiated = select(Release.id).where(
        Release.status == "pending",
        Release.operator_id == reviewer_id,
        Release.group_id.in_(select(Group.id).where(Group.allow_self_approval.is_(True))),
    )
    return stmt.where(
        (Approval.approver_id == reviewer_id) | (Approval.release_id.in_(self_initiated))
    )


def list_decidable_pending(
    db: Session,
    *,
    reviewer_id: int,
    is_admin: bool,
    limit: int,
) -> list[Approval]:
    """列出当前用户现在就能批的待办，同一发布只留一张。"""
    raw = list(
        db.scalars(
            decidable_pending_query(reviewer_id=reviewer_id, is_admin=is_admin).limit(max(limit * 8, limit))
        ).all()
    )
    return fold_or_sign_rows(raw, reviewer_id=reviewer_id, limit=limit)


def pending_approval_for_reviewer(
    db: Session,
    *,
    release_id: int,
    reviewer_id: int,
    is_admin: bool,
) -> Approval | None:
    """找到当前人可以处理的那条待办。

    先看派给自己的；没有且是管理员时，改拿该发布上任意一条还活着的待办。
    这样管理员代审和列表上的 can_decide 口径一致。
    """
    mine = db.scalars(
        select(Approval).where(
            Approval.release_id == release_id,
            Approval.approver_id == reviewer_id,
            Approval.status == "pending",
        )
    ).first()
    if mine is not None:
        return mine
    release = db.get(Release, release_id)
    if is_admin or can_self_approve_release(db, reviewer_id, release):
        return db.scalars(
            select(Approval)
            .where(Approval.release_id == release_id, Approval.status == "pending")
            .order_by(Approval.id.asc())
        ).first()
    return None


def cancel_pending_approvals(
    db: Session,
    *,
    release_id: int,
    comment: str = "",
    exclude_approval_id: int | None = None,
) -> None:
    """作废同一发布的其余待办。

    会话是 autoflush=False，已在内存里改过状态的行仍会被 SQL 命中，
    所以这里显式排除已处理的那条，避免把审批结论覆盖成 cancelled。
    """
    rows = db.scalars(
        select(Approval).where(Approval.release_id == release_id, Approval.status == "pending")
    ).all()
    for row in rows:
        if exclude_approval_id is not None and row.id == exclude_approval_id:
            continue
        if row.status != "pending":
            continue
        row.status = "cancelled"
        if comment:
            row.comment = comment


def approvals_by_release(db: Session, release_ids: set[int]) -> dict[int, list[Approval]]:
    """同一发布下的全部审批行，用来拼或签审批人名单。"""
    if not release_ids:
        return {}
    rows = db.scalars(select(Approval).where(Approval.release_id.in_(release_ids))).all()
    grouped: dict[int, list[Approval]] = {}
    for row in rows:
        grouped.setdefault(row.release_id, []).append(row)
    return grouped


def pick_or_sign_row(group: list[Approval], reviewer_id: int) -> Approval:
    """同一发布只给人看一张单。

    或签是「这张待办谁先批谁算」，不是每人一张。优先当前人名下还活着的待办，
    方便点通过时打到自己那条；管理员代审则拿最早的待办。已经有结论时展示那条结论。
    """
    pending = [row for row in group if row.status == "pending"]
    if pending:
        mine = next((row for row in pending if row.approver_id == reviewer_id), None)
        return mine or min(pending, key=lambda row: row.id)
    decided = [row for row in group if row.status in ("approved", "rejected")]
    if decided:
        return max(decided, key=lambda row: (row.approved_at is not None, row.id))
    return max(group, key=lambda row: row.id)


def fold_or_sign_rows(rows: list[Approval], *, reviewer_id: int, limit: int) -> list[Approval]:
    """按发布去重后再截断条数。钉钉/飞书或签也是一张单进待办，不是 N 张重复单。"""
    grouped: dict[int, list[Approval]] = {}
    for row in rows:
        grouped.setdefault(row.release_id, []).append(row)
    picked = [pick_or_sign_row(group, reviewer_id) for group in grouped.values()]
    picked.sort(key=lambda row: row.id, reverse=True)
    return picked[:limit]


def or_sign_approver_ids(siblings: list[Approval]) -> list[int]:
    """这张或签单的审批人池：还没结案时只看待办，结案后看当初派过的所有人。"""
    pending = [row for row in siblings if row.status == "pending"]
    source = pending or siblings
    ids: list[int] = []
    for row in sorted(source, key=lambda item: item.id):
        if row.approver_id and row.approver_id not in ids:
            ids.append(row.approver_id)
    return ids
