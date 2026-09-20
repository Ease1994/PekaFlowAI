"""观测与诊断技能：发布状态、构建机、DORA、失败诊断、待审、执行日志。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.env import is_platform_project
from app.modules.ai.registry import Skill, register
from app.modules.agent.models import BuildAgent
from app.modules.pipeline.models import Pipeline, Release
from app.modules.project.models import Project

# 日志进模型上下文的上限。完整日志走页面，这里只给技能作者够用的一段。
_LOG_CHARS = 6000
_MAX_APPROVALS = 20


def _is_platform_pipeline(db: Session, pipeline: Pipeline | None) -> bool:
    if pipeline is None:
        return False
    proj = db.get(Project, pipeline.project_id)
    return is_platform_project(proj.code if proj else None)


def _scope(db: Session, current) -> set[int] | None:
    """本次查询可见的流水线 id；管理员返回 None 表示不加限制。

    技能和 HTTP 接口必须共用一套可见性口径，否则用户能绕过接口权限从助手读到别人的数据。
    """
    from app.core.deps import visible_pipeline_ids

    return visible_pipeline_ids(db, current)


def _matched_pipeline_ids(db: Session, current, keyword: str) -> list[int]:
    """点名流水线时先解析 id，再按流水线取最近一次。不能先 limit 20 再按名字滤。"""
    from app.modules.ai.context import MATCH_EXACT, match_score, visible_pipelines

    scored: list[tuple[int, int]] = []
    for p, _proj, _g in visible_pipelines(db, current):
        score = match_score(keyword, p)
        if not score and keyword in (p.name or "").lower():
            score = MATCH_EXACT
        if score:
            scored.append((score, p.id))
    if not scored:
        return []
    top = max(score for score, _ in scored)
    return [pid for score, pid in scored if score == top]


def _status_window(params: dict) -> tuple[bool, int]:
    """默认每条流水线只看最近一次。用户要历史才拉开窗口。"""
    raw_limit = params.get("limit")
    if raw_limit not in (None, ""):
        try:
            n = int(raw_limit)
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, 20))
        return n <= 1, n
    latest = params.get("latest")
    if latest is False or str(latest).lower() in {"0", "false", "no"}:
        return False, 10
    if latest is True or str(latest).lower() in {"1", "true", "yes"}:
        return True, 1
    status = str(params.get("status") or "").strip().lower()
    named = bool(params.get("pipeline_id") or str(params.get("keyword") or "").strip())
    if not named and status in {
        "running",
        "in_progress",
        "active",
        "failed",
        "success",
        "cancelled",
        "rejected",
    }:
        return False, 20
    return True, 1


def _get_release_status(db: Session, current, params: dict) -> dict:
    scope = _scope(db, current)
    if scope is not None and not scope:
        return {"releases": [], "hint": "你当前没有任何可见的流水线"}

    keyword = str(params.get("keyword") or "").strip().lower()
    pipeline_ids: list[int] | None = None
    if params.get("pipeline_id"):
        pipeline_ids = [int(params["pipeline_id"])]
    elif keyword:
        pipeline_ids = _matched_pipeline_ids(db, current, keyword)
        if not pipeline_ids:
            return {"releases": [], "hint": f"没有名称对得上「{keyword}」的流水线"}

    filters = []
    if scope is not None:
        filters.append(Release.pipeline_id.in_(scope))
    status = str(params.get("status") or "").strip().lower()
    running = ("pending", "queued", "assigned", "running", "rolling_back")
    if status in {"running", "in_progress", "active"}:
        filters.append(Release.status.in_(running))
    elif status and status != "all":
        filters.append(Release.status == status)
    if params.get("release_id"):
        filters.append(Release.id == int(params["release_id"]))
    elif pipeline_ids is not None:
        filters.append(Release.pipeline_id.in_(pipeline_ids))

    latest_only, limit = _status_window(params)
    if params.get("release_id"):
        stmt = select(Release).where(*filters) if filters else select(Release)
    elif latest_only:
        grouped = select(Release.pipeline_id, func.max(Release.id).label("rid"))
        if filters:
            grouped = grouped.where(*filters)
        sub = grouped.group_by(Release.pipeline_id).subquery()
        stmt = select(Release).where(Release.id.in_(select(sub.c.rid))).order_by(Release.id.desc())
        if pipeline_ids is None:
            stmt = stmt.limit(10)
    else:
        stmt = select(Release)
        if filters:
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(Release.id.desc()).limit(limit)

    rows = db.scalars(stmt).all()
    out = []
    for r in rows:
        p = db.get(Pipeline, r.pipeline_id)
        name = p.name if p else ""
        if _is_platform_pipeline(db, p) and not params.get("pipeline_id") and not params.get("release_id"):
            continue
        out.append(
            {
                "id": r.id,
                "pipeline": name,
                "pipeline_id": r.pipeline_id,
                "status": r.status,
                "version": r.version,
                "source_ref": r.source_ref or "",
            }
        )
    if params.get("release_id") and not out:
        # 不区分“不存在”和“无权限”，避免用编号试探出别人的发布
        return {"releases": [], "error": f"发布 #{params['release_id']} 不存在或你没有查看权限"}
    return {"releases": out}


def _list_failed_releases(db: Session, current, params: dict) -> dict:
    scope = _scope(db, current)
    if scope is not None and not scope:
        return {"releases": [], "hint": "你当前没有任何可见的流水线"}
    stmt = select(Release).where(Release.status == "failed")
    if scope is not None:
        stmt = stmt.where(Release.pipeline_id.in_(scope))
    rows = db.scalars(stmt.order_by(Release.id.desc()).limit(10)).all()
    out = []
    for r in rows:
        p = db.get(Pipeline, r.pipeline_id)
        if _is_platform_pipeline(db, p):
            continue
        out.append({"id": r.id, "pipeline": p.name if p else "", "version": r.version, "status": r.status})
    return {"releases": out}


def _list_agents(db: Session, current, params: dict) -> dict:
    # 与 GET /agents 保持一致：构建机属于基础设施信息，只对管理员开放
    if not current.is_admin:
        return {"error": "构建机信息仅管理员可见，请联系管理员查询"}
    rows = db.scalars(select(BuildAgent).order_by(BuildAgent.id)).all()
    return {
        "agents": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role or "builder",
                "host": a.host or "",
                "os": a.os,
                "status": a.status,
                "tags": a.tags,
            }
            for a in rows
        ]
    }


def _get_dora_metrics(db: Session, current, params: dict) -> dict:
    days = int(params.get("days") or 30)
    since = datetime.now() - timedelta(days=days)
    scope = _scope(db, current)
    if scope is not None and not scope:
        return {"days": days, "deploy_frequency": 0, "success": 0, "failed": 0, "change_failure_rate": 0}

    def count(*conditions) -> int:
        stmt = select(func.count(Release.id)).where(Release.created_at >= since, *conditions)
        if scope is not None:
            stmt = stmt.where(Release.pipeline_id.in_(scope))
        return db.scalar(stmt) or 0

    total = count()
    success = count(Release.status == "success")
    failed = count(Release.status == "failed")
    return {
        "days": days,
        "scope": "全平台" if scope is None else "当前用户可见的流水线",
        "deploy_frequency": total,
        "success": success,
        "failed": failed,
        "change_failure_rate": round(failed / total * 100, 2) if total else 0,
    }


def _diagnose_release(db: Session, current, params: dict) -> dict:
    from app.modules.pipeline.diagnose import diagnose_release

    release_id = int(params.get("release_id") or 0)
    if not release_id:
        return {"error": "缺少 release_id，可先 list_failed_releases"}
    result = diagnose_release(db, release_id, current)
    return {
        "release_id": result["release_id"],
        "pipeline_id": result["pipeline_id"],
        "diagnosis": result["diagnosis"],
        "failed_steps": result["failed_steps"],
        "success_steps": result["success_steps"],
    }


def _list_pending_approvals(db: Session, current, params: dict) -> dict:
    """列出当前用户现在就能批、且发布仍停在 pending 的单子。

    口径和 GET /approvals/pending 的默认 scope 一样：普通人看派给自己的，
    管理员看全部还能审的。通过或驳回仍走 propose_approve，这里不改状态。
    """
    del params
    from app.modules.approval.service import list_decidable_pending

    rows = list_decidable_pending(
        db,
        reviewer_id=current.id,
        is_admin=bool(getattr(current, "is_admin", False)),
        limit=_MAX_APPROVALS,
    )
    out = []
    for row in rows:
        release = db.get(Release, row.release_id)
        pipeline = db.get(Pipeline, release.pipeline_id) if release is not None else None
        out.append(
            {
                "id": row.id,
                "release_id": row.release_id,
                "pipeline": pipeline.name if pipeline else "",
                "pipeline_id": release.pipeline_id if release is not None else None,
                "version": release.version if release is not None else "",
                "status": row.status,
                "requester_id": release.operator_id if release is not None else None,
            }
        )
    return {
        "approvals": out,
        "hint": "" if out else "没有待你审批的发布",
    }


def _get_release_logs(db: Session, current, params: dict) -> dict:
    """读取当前用户可见发布的执行日志，截断后给模型。

    无权限和不存在返回同一句，避免用编号试探。正文上限 _LOG_CHARS，完整日志走执行详情页。
    """
    from app.core.deps import check_permission
    from app.core.response import BizException
    from app.modules.pipeline import service as pipeline_service

    try:
        release_id = int(params.get("release_id") or 0)
    except (TypeError, ValueError):
        release_id = 0
    if not release_id:
        return {"error": "缺少 release_id，可先 get_release_status 或 list_failed_releases"}
    try:
        release = pipeline_service.get_release(db, release_id)
    except BizException:
        return {"error": "发布不存在或你没有查看权限"}
    if not check_permission(db, current, "pipeline", release.pipeline_id, "read"):
        return {"error": "发布不存在或你没有查看权限"}
    data = pipeline_service.get_release_logs(db, release_id)
    logs = str(data.get("logs") or "")
    if len(logs) > _LOG_CHARS:
        logs = logs[:_LOG_CHARS] + "…"
    return {
        "release_id": release.id,
        "status": data.get("status") or release.status,
        "logs": logs,
        "tasks": data.get("tasks") or [],
    }


def load() -> None:
    register(Skill(
        name="get_release_status",
        description=(
            "查询发布跑得怎样。在问状态、发完了没、正在发布有哪些时调用。"
            "不是发起发布，也不是列流水线目录。点名流水线用 keyword，不要先 list_pipelines。"
            "没说要历史则只查最近一次；running=正在发布。"
        ),
        category="observe",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "release_id": {"type": "integer"},
                "pipeline_id": {"type": "integer"},
                "keyword": {"type": "string"},
                "latest": {"type": "boolean", "description": "默认 true，每条流水线只返回最近一次"},
                "limit": {"type": "integer", "description": "只要历史时才传，最大 20"},
                "status": {
                    "type": "string",
                    "description": "不传=最近一次实际状态；running / success / failed / cancelled / rejected",
                },
            },
        },
        handler=_get_release_status,
        examples=["查询发布状态", "现在正在发布的流水线有哪些", "发完了没", "跑得怎么样了", "成功了吗"],
    ))
    register(Skill(
        name="list_failed_releases",
        description="列出最近失败的发布清单。在问哪些发布挂了、失败清单时调用。分析原因用 diagnose_release，不是发起发布。",
        category="observe",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_failed_releases,
        examples=["最近失败的发布", "哪些发布挂了", "失败清单"],
    ))
    register(Skill(
        name="list_agents",
        description="列出构建机及在线状态。在问编译机、构建机还活着吗时调用。这是编译机，不是部署节点；往机器传文件用 list_push_nodes。",
        category="observe",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_agents,
        examples=["构建机在线吗", "编译机有哪些", "agent 还活着吗"],
    ))
    register(Skill(
        name="get_dora_metrics",
        description="查询 DORA 指标（部署频率、变更失败率）。在问部署频率、变更失败率时调用。不是单条发布状态，也不要据此去发布。",
        category="observe",
        risk="read",
        parameters={"type": "object", "properties": {"days": {"type": "integer"}}},
        handler=_get_dora_metrics,
        examples=["最近的 DORA 指标", "部署频率怎么样", "失败率高不高"],
    ))
    register(Skill(
        name="diagnose_release",
        description="对失败发布做诊断，只分析失败步骤日志。在问为什么挂了、失败原因时调用。只要日志原文改用 get_release_logs。",
        category="diagnose",
        risk="read",
        parameters={"type": "object", "properties": {"release_id": {"type": "integer"}}, "required": ["release_id"]},
        handler=_diagnose_release,
        examples=["帮我看看这次失败原因", "为什么挂了", "诊断一下这次发布"],
    ))
    register(Skill(
        name="list_pending_approvals",
        description=(
            "列出当前用户现在就能批、仍停在 pending 的生产发布。"
            "普通人看派给自己的，管理员看全部待办。"
            "在问有没有要我点的发布、待审批的上线时调用。"
            "不是权限申请待审（那个用 list_pending_access_applications）。通过或驳回用 propose_approve。"
        ),
        category="observe",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_pending_approvals,
        examples=["有哪些待我审批的发布", "待审批的发布", "有没有要我点的生产发布"],
    ))
    register(Skill(
        name="get_release_logs",
        description="读取一条发布的执行日志原文（过长截断）。在要看某步输出、失败现场原文时调用。不是诊断结论，改用 diagnose_release。",
        category="observe",
        risk="read",
        parameters={
            "type": "object",
            "properties": {"release_id": {"type": "integer"}},
            "required": ["release_id"],
        },
        handler=_get_release_logs,
        examples=["把这次失败的日志发我", "发布 128 的日志", "某一步的输出"],
    ))
