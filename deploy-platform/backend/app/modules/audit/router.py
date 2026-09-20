"""审计路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_admin
from app.core.response import R
from app.db.session import get_db
from app.modules.audit import service as audit_service
from app.modules.audit.models import AuditLog

router = APIRouter(tags=["审计日志"])


@router.get("/audit-logs", summary="审计日志查询")
def list_audit_logs(
    action: str | None = None,
    source: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    """分页返回。原先固定取最近 500 条且没有翻页，再往前的记录查不到——
    审计恰恰是要能往回翻的。"""
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if source:
        stmt = stmt.where(AuditLog.source == source)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return R.ok({
        "items": list(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
        "retention_days": audit_service.retention_days_from_settings(db),
    })
