"""项目经理只读技能：工作台、白话进度、稳定性。不给执行权。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai.registry import Skill, register
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pm import service


def _pm_workbench(db: Session, current, params: dict) -> dict:
    data = service.workbench(
        db,
        current_id=current.id,
        is_admin=bool(getattr(current, "is_admin", False)),
        project_id=int(params["project_id"]) if params.get("project_id") else None,
        days=int(params.get("days") or 7),
    )
    # 给模型一份瘦的，避免把日历整表塞进上下文
    return {
        "is_pm": data.get("is_pm"),
        "projects": data.get("projects"),
        "in_progress": data.get("in_progress"),
        "planned": data.get("planned"),
        "pending_mine": data.get("pending_mine"),
        "recent": (data.get("recent") or [])[:15],
        "stability": data.get("stability"),
        "hint": "用 blocker.label 回答「卡在谁」；没写 business_summary 就说尚未填写，不要编。",
    }


def _pm_brief(db: Session, current, params: dict) -> dict:
    from app.core.deps import visible_pipeline_ids

    keyword = str(params.get("keyword") or "").strip().lower()
    scope = visible_pipeline_ids(db, current)
    stmt = select(Release).order_by(Release.id.desc()).limit(20)
    if params.get("release_id"):
        stmt = select(Release).where(Release.id == int(params["release_id"]))
    elif scope is not None:
        if not scope:
            return {"error": "当前看不到任何流水线"}
        stmt = stmt.where(Release.pipeline_id.in_(scope))
    rows = list(db.scalars(stmt).all())
    picked: list[Release] = []
    for r in rows:
        pipe = db.get(Pipeline, r.pipeline_id)
        name = f"{pipe.name if pipe else ''} {r.version or ''} {getattr(r, 'business_summary', '') or ''}"
        if keyword and keyword not in name.lower() and keyword not in str(r.id):
            continue
        picked.append(r)
        if len(picked) >= 5:
            break
    if not picked:
        return {"briefs": [], "hint": "没有匹配的发布"}
    return {
        "briefs": [
            {
                "release_id": r.id,
                "pipeline_id": r.pipeline_id,
                "text": service.brief_text(db, r),
                "blocker": service.current_blocker(db, r),
                "business_summary": getattr(r, "business_summary", "") or "",
            }
            for r in picked
        ]
    }


def _pm_stability(db: Session, current, params: dict) -> dict:
    from app.core.deps import visible_project_ids

    vis = visible_project_ids(db, current)
    if vis is not None and not vis:
        return {"stability": [], "hint": "当前看不到任何项目"}
    project_ids = [int(params["project_id"])] if params.get("project_id") else None
    if project_ids is None:
        project_ids = list(vis) if vis is not None else []
        if not project_ids:
            from app.modules.project.models import Project

            project_ids = list(db.scalars(select(Project.id)).all())
    return {"stability": service.stability(db, project_ids, days=int(params.get("days") or 30))}


def load() -> None:
    register(Skill(
        name="pm_workbench",
        description=(
            "项目经理工作台：进行中、待确认、计划中、最近完成。"
            "在问本周计划、今晚发了没、卡在谁时调用。只读，不要据此去发布。"
        ),
        category="observe",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "days": {"type": "integer"},
            },
        },
        handler=_pm_workbench,
        examples=["本周生产计划", "待我确认的上线", "订单服务今晚发了没", "卡在谁那"],
    ))
    register(Skill(
        name="pm_brief",
        description="用白话同步给业务：发了什么、到哪了、有没有风险。在要同步给业务、用一句话说这次发布时调用。只读已填写的业务说明，不要编。",
        category="observe",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "release_id": {"type": "integer"},
                "keyword": {"type": "string"},
            },
        },
        handler=_pm_brief,
        examples=["用白话说这次失败", "同步给业务一句话", "这次上线怎么跟业务讲"],
    ))
    register(Skill(
        name="pm_stability",
        description="按项目看近 N 天成功、失败、回滚，回答稳不稳。在问项目稳不稳、是不是老在回滚时调用。不是 DORA 英文指标，改用 get_dora_metrics。",
        category="observe",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "days": {"type": "integer"},
            },
        },
        handler=_pm_stability,
        examples=["这个项目最近稳不稳", "是不是老在回滚", "最近老失败吗"],
    ))
