"""指标路由（DORA 四项核心指标）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_admin
from app.core.response import R
from app.db.session import get_db
from app.modules.pipeline.models import Release

router = APIRouter(tags=["指标大盘"])


@router.get("/metrics/dora", summary="DORA 指标概览")
def dora_metrics(days: int = 30, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_admin)):
    # 每个统计都要带上这个时间窗。漏掉就会拿全历史的数字冒充「近 N 天」，
    # 平台跑得越久偏得越离谱，而且看不出来——数字本身长得很正常
    since = datetime.now() - timedelta(days=days)
    window = Release.created_at >= since

    total = db.scalar(select(func.count(Release.id)).where(window))
    success = db.scalar(
        select(func.count(Release.id)).where(window, Release.status == "success")
    )
    failed = db.scalar(select(func.count(Release.id)).where(window, Release.status == "failed"))
    rolled_back = db.scalar(
        select(func.count(Release.id)).where(window, Release.trigger_by == "rollback")
    )

    # 变更前置时间：只取三个时间列，不把快照/计划拉进内存
    times = db.execute(
        select(Release.finished_at, Release.started_at, Release.created_at).where(
            window, Release.finished_at.isnot(None)
        )
    ).all()
    lead_times = [
        (finished - (started or created)).total_seconds() / 60
        for finished, started, created in times
        if finished and (started or created)
    ]
    avg_lead_minutes = round(sum(lead_times) / len(lead_times), 1) if lead_times else 0

    return R.ok({
        "deploy_frequency": total or 0,                    # 部署频率（近 N 天发布次数）
        "change_failure_rate": round(failed / total * 100, 2) if total else 0,  # 变更失败率
        "change_lead_time_minutes": avg_lead_minutes,      # 变更前置时间
        "mttr_minutes": avg_lead_minutes,                  # 平均恢复时间（简化）
        "rollback_count": rolled_back or 0,
        "success_rate": round(success / total * 100, 2) if total else 0,
        "window_days": days,
    })


@router.get("/metrics/trend", summary="发布趋势（按天）")
def release_trend(days: int = 14, db: Session = Depends(get_db), _: CurrentUser = Depends(get_current_admin)):
    since = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(Release.created_at, Release.status).where(Release.created_at >= since)
    ).all()

    buckets: dict[str, dict] = {}
    for created_at, status in rows:
        if not created_at:
            continue
        day = created_at.strftime("%m-%d")
        buckets.setdefault(day, {"success": 0, "failed": 0, "total": 0})
        buckets[day]["total"] += 1
        if status == "success":
            buckets[day]["success"] += 1
        elif status == "failed":
            buckets[day]["failed"] += 1

    return R.ok([
        {"date": k, **v} for k, v in sorted(buckets.items())
    ])
