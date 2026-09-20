"""会话工作记忆（对齐 DeepSeek Harness 的 session projection：结构化状态，不靠解析上一句散文）。"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.modules.ai.models import AiConversation


def get_working(db: Session, conversation_id: int) -> dict:
    row = db.get(AiConversation, conversation_id)
    if row is None:
        return {}
    try:
        data = json.loads(row.working_json or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def set_working(db: Session, conversation_id: int, data: dict) -> dict:
    row = db.get(AiConversation, conversation_id)
    if row is None:
        return data
    row.working_json = json.dumps(data or {}, ensure_ascii=False)
    db.commit()
    return data


def absorb_traces(working: dict, traces: list | None) -> dict:
    """把一轮工具结果写进工作记忆。直接路径不经过模型循环，也要靠这个接上下一句。"""
    state = dict(working or {})
    for item in traces or []:
        result = item.get("result")
        state = absorb_tool(
            state,
            str(item.get("name") or ""),
            item.get("arguments") or {},
            result if isinstance(result, dict) else {},
        )
    return state


def absorb_tool(working: dict, name: str, args: dict, result: dict) -> dict:
    """工具结果写入投影，供下一轮提示词和短回复续接使用。"""
    working = dict(working or {})
    args = args or {}
    result = result if isinstance(result, dict) else {"result": result}

    if name == "list_access_catalog":
        projects = result.get("projects") or []
        working["goal"] = "access.apply"
        working["catalog_projects"] = [
            {
                "id": p.get("id"),
                "name": p.get("name") or "",
                "code": p.get("code") or "",
                "groups": p.get("groups") or [],
                "roles": p.get("roles") or [],
            }
            for p in projects[:12]
            if p.get("id")
        ]
        working["catalog"] = []
        working["selected_pipeline_id"] = None
        awaiting = str(result.get("awaiting") or "")
        if awaiting in {"pick_scope", "pick_project", "pick_role"}:
            working["awaiting"] = awaiting
        elif len(projects) == 1:
            working["awaiting"] = "pick_scope"
        else:
            working["awaiting"] = "pick_project"
        if len(projects) == 1:
            working["selected_project_id"] = projects[0].get("id")
            working["catalog_groups"] = projects[0].get("groups") or []
        if result.get("access_kind"):
            working["access_kind"] = result.get("access_kind")
        if "pending_env" in result:
            working["pending_env"] = result.get("pending_env") or ""
        if result.get("requested_actions"):
            working["requested_actions"] = result.get("requested_actions")
    elif name == "list_pipelines":
        pipes = result.get("pipelines") or []
        working["catalog"] = [
            {
                "id": p.get("id"),
                "name": p.get("name") or "",
                "project": p.get("project") or "",
                "group": p.get("group") or "",
                "env": p.get("env") or "",
            }
            for p in pipes[:12]
            if p.get("id")
        ]
        working["pipeline_total"] = result.get("total") or len(working["catalog"])
    elif name == "apply_pipeline_execute":
        if not result.get("error"):
            working["goal"] = "access.apply"
            working["awaiting"] = None
            working["selected_pipeline_id"] = args.get("pipeline_id")
            working["last_application_id"] = result.get("application_id")
        else:
            # 申请没成，最常见的是「你已经有权限了」——这种再申请多少次都是同样结果。
            # 还挂着 pick_pipeline 的话，用户下一句会被续接逻辑再拽去申请一遍，
            # 于是同一条错误反复出现。清掉等待状态，让下一轮重新判断意图
            working["awaiting"] = None
    elif name == "apply_project_execute":
        working["goal"] = "access.apply"
        working["awaiting"] = None
        working["last_application_id"] = result.get("application_id")
        working["pending_env"] = ""
        working["access_kind"] = None
    elif name == "apply_group_execute":
        working["goal"] = "access.apply"
        working["awaiting"] = None
        working["last_application_id"] = result.get("application_id")
        working["pending_env"] = ""
        working["access_kind"] = None
    elif name == "apply_project_role":
        working["goal"] = "access.apply"
        working["awaiting"] = None
        working["last_application_id"] = result.get("application_id")
        working["access_kind"] = None
    elif name in ("propose_release", "propose_rollback", "propose_rebuild", "propose_cancel", "propose_approve"):
        working["goal"] = name.replace("propose_", "")
        working["awaiting"] = "confirm_card"
        working["pipeline_id"] = args.get("pipeline_id")
        working["release_id"] = args.get("release_id")
    elif name == "propose_review_access":
        working["goal"] = "access.review"
        working["awaiting"] = "confirm_card"
        working["application_id"] = args.get("application_id")
    return working


def render_working(working: dict) -> str:
    if not working:
        return ""
    lines = ["会话投影优先于口头复述。"]
    goal = working.get("goal") or ""
    awaiting = working.get("awaiting")
    if goal:
        lines.append(f"目标：{goal}")
    if awaiting:
        lines.append(f"等待：{awaiting}")
    projects = working.get("catalog_projects") or []
    if awaiting == "pick_project" and projects:
        lines.append("待选项目：")
        for i, p in enumerate(projects, 1):
            lines.append(f"{i}. #{p.get('id')} {p.get('name')}")
        lines.append("用户指出项目编号或全称后按上一轮范围提交，不要列流水线。")
    if awaiting == "pick_scope":
        lines.append(
            f"项目已定为 #{working.get('selected_project_id')}。"
            "用户说「整个项目」即 apply_project_execute；说「只要测试/生产」即 apply_group_execute。"
        )
    if awaiting == "pick_role":
        lines.append(
            f"项目已定为 #{working.get('selected_project_id')}。"
            "用户指出角色名称后立刻 apply_project_role。"
        )
    catalog = working.get("catalog") or []
    if catalog and awaiting == "pick_pipeline":
        lines.append("可申请流水线（按列出顺序，用户说第N个即第N行）：")
        for i, p in enumerate(catalog, 1):
            lines.append(f"{i}. #{p.get('id')} {p.get('name')}")
        lines.append(
            "用户指定 ID / 名称 / 「第N个」/「确认」时立刻 apply_pipeline_execute，不要再问是否申请这条。"
        )
    if working.get("selected_pipeline_id") and not awaiting:
        lines.append(
            f"最近申请的 pipeline_id={working.get('selected_pipeline_id')} "
            f"application=#{working.get('last_application_id')}"
        )
    if awaiting == "confirm_card":
        lines.append("上一轮已给出确认卡片。用户说确认/好的即执行卡片，不要反问确认什么。")
    return "\n".join(lines)
