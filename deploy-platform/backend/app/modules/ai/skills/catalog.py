"""目录类技能：项目 / 分组 / 流水线 / 仓库 / 插件。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ai.context import MATCH_TOKEN, match_score, visible_pipelines
from app.modules.ai.registry import Skill, register
from app.modules.pipeline.models import Pipeline
from app.modules.pipeline.service import approval_required_for
from app.modules.project.models import Group, Project
from app.modules.repository.models import Repository
from app.modules.store.models import Plugin

# 一次最多摆给模型多少条流水线。上千条全量塞进上下文，token 预算会把它腰斩成
# 一份「看着像全集」的残表，模型据此挑流水线比不给还糟
_MAX_LISTED_PIPELINES = 40


def _list_projects(db: Session, current, params: dict) -> dict:
    from app.core.deps import visible_project_ids

    from app.core.env import is_platform_project

    vis = visible_project_ids(db, current)
    if vis is None:
        rows = db.scalars(select(Project)).all()
    else:
        rows = db.scalars(select(Project).where(Project.id.in_(vis))).all() if vis else []
    return {
        "projects": [
            {"id": p.id, "name": p.name, "code": p.code}
            for p in rows
            if not is_platform_project(p.code)
        ]
    }


def _list_groups(db: Session, current, params: dict) -> dict:
    from app.core.deps import visible_project_ids

    vis = visible_project_ids(db, current)
    stmt = select(Group)
    if vis is not None:
        if not vis:
            return {"groups": []}
        stmt = stmt.where(Group.project_id.in_(vis))
    if params.get("project_id"):
        stmt = stmt.where(Group.project_id == int(params["project_id"]))
    rows = db.scalars(stmt).all()
    return {"groups": [{"id": g.id, "name": g.name, "type": g.type, "project_id": g.project_id} for g in rows]}


def _list_pipelines(db: Session, current, params: dict) -> dict:
    keyword = str(params.get("keyword") or "").strip().lower()
    group_type = str(params.get("group_type") or "").strip().lower()
    project_kw = str(params.get("project") or "").strip().lower()
    scored: list[tuple[int, dict]] = []
    for p, proj, g in visible_pipelines(db, current):
        if group_type and g and g.type != group_type:
            continue
        proj_name = proj.name if proj else ""
        if project_kw and project_kw not in proj_name.lower():
            continue
        score = 0
        if keyword:
            score = match_score(keyword, p)
            if not score and keyword in f"{proj_name} {g.name if g else ''}".lower():
                # 命中的是项目名/分组名，比命中流水线名弱一档
                score = MATCH_TOKEN
            if not score:
                continue
        scored.append(
            (
                score,
                {
                    "id": p.id,
                    "name": p.name,
                    "project": proj_name,
                    "group": g.name if g else "",
                    "env": g.type if g else "",
                },
            )
        )
    if keyword and scored:
        # 只保留最强的一档：用户说 test-C 时不能把 order-service-test 一起端上来，
        # 模型会顺手拿第一条去发布
        top = max(score for score, _ in scored)
        scored = [(score, item) for score, item in scored if score == top]
    items = [item for _score, item in scored]
    total = len(items)
    if total <= _MAX_LISTED_PIPELINES:
        return {"pipelines": items, "total": total}

    # 超限就别硬塞：全量丢给模型会被 token 预算腰斩成一份「看着像全集」的残表，
    # 用户照着它挑反而更危险。改成给按项目的条数分布，让他顺着收窄再查一次
    by_project: dict[str, int] = {}
    for it in items:
        key = it["project"] or "(未归属项目)"
        by_project[key] = by_project.get(key, 0) + 1
    return {
        "pipelines": items[:_MAX_LISTED_PIPELINES],
        "total": total,
        "by_project": dict(sorted(by_project.items(), key=lambda kv: -kv[1])),
        "hint": (
            f"共 {total} 条，这里只列了前 {_MAX_LISTED_PIPELINES} 条，不是全部。"
            "先把 by_project 的分布告诉用户（各项目多少条），让他用项目名或关键字收窄，"
            "再用 project/keyword 参数查一次。不要把这份残表当全集，也不要替用户挑。"
        ),
    }


def _get_pipeline(db: Session, current, params: dict) -> dict:
    from app.core.deps import check_permission

    pid = int(params.get("pipeline_id") or 0)
    p = db.get(Pipeline, pid)
    if p is None:
        return {"error": f"流水线 #{pid} 不存在"}
    if not check_permission(db, current, "pipeline", pid, "read"):
        return {"error": f"无权限查看流水线 #{pid}"}
    g = db.get(Group, p.group_id)
    proj = db.get(Project, p.project_id)
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "project": proj.name if proj else p.project_id,
        "group": g.name if g else "",
        "env": g.type if g else "",
        "approval_required": bool(g) and approval_required_for(g, p),
        "approval_mode": p.approval_mode or "inherit",
    }


def _list_repositories(db: Session, current, params: dict) -> dict:
    from app.core.deps import visible_project_ids

    vis = visible_project_ids(db, current)
    stmt = select(Repository)
    if vis is not None:
        if not vis:
            return {"repositories": []}
        stmt = stmt.where(Repository.project_id.in_(vis))
    if params.get("project_id"):
        stmt = stmt.where(Repository.project_id == int(params["project_id"]))
    rows = db.scalars(stmt).all()
    return {
        "repositories": [
            {"id": r.id, "name": r.name, "alias": r.alias, "url": r.url, "project_id": r.project_id}
            for r in rows
        ]
    }


def _list_plugins(db: Session, current, params: dict) -> dict:
    rows = db.scalars(select(Plugin).where(Plugin.enabled.is_(True))).all()
    return {"plugins": [{"id": p.id, "name": p.name, "display_name": p.display_name or p.name, "category": p.category} for p in rows[:50]]}


def load() -> None:
    register(Skill(
        name="list_projects",
        description="列出当前用户可见的项目（名称、代号）。在问有哪些项目、项目清单时调用。不是申请执行权，也不是发起发布。",
        category="catalog",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_projects,
        examples=["有哪些项目", "系统里能看到哪些项目", "项目清单"],
    ))
    register(Skill(
        name="list_groups",
        description="列出项目下的环境分组（测试/生产等）。在问这个项目有哪些环境、分了哪些组时调用。不是申请某个环境的执行权。",
        category="catalog",
        risk="read",
        parameters={"type": "object", "properties": {"project_id": {"type": "integer"}}},
        handler=_list_groups,
        examples=["订单项目有哪些环境", "这个项目分了哪些组", "有没有生产分组"],
    ))
    register(Skill(
        name="list_pipelines",
        description=(
            "列出可见流水线目录。在问有哪些流水线、流水线清单时调用，只调一次。"
            "不是发起发布、查状态或申请执行权；点名要跑某一条改用 propose_release。"
            f"一次最多 {_MAX_LISTED_PIPELINES} 条。不要对每条再 get_pipeline。"
        ),
        category="catalog",
        risk="read",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "project": {"type": "string", "description": "按项目名筛选，用于在流水线很多时收窄"},
                "group_type": {
                    "type": "string",
                    "description": "环境码：prod / test / uat / staging / dev，或自定义小写码",
                },
            },
        },
        handler=_list_pipelines,
        examples=["有哪些流水线", "测试环境流水线", "能看到哪些线", "流水线清单"],
    ))
    register(Skill(
        name="get_pipeline",
        description="按 id 查看一条流水线的审批模式和描述。在已有 pipeline_id、要核对单条怎么配时调用。不是列目录（用 list_pipelines），也不是查发布状态。",
        category="catalog",
        risk="read",
        parameters={"type": "object", "properties": {"pipeline_id": {"type": "integer"}}, "required": ["pipeline_id"]},
        handler=_get_pipeline,
        examples=["这条流水线要不要审批", "看一下 12 号流水线怎么配"],
    ))
    register(Skill(
        name="list_repositories",
        description="列出可见代码仓库。在问有哪些代码库、git 仓库、项目绑了哪些仓时调用。不是拉代码，也不是发布。",
        category="catalog",
        risk="read",
        parameters={"type": "object", "properties": {"project_id": {"type": "integer"}}},
        handler=_list_repositories,
        examples=["有哪些代码库", "项目绑定了哪些仓库", "git 仓库清单"],
    ))
    register(Skill(
        name="list_plugins",
        description="列出已安装的流水线步骤插件（编排器里能选的步骤）。在问流水线能用哪些插件、有没有 docker 构建步骤时调用。不是助手技能包，也不是插件草稿。",
        category="catalog",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_plugins,
        examples=["支持哪些插件", "流水线能用哪些步骤插件", "有没有 docker 构建插件"],
    ))
