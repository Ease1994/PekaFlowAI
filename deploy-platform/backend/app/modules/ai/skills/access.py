"""权限申请技能：按项目 / 环境分组 / 单条流水线提交执行权，以及查看、审批、撤销。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.ai.registry import Skill, register
from app.modules.ai.skills.base import card
from app.core.response import BizException


def _scope_label(row: dict) -> str:
    apply_type = str(row.get("apply_type") or "")
    if apply_type == "project_execute":
        return f"{row.get('project_name')} 整项目"
    if apply_type == "group_execute":
        return f"{row.get('project_name')} / {row.get('group_name')}"
    if apply_type == "role":
        return f"{row.get('project_name')} / 角色 {row.get('role_name') or row.get('pipeline_name')}"
    return f"{row.get('project_name')} / {row.get('group_name')} / {row.get('pipeline_name')}"


def _list_access_catalog(db: Session, current, params: dict) -> dict:
    """查可申请范围。命中后把「整项目还是某个环境」写进 reply，本轮直接问用户，禁止再搜。"""
    from app.modules.access.assistant import _ask_project, _ask_scope
    from app.modules.access.service import catalog_for_apply

    data = catalog_for_apply(db, current, str(params.get("keyword") or ""))
    projects = data.get("projects") or []
    if len(projects) == 1:
        data["awaiting"] = "pick_scope"
        data["reply"] = _ask_scope(projects[0])
        data["_ask"] = True
    elif len(projects) > 1:
        data["awaiting"] = "pick_project"
        data["reply"] = _ask_project(projects, "找到多个可能的项目，请指出要申请哪一个：")
        data["_ask"] = True
    else:
        data["reply"] = "没有匹配到可申请的项目。换个名称再说一次，或先问有哪些项目。"
        data["_ask"] = True
    return data


def _actions_param() -> dict:
    """三个申请工具共用的 actions 槽位说明。"""
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "要申请的动作。不传则默认 read,execute。"
            "用户点名编辑/删除/审批/豁免或「全部权限」时传入："
            "read、create、update、delete、execute、approve、approval_exempt。"
        ),
    }


def _grant_summary(row: dict) -> str:
    """申请单拟授文案：有中文标签用中文，否则退回动作码。"""
    return str(row.get("granted_action_labels") or row.get("granted_actions") or "查看、执行")


def _apply_execute(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import apply_execute

    pipeline_id = int(params.get("pipeline_id") or 0)
    if not pipeline_id:
        return {"error": "缺少 pipeline_id。整个项目用 apply_project_execute，某个环境用 apply_group_execute"}
    try:
        row = apply_execute(
            db,
            current,
            pipeline_id,
            str(params.get("reason") or "助手代为申请权限"),
            actions=params.get("actions"),
        )
    except BizException as e:
        return {"error": e.message}
    return {
        "application_id": row["id"],
        "status": row["status"],
        "scope": "pipeline",
        "granted_if_approved": row.get("granted_actions") or "read,execute",
        "reply": (
            f"已提交权限申请 #{row['id']}：{_scope_label(row)}。\n"
            f"拟授：{_grant_summary(row)}。这是送审，通过后才生效。"
            "整项目或某个环境请改用对应的一张申请，不要逐条再交。"
        ),
    }


def _apply_project_execute(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import apply_project_execute

    project_id = int(params.get("project_id") or 0)
    keyword = str(params.get("project") or params.get("keyword") or "").strip()
    if not project_id and not keyword:
        return {"error": "缺少 project_id 或项目名称"}
    try:
        row = apply_project_execute(
            db,
            current,
            project_id=project_id or None,
            keyword=keyword,
            reason=str(params.get("reason") or "助手代为申请该项目全部流水线权限"),
            actions=params.get("actions"),
        )
    except BizException as e:
        return {"error": e.message}
    return {
        "application_id": row["id"],
        "status": row["status"],
        "scope": "project",
        "granted_if_approved": row.get("granted_actions") or "read,execute",
        "reply": (
            f"已提交一张项目级权限申请 #{row['id']}：{row['project_name']} 下全部流水线（含生产）。\n"
            f"拟授：{_grant_summary(row)}。管理员通过后生效。只要测试或生产某一个环境，直接说「只要测试」或「只要生产」。"
        ),
    }


def _apply_group_execute(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import apply_group_execute

    group_id = int(params.get("group_id") or 0)
    project_id = int(params.get("project_id") or 0)
    env = str(params.get("env") or "").strip()
    if not group_id and not (project_id and env):
        return {"error": "申请某个环境需要 group_id，或 project_id + env（test/prod/uat/staging/dev）"}
    try:
        row = apply_group_execute(
            db,
            current,
            group_id=group_id or None,
            project_id=project_id or None,
            env=env,
            reason=str(params.get("reason") or "助手代为申请该环境全部分组流水线权限"),
            actions=params.get("actions"),
        )
    except BizException as e:
        return {"error": e.message}
    return {
        "application_id": row["id"],
        "status": row["status"],
        "scope": "group",
        "granted_if_approved": row.get("granted_actions") or "read,execute",
        "reply": (
            f"已提交一张环境权限申请 #{row['id']}：{row['project_name']} / {row['group_name']}。\n"
            f"拟授：{_grant_summary(row)}。通过后只覆盖这个环境。若要整个项目（含生产），请说「整个项目」。"
        ),
    }


def _apply_project_role(db: Session, current, params: dict) -> dict:
    """申请加入项目角色。点名角色名或 role_id；通过后成为该角色成员。"""
    from app.modules.access.service import apply_role

    role_id = int(params.get("role_id") or 0)
    project_id = int(params.get("project_id") or 0)
    keyword = str(params.get("project") or params.get("keyword") or "").strip()
    role_name = str(params.get("role") or params.get("role_name") or "").strip()
    if not role_id and not ((project_id or keyword) and role_name):
        return {"error": "缺少 role_id，或项目 + 角色名称"}
    try:
        row = apply_role(
            db,
            current,
            role_id=role_id or None,
            project_id=project_id or None,
            keyword=keyword,
            role_name=role_name,
            reason=str(params.get("reason") or "助手代为申请项目角色"),
        )
    except BizException as e:
        return {"error": e.message}
    return {
        "application_id": row["id"],
        "status": row["status"],
        "scope": "role",
        "role_id": row.get("role_id") or 0,
        "reply": (
            f"已提交角色申请 #{row['id']}：{_scope_label(row)}。\n"
            "这是送审，通过后你会成为该角色成员，权限随角色模板走。"
        ),
    }


def _format_application_lines(rows: list[dict], *, mine: bool) -> str:
    """把申请列表收成用户能直接看的几行，避免再让模型复述。"""
    if not rows:
        return "你还没有提交过权限申请。" if mine else "当前没有待你审批的权限申请。"
    title = "你的权限申请：" if mine else "待审批："
    lines = [title]
    for row in rows[:20]:
        who = "" if mine else f"{row.get('applicant')} "
        lines.append(f"- #{row['id']} {who}{_scope_label(row)}  {row.get('status') or 'pending'}")
    return "\n".join(lines)


def _list_my_applications(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import list_applications

    rows = list_applications(db, current, "mine")
    items = [
        {
            "id": r["id"],
            "scope": _scope_label(r),
            "pipeline": r["pipeline_name"],
            "apply_type": r.get("apply_type") or "",
            "status": r["status"],
            "reason": r["reason"],
        }
        for r in rows[:20]
    ]
    return {"applications": items, "reply": _format_application_lines(rows[:20], mine=True)}


def _list_pending_applications(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import list_applications

    rows = list_applications(db, current, "pending")
    items = [
        {
            "id": r["id"],
            "applicant": r["applicant"],
            "scope": _scope_label(r),
            "pipeline": r["pipeline_name"],
            "project": r["project_name"],
            "group": r["group_name"],
            "apply_type": r.get("apply_type") or "",
            "reason": r["reason"],
        }
        for r in rows[:20]
    ]
    return {"applications": items, "reply": _format_application_lines(rows[:20], mine=False)}


def _propose_review(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import _can_review_application, application_public
    from app.modules.auth.models import PermissionApplication

    aid = int(params.get("application_id") or 0)
    approved = bool(params.get("approved", True))
    a = db.get(PermissionApplication, aid)
    if a is None:
        return {"error": f"申请 #{aid} 不存在"}
    if a.status != "pending":
        return {"error": f"申请 #{aid} 当前状态 {a.status}，不可审批"}
    if not _can_review_application(db, current, a):
        return {"error": "无权限审批该申请"}
    info = application_public(db, a)
    return card(
        "confirm_access_approve" if approved else "confirm_access_reject",
        f"{'通过' if approved else '驳回'}申请 #{aid}",
        {"application_id": aid, "approved": approved, "comment": params.get("comment") or ""},
        f"{'通过' if approved else '驳回'} {info['applicant']} 对「{_scope_label(info)}」的权限申请？\n"
        + (
            "通过后加入该角色，权限随角色模板走。"
            if info.get("apply_type") == "role"
            else f"拟授：{_grant_summary(info)}。通过后按拟授落库，审核人可改。"
        ),
    )


def _cancel_application(db: Session, current, params: dict) -> dict:
    from app.modules.access.service import cancel, list_applications

    aid = int(params.get("application_id") or 0)
    if not aid:
        mine = [r for r in list_applications(db, current, "mine") if r.get("status") == "pending"]
        if len(mine) == 1:
            aid = int(mine[0]["id"])
        elif not mine:
            return {"reply": "没有待取消的申请。"}
        else:
            lines = "\n".join(f"- #{r['id']} {_scope_label(r)}" for r in mine[:10])
            return {
                "pending": [{"id": r["id"], "scope": _scope_label(r)} for r in mine[:10]],
                "reply": f"有多张待审申请，请指定要取消哪一张：\n{lines}",
            }
    try:
        row = cancel(db, current, aid)
    except BizException as e:
        return {"error": e.message}
    return {
        "application_id": row["id"],
        "status": row["status"],
        "reply": f"已取消申请 #{row['id']}（{_scope_label(row)}）。",
    }


def load() -> None:
    register(Skill(
        name="list_access_catalog",
        description="列出可申请权限的项目和环境。在要申请但范围未定时调用一次。命中后问用户整项目还是某个环境，不要再搜。不是列流水线，也不是发布。",
        category="access",
        risk="read",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "项目名称关键字"}},
        },
        handler=_list_access_catalog,
        examples=["有哪些项目可以申请执行权限", "我能申请哪些范围"],
    ))
    register(Skill(
        name="apply_project_execute",
        description="整项目权限时调用。一张单覆盖全部环境。默认查看+执行；点名编辑/删除/审批/豁免或全部权限时带 actions。某个环境改用 apply_group_execute；一条线改用 apply_pipeline_execute。不是立刻发布。",
        category="access",
        risk="write",
        parameters={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "project": {"type": "string", "description": "项目名称或代号"},
                "reason": {"type": "string"},
                "actions": _actions_param(),
            },
        },
        handler=_apply_project_execute,
        examples=["申请 DMS 整个项目所有流水线的执行权限", "我要整个项目的全部权限", "开通全部流水线权限"],
    ))
    register(Skill(
        name="apply_group_execute",
        description="某个环境分组权限时调用。一张单覆盖该环境下全部流水线。默认查看+执行；点名其它动作时带 actions。整项目改用 apply_project_execute，单条线改用 apply_pipeline_execute。不是立刻发布。",
        category="access",
        risk="write",
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "integer"},
                "project_id": {"type": "integer"},
                "env": {"type": "string", "description": "test / prod / uat / staging / dev"},
                "reason": {"type": "string"},
                "actions": _actions_param(),
            },
        },
        handler=_apply_group_execute,
        examples=["申请 DMS 测试环境全部流水线执行权限", "只要测试环境的执行权", "开通生产分组权限"],
    ))
    register(Skill(
        name="apply_pipeline_execute",
        description="只要某一条流水线权限时调用。默认查看+执行；点名编辑/删除/审批/全部权限时带 actions。整个项目或某个环境不要循环调用本工具。不是发起发布。",
        category="access",
        risk="write",
        parameters={
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "integer"},
                "reason": {"type": "string"},
                "actions": _actions_param(),
            },
            "required": ["pipeline_id"],
        },
        handler=_apply_execute,
        examples=["申请订单测试流水线的执行权限", "申请这条线的编辑权限", "只要这一条线的全部权限"],
    ))
    register(Skill(
        name="apply_project_role",
        description="用户点名项目角色时调用。通过后写入角色成员。不要改成流水线动作申请。没点名角色时改用 apply_project_execute、apply_group_execute 或 apply_pipeline_execute。",
        category="access",
        risk="write",
        parameters={
            "type": "object",
            "properties": {
                "role_id": {"type": "integer"},
                "project_id": {"type": "integer"},
                "project": {"type": "string", "description": "项目名称或代号"},
                "role": {"type": "string", "description": "角色名称"},
                "reason": {"type": "string"},
            },
        },
        handler=_apply_project_role,
        examples=["申请订单项目的开发者角色", "我要加入测试角色", "申请 DMS 的运维角色"],
    ))
    register(Skill(
        name="list_my_access_applications",
        description="查询本人执行权申请的审批进度。在问申请批了没、我交过的单时调用。不是作废申请，改用 cancel_access_application。",
        category="access",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_my_applications,
        examples=["我的权限申请怎么样了", "申请批了没", "查一下我交过的执行权申请"],
    ))
    register(Skill(
        name="list_pending_access_applications",
        description="列出待我审批的执行权申请。在问谁在等我开权限、待批的执行权时调用。不是待审的生产发布（那个用 list_pending_approvals）。通过或驳回用 propose_review_access。",
        category="access",
        risk="read",
        parameters={"type": "object", "properties": {}},
        handler=_list_pending_applications,
        examples=["有哪些权限申请待审批", "待我批的执行权", "谁在等我开权限", "有哪些待我审批"],
    ))
    register(Skill(
        name="propose_review_access",
        description="对权限申请生成通过/驳回确认卡。在要批准或驳回执行权申请时调用。不会立刻改权限。不是审批生产发布（那个用 propose_approve）。",
        category="access",
        risk="write",
        confirm=True,
        parameters={
            "type": "object",
            "properties": {
                "application_id": {"type": "integer"},
                "approved": {"type": "boolean"},
                "comment": {"type": "string"},
            },
            "required": ["application_id"],
        },
        handler=_propose_review,
        examples=["通过这条权限申请", "批准申请 #12", "驳回这张执行权申请", "同意开权限"],
    ))
    register(Skill(
        name="cancel_access_application",
        description="作废本人尚未通过的执行权申请（收回、撤回、撤销、取消都是同一件事）。在申请不要了、撤销刚才那张单时调用。不是查进度，也不是停掉正在跑的发布（那个用 propose_cancel）。无单号时处理待审，多张则列出。",
        category="access",
        risk="write",
        parameters={
            "type": "object",
            "properties": {
                "application_id": {
                    "type": "integer",
                    "description": "申请单号。没有单号也可以调用，工具会处理当前待审申请。",
                }
            },
        },
        handler=_cancel_application,
        examples=[
            "取消这次权限申请",
            "撤销刚才那张申请",
            "撤回执行权申请",
            "那张申请不要了",
            "作废申请 #12",
        ],
    ))
