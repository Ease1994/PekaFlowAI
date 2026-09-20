"""权限相关话语：查申请、审批、撤销、按范围提交。不经过模型选工具。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.modules.ai.intent import (
    is_access_apply_intent,
    is_access_cancel_intent,
    is_access_lookup_mine,
    is_access_lookup_pending,
    is_access_review_intent,
    is_group_wide_access,
    is_project_wide_access,
    is_role_access_intent,
    parse_application_id,
    parse_env_code,
    parse_project_id,
)


def _trace(name: str, arguments: dict, result: dict) -> dict:
    return {"id": "direct-access", "name": name, "arguments": arguments, "result": result}


def _ok(reply: str, traces: list | None = None, actions: list | None = None) -> dict:
    return {
        "reply": reply,
        "actions": actions or [],
        "audit": bool(actions),
        "traces": traces or [],
    }


def _tool(db: Session, name: str, params: dict, current: CurrentUser) -> tuple[dict, dict]:
    from app.modules.ai.chat import execute_tool

    result = execute_tool(db, name, params, current)
    traces = [_trace(name, params, result)]
    return result, traces


def dispatch_access_utterance(db: Session, current: CurrentUser, message: str) -> dict | None:
    """权限这条业务不让模型绕：能唯一确定范围就提交，确定不了只问范围不问流水线清单。"""
    text = (message or "").strip()
    if not text:
        return None
    if is_access_lookup_pending(text):
        return _list_pending(db, current)
    if is_access_lookup_mine(text):
        return _list_mine(db, current)
    if is_access_review_intent(text):
        return _review(db, current, text)
    if is_access_cancel_intent(text):
        return _cancel(db, current, text)
    if not is_access_apply_intent(text):
        return None
    return _apply(db, current, text)


def _list_mine(db: Session, current: CurrentUser) -> dict:
    result, traces = _tool(db, "list_my_access_applications", {}, current)
    rows = result.get("applications") or []
    if not rows:
        return _ok("你还没有提交过权限申请。", traces)
    lines = ["我的权限申请："]
    for r in rows[:15]:
        scope = r.get("scope") or r.get("pipeline") or ""
        lines.append(f"· #{r['id']} {scope} {r.get('status')}")
    return _ok("\n".join(lines), traces)


def _list_pending(db: Session, current: CurrentUser) -> dict:
    result, traces = _tool(db, "list_pending_access_applications", {}, current)
    rows = result.get("applications") or []
    if not rows:
        return _ok("当前没有待你审批的权限申请。", traces)
    lines = ["待你审批："]
    for r in rows[:15]:
        who = r.get("applicant") or ""
        scope = " / ".join(
            x for x in (r.get("project"), r.get("group"), r.get("pipeline")) if x
        )
        lines.append(f"· #{r['id']} {who} {scope}")
    lines.append("要处理哪一条，直接说「通过 #单号」或「驳回 #单号」。")
    return _ok("\n".join(lines), traces)


def _review(db: Session, current: CurrentUser, message: str) -> dict:
    aid = parse_application_id(message)
    if not aid:
        listed = _list_pending(db, current)
        listed["reply"] = "请带上申请单号，例如「通过 #12」。\n" + listed["reply"]
        return listed
    approved = not any(k in message for k in ("驳回", "拒绝", "不同意"))
    result, traces = _tool(
        db,
        "propose_review_access",
        {"application_id": aid, "approved": approved},
        current,
    )
    if result.get("error"):
        return _ok(str(result["error"]), traces)
    actions = []
    if result.get("_action"):
        actions = [
            {
                "type": result["_action"],
                "label": result.get("label") or "确认",
                "payload": result.get("payload") or {},
            }
        ]
    return _ok(str(result.get("reply") or "请确认是否审批。"), traces, actions)


def _cancel(db: Session, current: CurrentUser, message: str) -> dict:
    aid = parse_application_id(message)
    params = {"application_id": aid} if aid else {}
    result, traces = _tool(db, "cancel_access_application", params, current)
    return _ok(str(result.get("reply") or result.get("error") or result), traces)


def _match_projects(projects: list[dict], message: str) -> list[dict]:
    pid = parse_project_id(message)
    if pid:
        hit = next((p for p in projects if int(p.get("id") or 0) == pid), None)
        return [hit] if hit else []
    compact = message.replace(" ", "")
    hits: list[dict] = []
    for p in projects:
        name = str(p.get("name") or "")
        code = str(p.get("code") or "")
        if name and (name in message or name.replace(" ", "") in compact):
            hits.append(p)
        elif code and code.lower() in message.lower():
            hits.append(p)
    uniq = {int(p["id"]): p for p in hits}
    return list(uniq.values())


def _actions_payload(message: str) -> dict:
    """用户点名了哪些动作。没点名就空，申请工具会走默认查看+执行。"""
    from app.modules.access.service import parse_requested_actions

    actions = parse_requested_actions(message)
    return {"actions": actions} if actions else {}


def _with_requested(result: dict, extra: dict) -> dict:
    """把本轮点名的动作写进目录工具结果，工作记忆下一轮还能带上。"""
    actions = extra.get("actions")
    if actions:
        result = dict(result)
        result["requested_actions"] = actions
    return result


def _apply(db: Session, current: CurrentUser, message: str) -> dict:
    from app.modules.access.service import catalog_for_apply
    from app.modules.pipeline.models import Pipeline as PipelineModel
    from app.modules.ai.context import match_score

    data = catalog_for_apply(db, current)
    projects = data.get("projects") or []
    env = parse_env_code(message)
    want_group = bool(env) or is_group_wide_access(message)
    want_project = is_project_wide_access(message) and not want_group
    matched_projects = _match_projects(projects, message)
    extra = _actions_payload(message)

    if current.is_admin:
        return _ok("你是管理员，已有全部权限，不用申请。")

    if is_role_access_intent(message):
        return _apply_role(db, current, projects, matched_projects, message)

    if want_group:
        if len(matched_projects) != 1:
            return _ok(
                _ask_project(projects, "要申请哪个项目的该环境权限："),
                [
                    _trace(
                        "list_access_catalog",
                        {},
                        _with_requested(
                            {
                                "projects": projects[:10],
                                "awaiting": "pick_project",
                                "access_kind": "group",
                                "pending_env": env,
                            },
                            extra,
                        ),
                    )
                ],
            )
        return _apply_group(db, current, int(matched_projects[0]["id"]), env, extra)

    if want_project:
        if len(matched_projects) != 1:
            return _ok(
                _ask_project(projects, "整项目权限含该项目所有环境（含生产）。请指出项目编号或全称："),
                [
                    _trace(
                        "list_access_catalog",
                        {},
                        _with_requested(
                            {
                                "projects": projects[:10],
                                "awaiting": "pick_project",
                                "access_kind": "project",
                            },
                            extra,
                        ),
                    )
                ],
            )
        return _call_apply(
            db,
            current,
            "apply_project_execute",
            {
                "project_id": int(matched_projects[0]["id"]),
                "reason": "助手代为申请该项目全部流水线权限",
                **extra,
            },
        )

    from app.modules.access.service import catalog as access_catalog

    raw = access_catalog(db)
    scored: list[tuple[int, dict]] = []
    for item in raw.get("pipelines") or []:
        p = db.get(PipelineModel, item["id"])
        score = match_score(message.lower(), p) if p is not None else 0
        if score:
            scored.append((score, item))
    top = max((score for score, _ in scored), default=0)
    matched = [item for score, item in scored if score == top]
    if len(matched) == 1:
        return _call_apply(
            db,
            current,
            "apply_pipeline_execute",
            {"pipeline_id": matched[0]["id"], "reason": "助手代为申请权限", **extra},
        )

    if len(matched_projects) == 1:
        proj = matched_projects[0]
        return _ok(
            _ask_scope(proj),
            [
                _trace(
                    "list_access_catalog",
                    {},
                    _with_requested(
                        {
                            "projects": [proj],
                            "awaiting": "pick_scope",
                            "access_kind": "scope",
                        },
                        extra,
                    ),
                )
            ],
        )

    if matched_projects:
        return _ok(
            _ask_project(matched_projects, "找到这些项目，请指出要申请哪一个："),
            [
                _trace(
                    "list_access_catalog",
                    {},
                    _with_requested(
                        {
                            "projects": matched_projects,
                            "awaiting": "pick_project",
                            "access_kind": "scope",
                        },
                        extra,
                    ),
                )
            ],
        )
    return None


def _apply_group(db: Session, current: CurrentUser, project_id: int, env: str, extra: dict | None = None) -> dict:
    """项目已定、要按环境申请时提交；环境码还没有则只问环境。"""
    extra = extra or {}
    params: dict = {
        "project_id": project_id,
        "reason": "助手代为申请该环境全部分组流水线权限",
        **extra,
    }
    if env:
        params["env"] = env
        return _call_apply(db, current, "apply_group_execute", params)
    from app.modules.access.service import catalog_for_apply

    data = catalog_for_apply(db, current)
    proj = next((p for p in data.get("projects") or [] if int(p.get("id") or 0) == project_id), None)
    return _ok(
        "请说明要哪个环境：测试、生产、UAT、预发或开发。整项目（含生产）请说「整个项目」。",
        [
            _trace(
                "list_access_catalog",
                {},
                _with_requested(
                    {
                        "projects": [proj] if proj else [{"id": project_id, "name": f"项目#{project_id}", "groups": []}],
                        "awaiting": "pick_scope",
                        "access_kind": "group",
                    },
                    extra,
                ),
            )
        ],
    )


def _call_apply(db: Session, current: CurrentUser, name: str, params: dict) -> dict:
    result, traces = _tool(db, name, params, current)
    return _ok(str(result.get("reply") or result.get("error") or result), traces)


def _ask_project(projects: list[dict], lead: str) -> str:
    lines = [lead]
    for p in projects[:10]:
        n = p.get("pipeline_count") or 0
        flag = "（已有执行权）" if p.get("already_execute") else ""
        lines.append(f"· #{p['id']} {p.get('name')}，{n} 条流水线{flag}")
    return "\n".join(lines)


def _ask_scope(proj: dict) -> str:
    """项目能对上、范围对不上时，只问整项目还是某个环境，不列流水线。"""
    name = proj.get("name") or f"项目#{proj.get('id')}"
    lines = [
        f"「{name}」可以按两种范围申请权限（默认查看+执行；也可以点名编辑、删除、审批或全部权限）：",
        "· 整个项目：当前和以后新建的流水线，含生产",
        "· 只要某个环境：测试 / 生产 / UAT / 预发 / 开发",
    ]
    for g in proj.get("groups") or []:
        flag = "（已有）" if g.get("already_execute") else ""
        lines.append(f"  - {g.get('name')}（{g.get('env') or ''}）{flag}")
    lines.append("直接回复「整个项目」或「只要测试 / 只要生产」。")
    return "\n".join(lines)


def _match_roles(roles: list[dict], message: str) -> list[dict]:
    """话里点到的角色名。"""
    text = (message or "").lower()
    hits = []
    for r in roles:
        name = str(r.get("name") or "").strip().lower()
        if name and name in text:
            hits.append(r)
    return hits


def _apply_role(
    db: Session,
    current: CurrentUser,
    projects: list[dict],
    matched_projects: list[dict],
    message: str,
) -> dict:
    """点名角色时提交角色申请；项目或角色对不上就只问范围。"""
    if len(matched_projects) != 1:
        return _ok(
            _ask_project(projects, "要申请哪个项目的角色："),
            [
                _trace(
                    "list_access_catalog",
                    {},
                    {
                        "projects": projects[:10],
                        "awaiting": "pick_project",
                        "access_kind": "role",
                    },
                )
            ],
        )
    proj = matched_projects[0]
    roles = list(proj.get("roles") or [])
    if not roles:
        return _ok(
            f"项目「{proj.get('name')}」还没有角色。可以改申请资源权限（默认查看+执行），或请管理员先在权限管理里建角色。",
            [_trace("list_access_catalog", {}, {"projects": [proj], "access_kind": "role"})],
        )
    hits = _match_roles(roles, message)
    if len(hits) == 1:
        return _call_apply(
            db,
            current,
            "apply_project_role",
            {
                "role_id": int(hits[0]["id"]),
                "reason": "助手代为申请项目角色",
            },
        )
    lines = [f"「{proj.get('name')}」有这些角色，请指出要申请哪一个："]
    for r in roles[:10]:
        desc = r.get("description") or ""
        extra = f"（{desc}）" if desc else ""
        lines.append(f"· {r.get('name')}{extra}")
    return _ok(
        "\n".join(lines),
        [
            _trace(
                "list_access_catalog",
                {},
                {
                    "projects": [proj],
                    "awaiting": "pick_role",
                    "access_kind": "role",
                },
            )
        ],
    )
