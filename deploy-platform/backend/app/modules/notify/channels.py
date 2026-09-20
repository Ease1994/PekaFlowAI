"""投递适配器：站内信 / 邮件。新增渠道只改这里。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.notify.models import InAppNotice


def deliver_in_app(
    db: Session,
    user_id: int,
    *,
    title: str,
    content: str,
    kind: str,
    link: str,
    related_type: str,
    related_id: int | None,
    dedupe_key: str,
) -> InAppNotice | None:
    if dedupe_key:
        from sqlalchemy import select

        exist = db.scalar(
            select(InAppNotice.id).where(
                InAppNotice.user_id == user_id,
                InAppNotice.dedupe_key == dedupe_key,
            )
        )
        if exist is not None:
            return None
    row = InAppNotice(
        user_id=user_id,
        title=(title or "")[:128],
        content=content or "",
        kind=(kind or "")[:64],
        link=link or "",
        related_type=related_type or "",
        related_id=related_id,
        dedupe_key=(dedupe_key or "")[:160],
        is_read=False,
    )
    db.add(row)
    return row


def deliver_email(to_addr: str, subject: str, body: str, link: str = "") -> bool:
    from app.modules.notify.mail import send_mail

    return send_mail(to_addr, subject, body, link=link)
