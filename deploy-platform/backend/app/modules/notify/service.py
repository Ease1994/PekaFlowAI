"""站内通知：写入 MySQL，按用户投递。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.approval.service import resolve_group_approvers
from app.modules.notify.models import InAppNotice


def public(n: InAppNotice, db: Session | None = None) -> dict:
    from app.modules.notify.events import action_path

    # 待审旧数据 link 仍指向执行画布，按种类改写，点铃铛才能进审批页
    href = action_path(n.kind or "", related_id=n.related_id) or n.link or _fallback_link(db, n)
    return {
        "id": n.id,
        "title": n.title,
        "content": n.content,
        "kind": n.kind,
        "link": href,
        "related_type": n.related_type,
        "related_id": n.related_id,
        "is_read": bool(n.is_read),
        "created_at": n.created_at.isoformat() if n.created_at else "",
    }


def _fallback_link(db: Session | None, n: InAppNotice) -> str:
    """没带 link 的发布通知（早期写入的那批）也要能一键直达执行详情。

    否则点「查看详情」只能落到消息详情页，还得再点一次才看得到到底哪步挂了。
    """
    if db is None or not n.related_id or (n.related_type or "") != "release":
        return ""
    from app.modules.pipeline.models import Release

    r = db.get(Release, int(n.related_id))
    return f"/executions/{r.pipeline_id}/{r.id}" if r is not None else ""


def reviewers_of_group(db: Session, group_id: int, exclude_user_id: int | None = None) -> list[int]:
    """管理员 + 直接授权 + 角色继承得到 approve 的用户。"""
    rows = resolve_group_approvers(
        db,
        int(group_id),
        exclude_user_ids={int(exclude_user_id)} if exclude_user_id else None,
    )
    return [u.id for u in rows]


def list_mine(
    db: Session,
    user_id: int,
    unread_only: bool = False,
    read_only: bool = False,
    kind: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = select(InAppNotice).where(InAppNotice.user_id == user_id).order_by(InAppNotice.id.desc())
    if unread_only:
        stmt = stmt.where(InAppNotice.is_read.is_(False))
    elif read_only:
        stmt = stmt.where(InAppNotice.is_read.is_(True))
    k = (kind or "").strip()
    if k:
        stmt = stmt.where(InAppNotice.kind.like(f"{k}%"))
    rows = db.scalars(stmt.offset(max(offset, 0)).limit(min(max(limit, 1), 200))).all()
    return [public(n, db) for n in rows]


def get_one(db: Session, user_id: int, notice_id: int) -> dict | None:
    n = db.get(InAppNotice, notice_id)
    if n is None or n.user_id != user_id:
        return None
    return public(n, db)


def unread_count(db: Session, user_id: int) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(
            select(func.count(InAppNotice.id)).where(
                InAppNotice.user_id == user_id,
                InAppNotice.is_read.is_(False),
            )
        )
        or 0
    )


def mark_read(db: Session, user_id: int, notice_id: int | None = None) -> int:
    stmt = select(InAppNotice).where(InAppNotice.user_id == user_id, InAppNotice.is_read.is_(False))
    if notice_id:
        stmt = stmt.where(InAppNotice.id == int(notice_id))
    n = 0
    for row in db.scalars(stmt).all():
        row.is_read = True
        n += 1
    db.commit()
    return n
