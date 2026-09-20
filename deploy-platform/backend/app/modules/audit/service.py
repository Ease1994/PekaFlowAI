"""操作审计：写入 MySQL audit_log。操作人永远是当前用户，source 区分 AI/手动/API/定时。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.audit.context import (
    current_ip,
    current_skill,
    current_source,
    current_user_id,
    current_username,
)
from app.modules.audit.models import AuditLog

# 默认 / 下限 / 上限：太短会把排障线索清掉，太长会把备份和查询拖垮
RETENTION_DAYS = 180
RETENTION_DAYS_MIN = 30
RETENTION_DAYS_MAX = 3650
_PURGE_BATCH = 5000


def clamp_retention_days(raw: object) -> int:
    """审计保留天数夹到 30～3650；非法输入用默认 180。"""
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        days = RETENTION_DAYS
    return max(RETENTION_DAYS_MIN, min(RETENTION_DAYS_MAX, days))


def retention_days_from_settings(db: Session) -> int:
    """读平台设置里的审计保留天数，供查询页展示和定时清理共用。"""
    from app.modules.settings import get_setting

    return clamp_retention_days(get_setting(db, "audit_retention_days", str(RETENTION_DAYS)))


def purge_expired(db: Session, retention_days: int | None = None) -> int:
    """删掉超过保留期的审计记录，返回删除条数。分批删，避免一条大事务锁住整张表。

    未显式传入天数时，用平台设置；定时任务走这条路径，改设置后下一次清理即生效。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import delete, select

    days = clamp_retention_days(
        retention_days if retention_days is not None else retention_days_from_settings(db)
    )
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    while True:
        ids = db.scalars(
            select(AuditLog.id).where(AuditLog.created_at < cutoff).limit(_PURGE_BATCH)
        ).all()
        if not ids:
            break
        db.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
        db.commit()
        removed += len(ids)
        if len(ids) < _PURGE_BATCH:
            break
    return removed


def write(
    db: Session,
    action: str,
    resource_type: str = "",
    resource_id: int | None = None,
    detail: str = "",
    *,
    user_id: int | None = None,
    username: str | None = None,
    source: str | None = None,
) -> None:
    """追加一条审计（不 commit，由调用方随业务事务提交）。"""
    src = source or current_source() or "web"
    uid = user_id if user_id is not None else current_user_id()
    name = username if username is not None else current_username()
    if not name and uid:
        from app.modules.auth.models import User

        u = db.get(User, uid)
        name = u.username if u else f"user#{uid}"
    skill = current_skill()
    extra = []
    if src == "ai":
        extra.append("via=ai")
    if skill:
        extra.append(f"skill={skill}")
    if extra:
        suffix = "[" + " ".join(extra) + "]"
        detail = f"{detail} {suffix}".strip() if detail else suffix
    db.add(
        AuditLog(
            user_id=uid,
            username=name or "",
            action=action,
            resource_type=resource_type or "",
            resource_id=resource_id,
            detail=detail or "",
            source=src,
            ip=current_ip(),
        )
    )
