"""系统依赖健康状态。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import R
from app.db.session import get_db
from app.modules.health.service import probe_deps

router = APIRouter(tags=["系统"])


@router.get("/health/deps", summary="MySQL / Redis / ES 依赖状态（顶栏告警）")
def health_deps(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
):
    """登录用户可看。依赖挂了仍返回 200，状态写在 data 里，避免轮询刷错误弹窗。"""
    return R.ok(probe_deps(db))
