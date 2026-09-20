"""权限申请：资源直授（项目 / 分组 / 流水线）或项目角色，审批记录永久落库。"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.core.deps import CurrentUser, check_permission, invalidate_permission_cache
from app.core.response import BizException
from app.modules.audit.context import current_source
from app.modules.audit.service import write as write_audit
from app.modules.auth.models import PermissionApplication, Role, User, UserRole
from app.modules.auth.permission_service import (
    ACTION_LABELS,
    RESOURCE_ACTIONS,
    grant_permissions,
    list_permissions,
)
from app.modules.notify import emit
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project

# 没点名具体动作时，申请查看 + 执行。点名编辑/删除/审批/豁免或「全部权限」时再写入那些动作。
EXECUTE_GRANT = ["read", "execute"]
# 申请单能要的操作：项目/分组/流水线三层并集。节点下发不走这条申请。
APPLICATION_ACTIONS = ["read", "create", "update", "delete", "execute", "approve", "approval_exempt"]
# 整项目一张单：pipeline_id / group_id 为 0 表示「该项目下全部流水线」
APPLY_PROJECT_EXECUTE = "project_execute"
APPLY_GROUP_EXECUTE = "group_execute"
APPLY_ROLE = "role"
SCOPE_ALL_ID = 0
SCOPE_ALL_GROUP = "（全部环境分组）"
SCOPE_ALL_PIPELINE = "（项目下全部流水线）"
SCOPE_GROUP_PIPELINE = "（该环境下全部流水线）"
SCOPE_ROLE_GROUP = "项目角色"

# 从话术里认要申请哪些动作。没点名时 parse_requested_actions 返回 None，走默认。
_ACTION_SPEECH = (
    (re.compile(r"全部权限|所有权限|全部操作|所有操作"), "*"),
    (re.compile(r"豁免审批|免审"), "approval_exempt"),
    (re.compile(r"审批权|审批权限"), "approve"),
    (re.compile(r"删除权|删除权限"), "delete"),
    (re.compile(r"编辑权|编辑权限|更新权|修改权"), "update"),
    (re.compile(r"创建权|新建权|创建权限"), "create"),
    (re.compile(r"执行权|执行权限"), "execute"),
    (re.compile(r"查看权|只读权限|查看权限"), "read"),
)


def format_action_labels(actions: list[str]) -> str:
    """把动作码收成通知和对话里的中文清单。"""
    return "、".join(ACTION_LABELS.get(a, a) for a in actions)


SOURCE_LABELS = {
    "ai": "AI 助手",
    "web": "手动申请",
    "api": "API",
}


def source_label(source: str | None) -> str:
    """申请入口给人看的名称。按申请单 source 走，页面提交是手动，对话提交才是 AI。"""
    key = (source or "web").strip().lower()
    return SOURCE_LABELS.get(key) or SOURCE_LABELS["web"]


def parse_requested_actions(message: str) -> list[str] | None:
    """从用户话里抽出要申请的动作。

    没点名具体治理动作时返回 None，调用方按默认查看+执行落单。
    「申请执行权限」只是默认口吻，不当成额外点名。
    「申请所有权限」返回可申请的完整集合。
    """
    text = message or ""
    found: list[str] = []
    for pattern, code in _ACTION_SPEECH:
        if not pattern.search(text):
            continue
        if code == "*":
            return list(APPLICATION_ACTIONS)
        if code not in found:
            found.append(code)
    extra = [a for a in found if a not in EXECUTE_GRANT]
    if not extra:
        return None
    return _parse_actions(found)


def _parse_actions(raw: str | list | None, fallback: list[str] | None = None) -> list[str]:
    """解析申请/审核传入的动作列表。未知码丢掉；写类动作自动补上查看。"""
    if isinstance(raw, list):
        items = [str(a).strip() for a in raw if str(a).strip()]
    elif isinstance(raw, str) and raw.strip():
        items = [a.strip() for a in raw.split(",") if a.strip()]
    else:
        items = list(fallback or [])
    allowed = set(APPLICATION_ACTIONS)
    out: list[str] = []
    for a in items:
        if a not in allowed:
            continue
        if a not in out:
            out.append(a)
    if out and "read" not in out:
        out.insert(0, "read")
    if not out:
        raise BizException.bad_request("至少选择一项权限")
    order = {name: i for i, name in enumerate(APPLICATION_ACTIONS)}
    return sorted(out, key=lambda name: order.get(name, 99))


def _requested_grant(actions: str | list | None) -> list[str]:
    """申请入参：没传动作就用默认查看+执行。"""
    return _parse_actions(actions, fallback=EXECUTE_GRANT)


def _can_review(db: Session, user: CurrentUser, group_id: int) -> bool:
    if user.is_admin:
        return True
    if int(group_id or 0) == SCOPE_ALL_ID:
        return False
    return check_permission(db, user, "group", group_id, "approve")


def _is_project_wide_apply(apply_type: str | None) -> bool:
    """整项目资源申请和角色申请都按项目下任一分组的审批人来审。"""
    return apply_type in {APPLY_PROJECT_EXECUTE, APPLY_ROLE}


def _can_review_application(db: Session, user: CurrentUser, a: PermissionApplication) -> bool:
    """单条流水线看该分组审批人；整项目 / 角色申请看项目下任一分组的审批人或管理员。"""
    if user.is_admin:
        return True
    if _is_project_wide_apply(a.apply_type):
        groups = db.scalars(select(Group).where(Group.project_id == a.project_id)).all()
        return any(check_permission(db, user, "group", g.id, "approve") for g in groups)
    return _can_review(db, user, a.group_id)


def _reviewer_user_ids(db: Session, a: PermissionApplication, exclude_user_id: int) -> list[int]:
    """申请单该通知谁来审。"""
    from app.modules.notify.service import reviewers_of_group

    if _is_project_wide_apply(a.apply_type):
        ids: set[int] = set()
        for g in db.scalars(select(Group).where(Group.project_id == a.project_id)).all():
            ids.update(reviewers_of_group(db, g.id, exclude_user_id=exclude_user_id))
        return list(ids)
    return reviewers_of_group(db, a.group_id, exclude_user_id=exclude_user_id)


def _role_permissions(raw: str | None) -> dict:
    """角色权限包：坏 JSON 当空对象。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _role_public(r: Role) -> dict:
    """申请目录用的角色摘要，不含成员名单。"""
    return {
        "id": r.id,
        "project_id": r.project_id,
        "name": r.name,
        "description": r.description or "",
        "permissions": _role_permissions(r.permissions),
    }


def catalog(db: Session) -> dict:
    """申请用目录：全量项目/分组/流水线（未授权用户也要能选到要申请的流水线）。"""
    projects = db.scalars(select(Project).order_by(Project.id)).all()
    groups = db.scalars(select(Group).order_by(Group.id)).all()
    pipelines = db.scalars(
        select(Pipeline)
        .options(defer(Pipeline.yaml))
        .where(Pipeline.status == "active")
        .order_by(Pipeline.id)
    ).all()
    group_map = {g.id: g for g in groups}
    project_map = {p.id: p for p in projects}
    counts: dict[int, int] = {}
    for p in pipelines:
        counts[p.project_id] = counts.get(p.project_id, 0) + 1
    roles = db.scalars(select(Role).order_by(Role.id)).all()
    return {
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "code": p.code,
                "pipeline_count": counts.get(p.id, 0),
            }
            for p in projects
        ],
        "groups": [
            {"id": g.id, "name": g.name, "type": g.type, "project_id": g.project_id} for g in groups
        ],
        "pipelines": [
            {
                "id": p.id,
                "name": p.name,
                "project_id": p.project_id,
                "group_id": p.group_id,
                "project": project_map[p.project_id].name if p.project_id in project_map else "",
                "group": group_map[p.group_id].name if p.group_id in group_map else "",
                "env": group_map[p.group_id].type if p.group_id in group_map else "",
            }
            for p in pipelines
        ],
        "roles": [_role_public(r) for r in roles],
    }


def application_public(db: Session, a: PermissionApplication) -> dict:
    applicant = db.get(User, a.applicant_id)
    reviewer = db.get(User, a.reviewer_id) if a.reviewer_id else None
    return {
        "id": a.id,
        "applicant_id": a.applicant_id,
        "applicant": (applicant.display_name or applicant.username) if applicant else str(a.applicant_id),
        "project_id": a.project_id,
        "group_id": a.group_id,
        "pipeline_id": a.pipeline_id,
        "project_name": a.project_name,
        "group_name": a.group_name,
        "pipeline_name": a.pipeline_name,
        "apply_type": a.apply_type,
        "granted_actions": a.granted_actions,
        "reason": a.reason,
        "status": a.status,
        "reviewer_id": a.reviewer_id,
        "reviewer": (reviewer.display_name or reviewer.username) if reviewer else "",
        "review_comment": a.review_comment,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
        "source": a.source or "web",
        "granted_action_list": [x for x in (a.granted_actions or "").split(",") if x],
        "granted_action_labels": format_action_labels(
            [x for x in (a.granted_actions or "").split(",") if x]
        ),
        "role_id": int(getattr(a, "role_id", 0) or 0),
        "role_name": a.pipeline_name if a.apply_type == APPLY_ROLE else "",
        "scope": _application_scope(a.apply_type),
        "scope_label": _application_scope_label(a.apply_type),
        "scope_text": _application_scope_text(a),
    }


def _application_scope(apply_type: str | None) -> str:
    """申请单覆盖范围：project / group / pipeline / role。"""
    if apply_type == APPLY_PROJECT_EXECUTE:
        return "project"
    if apply_type == APPLY_GROUP_EXECUTE:
        return "group"
    if apply_type == APPLY_ROLE:
        return "role"
    return "pipeline"


def _application_scope_label(apply_type: str | None) -> str:
    """列表和审批文案用的范围名称。"""
    if apply_type == APPLY_PROJECT_EXECUTE:
        return "整项目"
    if apply_type == APPLY_GROUP_EXECUTE:
        return "环境分组"
    if apply_type == APPLY_ROLE:
        return "项目角色"
    return "流水线"


def _application_scope_text(a: PermissionApplication) -> str:
    """列表一行能读完的范围。角色单用角色名，资源单用项目/分组/流水线。"""
    if a.apply_type == APPLY_ROLE:
        return f"{a.project_name} / 角色 {a.pipeline_name}"
    return f"{a.project_name} / {a.group_name} / {a.pipeline_name}"


def _assert_not_admin(current: CurrentUser) -> None:
    if current.is_admin:
        raise BizException.bad_request("你是管理员，已有全部权限，无需申请")


def _has_all_actions(
    db: Session, current: CurrentUser, resource_type: str, resource_id: int, actions: list[str]
) -> bool:
    """当前用户是否已经具备这批动作（豁免审批必须明示授予，不吃通配符）。"""
    return all(
        check_permission(
            db,
            current,
            resource_type,
            resource_id,
            action,
            allow_wildcard=action != "approval_exempt",
        )
        for action in actions
    )


def _assert_can_apply(
    db: Session,
    current: CurrentUser,
    *,
    project_id: int,
    group_id: int | None = None,
    pipeline_id: int | None = None,
    requested: list[str] | None = None,
) -> None:
    """已经有更大范围的同等权限、或已有待审单，就不要再交一张重复的。"""
    _assert_not_admin(current)
    grant = list(requested or EXECUTE_GRANT)
    proj = db.get(Project, project_id)
    pname = proj.name if proj else f"项目#{project_id}"
    if _has_all_actions(db, current, "project", project_id, grant):
        raise BizException.bad_request(f"你已有项目「{pname}」的这些权限，无需再申请")
    pending_proj = db.scalar(
        select(PermissionApplication).where(
            PermissionApplication.applicant_id == current.id,
            PermissionApplication.project_id == project_id,
            PermissionApplication.apply_type == APPLY_PROJECT_EXECUTE,
            PermissionApplication.status == "pending",
        )
    )
    if pending_proj is not None:
        raise BizException.bad_request(f"已有该项目的待审批申请 #{pending_proj.id}，请等待审批")
    if group_id:
        grp = db.get(Group, group_id)
        gname = grp.name if grp else f"分组#{group_id}"
        if _has_all_actions(db, current, "group", group_id, grant):
            raise BizException.bad_request(f"你已有「{pname} / {gname}」的这些权限，无需再申请")
        pending_g = db.scalar(
            select(PermissionApplication).where(
                PermissionApplication.applicant_id == current.id,
                PermissionApplication.group_id == group_id,
                PermissionApplication.apply_type == APPLY_GROUP_EXECUTE,
                PermissionApplication.status == "pending",
            )
        )
        if pending_g is not None:
            raise BizException.bad_request(f"已有该环境的待审批申请 #{pending_g.id}，请等待审批")
    if pipeline_id and _has_all_actions(db, current, "pipeline", pipeline_id, grant):
        raise BizException.bad_request("你已有该流水线的这些权限，无需再申请")


def _fold_key(text: str) -> str:
    """去掉空格、连字符再比名称，避免「DMS 经销商」对不上「DMS-经销商」。"""
    return re.sub(r"[\s\-_/·.]+", "", (text or "").lower())


def _project_matches_keyword(keyword: str, name: str, code: str) -> bool:
    """关键字命中项目名或代号。双向包含，整句当关键字也能对上项目全称。"""
    kw = (keyword or "").strip()
    if not kw:
        return True
    folded = _fold_key(kw)
    name_key = _fold_key(name)
    code_key = _fold_key(code)
    if not folded:
        return True
    for blob in (name_key, code_key):
        if not blob:
            continue
        if folded in blob or blob in folded:
            return True
    tokens = [_fold_key(part) for part in re.split(r"[\s\-_/]+", kw) if _fold_key(part)]
    haystack = name_key + code_key
    return bool(tokens) and all(token in haystack for token in tokens)


def catalog_for_apply(db: Session, current: CurrentUser, keyword: str = "") -> dict:
    """给助手用的申请目录：项目和环境下有没有执行权、有没有待审单，不列流水线。"""
    from app.core.env import is_platform_project

    data = catalog(db)
    mine = [
        a
        for a in list_applications(db, current, "mine")
        if a.get("status") == "pending"
    ]
    pending_project = {int(a["project_id"]): a["id"] for a in mine if a.get("apply_type") == APPLY_PROJECT_EXECUTE}
    pending_group = {int(a["group_id"]): a["id"] for a in mine if a.get("apply_type") == APPLY_GROUP_EXECUTE}
    pending_role = {int(a["role_id"]): a["id"] for a in mine if a.get("apply_type") == APPLY_ROLE and a.get("role_id")}
    role_by_project: dict[int, list[dict]] = {}
    for r in data.get("roles") or []:
        role_by_project.setdefault(int(r.get("project_id") or 0), []).append(
            {
                "id": r.get("id"),
                "name": r.get("name") or "",
                "description": r.get("description") or "",
                "pending_id": pending_role.get(int(r.get("id") or 0)),
            }
        )
    projects = []
    for p in data.get("projects") or []:
        if is_platform_project(p.get("code")):
            continue
        if not _project_matches_keyword(keyword, str(p.get("name") or ""), str(p.get("code") or "")):
            continue
        pid = int(p["id"])
        already = bool(current.is_admin) or check_permission(db, current, "project", pid, "execute")
        groups = []
        for g in data.get("groups") or []:
            if int(g.get("project_id") or 0) != pid:
                continue
            gid = int(g["id"])
            g_already = already or check_permission(db, current, "group", gid, "execute")
            groups.append(
                {
                    "id": gid,
                    "name": g.get("name") or "",
                    "env": g.get("type") or "",
                    "already_execute": g_already,
                    "pending_id": pending_group.get(gid),
                }
            )
        projects.append(
            {
                "id": pid,
                "name": p.get("name") or "",
                "code": p.get("code") or "",
                "pipeline_count": p.get("pipeline_count") or 0,
                "already_execute": already,
                "pending_id": pending_project.get(pid),
                "groups": groups,
                "roles": role_by_project.get(pid, []),
            }
        )
    return {
        "projects": projects[:20],
        "hint": (
            "整项目用 apply_project_execute；某个环境用 apply_group_execute；"
            "只要一条流水线才用 apply_pipeline_execute；点名项目角色用 apply_project_role。"
            "不要列出流水线再循环申请。"
        ),
    }


def my_entitlements(db: Session, current: CurrentUser) -> dict:
    """当前用户已有的直授和角色，按项目分组给申请页展示。"""
    if current.is_admin:
        return {"is_admin": True, "projects": [], "nodes": []}
    rows = [
        p
        for p in list_permissions(db, user_id=current.id)
        if p.get("resource_type") != "console" and p.get("effect") != "deny"
    ]
    grouped: dict[tuple[str, int], dict] = {}
    for p in rows:
        key = (str(p["resource_type"]), int(p["resource_id"]))
        cur = grouped.get(key)
        if cur is None:
            grouped[key] = {
                "resource_type": p["resource_type"],
                "resource_id": int(p["resource_id"]),
                "resource_name": p.get("resource_name") or "",
                "actions": [p["action"]],
            }
        elif p["action"] not in cur["actions"]:
            cur["actions"].append(p["action"])
    direct = list(grouped.values())
    pipe_ids = {d["resource_id"] for d in direct if d["resource_type"] == "pipeline"}
    group_ids = {d["resource_id"] for d in direct if d["resource_type"] == "group"}
    pipe_proj: dict[int, int] = {}
    if pipe_ids:
        pipe_proj = {
            int(pid): int(project_id)
            for pid, project_id in db.execute(
                select(Pipeline.id, Pipeline.project_id).where(Pipeline.id.in_(pipe_ids))
            ).all()
        }
    group_proj: dict[int, int] = {}
    if group_ids:
        group_proj = {
            int(gid): int(project_id)
            for gid, project_id in db.execute(
                select(Group.id, Group.project_id).where(Group.id.in_(group_ids))
            ).all()
        }

    def _project_id_of(item: dict) -> int:
        rtype = item["resource_type"]
        rid = int(item["resource_id"])
        if rtype == "project":
            return rid
        if rtype == "group":
            return group_proj.get(rid, 0)
        if rtype == "pipeline":
            return pipe_proj.get(rid, 0)
        return 0

    nodes = [d for d in direct if d["resource_type"] in {"node", "node_group"}]
    by_project: dict[int, list[dict]] = {}
    for item in direct:
        if item["resource_type"] in {"node", "node_group"}:
            continue
        by_project.setdefault(_project_id_of(item), []).append(item)

    role_rows = db.scalars(select(UserRole).where(UserRole.user_id == current.id)).all()
    roles_by_project: dict[int, list[dict]] = {}
    for ur in role_rows:
        role = db.get(Role, ur.role_id)
        if role is None:
            continue
        roles_by_project.setdefault(role.project_id, []).append(
            {
                "role_id": role.id,
                "name": role.name,
                "description": role.description or "",
                "permissions": _role_permissions(role.permissions),
            }
        )

    project_ids = {pid for pid in set(by_project) | set(roles_by_project) if pid}
    name_map: dict[int, str] = {}
    if project_ids:
        name_map = {
            int(pid): name
            for pid, name in db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids))).all()
        }
    projects = []
    for pid in sorted(project_ids, key=lambda i: name_map.get(i, f"项目#{i}")):
        projects.append(
            {
                "project_id": pid,
                "project_name": name_map.get(pid, f"项目#{pid}"),
                "roles": roles_by_project.get(pid, []),
                "direct": by_project.get(pid, []),
            }
        )
    orphan = by_project.get(0) or []
    if orphan:
        projects.append(
            {
                "project_id": 0,
                "project_name": "其它",
                "roles": [],
                "direct": orphan,
            }
        )
    return {"is_admin": False, "projects": projects, "nodes": nodes}


def _grant_approved(db: Session, a: PermissionApplication, grant: list[str]) -> None:
    """按申请范围落权：资源申请写 Permission，角色申请写 UserRole。

    审批权只落在项目或分组上；单条流水线申请若带了审批，改授到所属环境分组。
    """
    if a.apply_type == APPLY_ROLE:
        _grant_role_membership(db, a)
        return
    if a.apply_type == APPLY_PROJECT_EXECUTE:
        grant_permissions(
            db, [a.applicant_id], "project", [a.project_id], grant, commit=False, audit=False
        )
        return
    if a.apply_type == APPLY_GROUP_EXECUTE:
        grant_permissions(
            db, [a.applicant_id], "group", [a.group_id], grant, commit=False, audit=False
        )
        return
    pipeline_ok = set(RESOURCE_ACTIONS.get("pipeline") or [])
    on_pipe = [x for x in grant if x in pipeline_ok]
    on_group = [x for x in grant if x not in pipeline_ok]
    if on_pipe:
        grant_permissions(
            db, [a.applicant_id], "pipeline", [a.pipeline_id], on_pipe, commit=False, audit=False
        )
    if on_group and a.group_id:
        grant_permissions(
            db, [a.applicant_id], "group", [a.group_id], on_group, commit=False, audit=False
        )


def _grant_role_membership(db: Session, a: PermissionApplication) -> None:
    """角色申请通过后把申请人加进该角色，已是成员则跳过。"""
    role_id = int(getattr(a, "role_id", 0) or 0)
    if not role_id:
        return
    exists = db.scalar(
        select(UserRole).where(UserRole.user_id == a.applicant_id, UserRole.role_id == role_id)
    )
    if exists is None:
        db.add(UserRole(user_id=a.applicant_id, role_id=role_id))
    invalidate_permission_cache(db, a.applicant_id)


def apply_execute(
    db: Session,
    current: CurrentUser,
    pipeline_id: int,
    reason: str,
    actions: str | list | None = None,
) -> dict:
    p = db.get(Pipeline, int(pipeline_id))
    if p is None:
        raise BizException.not_found("流水线")
    grant = _requested_grant(actions)
    _assert_can_apply(
        db, current, project_id=p.project_id, group_id=p.group_id, pipeline_id=p.id, requested=grant
    )
    pending = db.scalar(
        select(PermissionApplication).where(
            PermissionApplication.applicant_id == current.id,
            PermissionApplication.pipeline_id == p.id,
            PermissionApplication.apply_type == "execute",
            PermissionApplication.status == "pending",
        )
    )
    if pending is not None:
        raise BizException.bad_request(f"已有待审批申请 #{pending.id}，请等待审批")
    proj = db.get(Project, p.project_id)
    grp = db.get(Group, p.group_id)
    src = current_source() or "web"
    row = PermissionApplication(
        applicant_id=current.id,
        project_id=p.project_id,
        group_id=p.group_id,
        pipeline_id=p.id,
        apply_type="execute",
        project_name=proj.name if proj else "",
        group_name=grp.name if grp else "",
        pipeline_name=p.name,
        reason=(reason or "").strip(),
        status="pending",
        source=src,
        granted_actions=",".join(grant),
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        "access.apply",
        "permission_application",
        row.id,
        f"申请流水线权限 {row.project_name}/{row.group_name}/{row.pipeline_name} {format_action_labels(grant)}",
        user_id=current.id,
        username=current.username,
        source=src,
    )
    applicant = current.username
    emit(
        db,
        "access.apply",
        group_reviewers=row.group_id,
        exclude_user_id=current.id,
        title=f"权限申请待审核 #{row.id}",
        content=(
            f"申请人：{applicant}\n"
            f"范围：{row.project_name} / {row.group_name} / {row.pipeline_name}\n"
            f"申请权限：{format_action_labels(grant)}\n"
            f"入口：{source_label(src)}\n"
            f"说明：{row.reason or '（无）'}"
        ),
        link=f"/permissions?tab=pending&id={row.id}",
        related_id=row.id,
        dedupe_key=f"access.apply:{row.id}",
    )
    db.commit()
    db.refresh(row)
    return application_public(db, row)


def resolve_project(db: Session, *, project_id: int | None = None, keyword: str = "") -> Project:
    """用 id 或名称/代号定位项目。名称匹配必须唯一，避免申请错项目。"""
    if project_id:
        proj = db.get(Project, int(project_id))
        if proj is None:
            raise BizException.not_found("项目")
        return proj
    text = (keyword or "").strip().lower()
    if not text:
        raise BizException.bad_request("缺少 project_id 或项目名称")
    rows = db.scalars(select(Project).order_by(Project.id)).all()
    hits = [
        p
        for p in rows
        if text == (p.name or "").strip().lower()
        or text == (p.code or "").strip().lower()
        or text in (p.name or "").strip().lower()
    ]
    exact = [
        p
        for p in hits
        if text == (p.name or "").strip().lower() or text == (p.code or "").strip().lower()
    ]
    chosen = exact if len(exact) == 1 else hits
    if len(chosen) != 1:
        names = "、".join(f"{p.name}(#{p.id})" for p in (chosen or rows)[:8])
        raise BizException.bad_request(
            "无法唯一确定项目，请给出完整项目名或 project_id。"
            + (f"候选：{names}" if names else "")
        )
    return chosen[0]


def apply_project_execute(
    db: Session,
    current: CurrentUser,
    *,
    project_id: int | None = None,
    keyword: str = "",
    reason: str = "",
    actions: str | list | None = None,
) -> dict:
    """一张申请覆盖项目下当前和以后的全部流水线。默认查看+执行，可点名其它动作。"""
    proj = resolve_project(db, project_id=project_id, keyword=keyword)
    from app.core.env import is_platform_project

    if is_platform_project(proj.code):
        raise BizException.bad_request("平台内置项目不能申请业务执行权")
    grant = _requested_grant(actions)
    _assert_can_apply(db, current, project_id=proj.id, requested=grant)
    src = current_source() or "web"
    row = PermissionApplication(
        applicant_id=current.id,
        project_id=proj.id,
        group_id=SCOPE_ALL_ID,
        pipeline_id=SCOPE_ALL_ID,
        apply_type=APPLY_PROJECT_EXECUTE,
        project_name=proj.name,
        group_name=SCOPE_ALL_GROUP,
        pipeline_name=SCOPE_ALL_PIPELINE,
        reason=(reason or "").strip(),
        status="pending",
        source=src,
        granted_actions=",".join(grant),
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        "access.apply",
        "permission_application",
        row.id,
        f"申请项目权限 {row.project_name} {format_action_labels(grant)}",
        user_id=current.id,
        username=current.username,
        source=src,
    )
    reviewer_ids = _reviewer_user_ids(db, row, current.id)
    emit(
        db,
        "access.apply",
        user_ids=reviewer_ids,
        exclude_user_id=current.id,
        title=f"权限申请待审核 #{row.id}",
        content=(
            f"申请人：{current.username}\n"
            f"范围：{row.project_name} / {row.group_name} / {row.pipeline_name}\n"
            f"申请权限：{format_action_labels(grant)}\n"
            f"入口：{source_label(src)}\n"
            f"说明：{row.reason or '（无）'}"
        ),
        link=f"/permissions?tab=pending&id={row.id}",
        related_id=row.id,
        dedupe_key=f"access.apply:{row.id}",
    )
    db.commit()
    db.refresh(row)
    return application_public(db, row)


def resolve_group(
    db: Session, *, group_id: int | None = None, project_id: int | None = None, env: str = ""
) -> Group:
    """用分组 id，或「项目 + 环境码」定位环境分组。"""
    if group_id:
        grp = db.get(Group, int(group_id))
        if grp is None:
            raise BizException.not_found("环境分组")
        return grp
    if not project_id or not (env or "").strip():
        raise BizException.bad_request("申请某个环境的执行权需要 project_id 和 env")
    code = env.strip().lower()
    rows = db.scalars(select(Group).where(Group.project_id == int(project_id))).all()
    hits = [g for g in rows if (g.type or "").strip().lower() == code]
    if len(hits) != 1:
        names = "、".join(f"{g.name}({g.type})" for g in rows[:8]) or "（该项目还没有环境分组）"
        raise BizException.bad_request(f"项目下找不到唯一的「{code}」环境分组。现有：{names}")
    return hits[0]


def apply_group_execute(
    db: Session,
    current: CurrentUser,
    *,
    group_id: int | None = None,
    project_id: int | None = None,
    env: str = "",
    reason: str = "",
    actions: str | list | None = None,
) -> dict:
    """一张申请覆盖某个环境分组下当前和以后的全部流水线。默认查看+执行。"""
    grp = resolve_group(db, group_id=group_id, project_id=project_id, env=env)
    proj = db.get(Project, grp.project_id)
    if proj is None:
        raise BizException.not_found("项目")
    from app.core.env import is_platform_project

    if is_platform_project(proj.code):
        raise BizException.bad_request("平台内置项目不能申请业务执行权")
    grant = _requested_grant(actions)
    _assert_can_apply(db, current, project_id=proj.id, group_id=grp.id, requested=grant)
    src = current_source() or "web"
    row = PermissionApplication(
        applicant_id=current.id,
        project_id=proj.id,
        group_id=grp.id,
        pipeline_id=SCOPE_ALL_ID,
        apply_type=APPLY_GROUP_EXECUTE,
        project_name=proj.name,
        group_name=grp.name,
        pipeline_name=SCOPE_GROUP_PIPELINE,
        reason=(reason or "").strip(),
        status="pending",
        source=src,
        granted_actions=",".join(grant),
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        "access.apply",
        "permission_application",
        row.id,
        f"申请环境分组权限 {row.project_name}/{row.group_name} {format_action_labels(grant)}",
        user_id=current.id,
        username=current.username,
        source=src,
    )
    emit(
        db,
        "access.apply",
        group_reviewers=row.group_id,
        exclude_user_id=current.id,
        title=f"权限申请待审核 #{row.id}",
        content=(
            f"申请人：{current.username}\n"
            f"范围：{row.project_name} / {row.group_name} / {row.pipeline_name}\n"
            f"申请权限：{format_action_labels(grant)}\n"
            f"入口：{source_label(src)}\n"
            f"说明：{row.reason or '（无）'}"
        ),
        link=f"/permissions?tab=pending&id={row.id}",
        related_id=row.id,
        dedupe_key=f"access.apply:{row.id}",
    )
    db.commit()
    db.refresh(row)
    return application_public(db, row)


def resolve_role(
    db: Session,
    *,
    role_id: int | None = None,
    project_id: int | None = None,
    keyword: str = "",
    role_name: str = "",
) -> Role:
    """用角色 id，或「项目 + 角色名」定位项目角色。名称匹配必须唯一。"""
    if role_id:
        role = db.get(Role, int(role_id))
        if role is None:
            raise BizException.not_found("角色")
        return role
    proj = resolve_project(db, project_id=project_id, keyword=keyword)
    text = (role_name or "").strip().lower()
    if not text:
        raise BizException.bad_request("缺少 role_id 或角色名称")
    rows = db.scalars(select(Role).where(Role.project_id == proj.id).order_by(Role.id)).all()
    hits = [r for r in rows if text in (r.name or "").strip().lower()]
    exact = [r for r in hits if text == (r.name or "").strip().lower()]
    chosen = exact if len(exact) == 1 else hits
    if len(chosen) != 1:
        names = "、".join(r.name for r in rows[:8]) or "（该项目还没有角色）"
        raise BizException.bad_request(f"无法唯一确定角色。现有：{names}")
    return chosen[0]


def apply_role(
    db: Session,
    current: CurrentUser,
    *,
    role_id: int | None = None,
    project_id: int | None = None,
    keyword: str = "",
    role_name: str = "",
    reason: str = "",
) -> dict:
    """申请加入某个项目角色。通过后写 UserRole，不写资源直授。"""
    _assert_not_admin(current)
    role = resolve_role(
        db, role_id=role_id, project_id=project_id, keyword=keyword, role_name=role_name
    )
    proj = db.get(Project, role.project_id)
    if proj is None:
        raise BizException.not_found("项目")
    from app.core.env import is_platform_project

    if is_platform_project(proj.code):
        raise BizException.bad_request("平台内置项目不能申请业务角色")
    member = db.scalar(
        select(UserRole).where(UserRole.user_id == current.id, UserRole.role_id == role.id)
    )
    if member is not None:
        raise BizException.bad_request(f"你已是项目「{proj.name}」的角色「{role.name}」成员，无需再申请")
    pending = db.scalar(
        select(PermissionApplication).where(
            PermissionApplication.applicant_id == current.id,
            PermissionApplication.role_id == role.id,
            PermissionApplication.apply_type == APPLY_ROLE,
            PermissionApplication.status == "pending",
        )
    )
    if pending is not None:
        raise BizException.bad_request(f"已有该角色的待审批申请 #{pending.id}，请等待审批")
    src = current_source() or "web"
    row = PermissionApplication(
        applicant_id=current.id,
        project_id=proj.id,
        group_id=SCOPE_ALL_ID,
        pipeline_id=SCOPE_ALL_ID,
        role_id=role.id,
        apply_type=APPLY_ROLE,
        project_name=proj.name,
        group_name=SCOPE_ROLE_GROUP,
        pipeline_name=role.name,
        reason=(reason or "").strip(),
        status="pending",
        source=src,
        granted_actions="",
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        "access.apply",
        "permission_application",
        row.id,
        f"申请项目角色 {row.project_name}/{role.name}",
        user_id=current.id,
        username=current.username,
        source=src,
    )
    reviewer_ids = _reviewer_user_ids(db, row, current.id)
    emit(
        db,
        "access.apply",
        user_ids=reviewer_ids,
        exclude_user_id=current.id,
        title=f"权限申请待审核 #{row.id}",
        content=(
            f"申请人：{current.username}\n"
            f"范围：{row.project_name} / 角色 {role.name}\n"
            f"申请：加入项目角色\n"
            f"入口：{source_label(src)}\n"
            f"说明：{row.reason or '（无）'}"
        ),
        link=f"/permissions?tab=pending&id={row.id}",
        related_id=row.id,
        dedupe_key=f"access.apply:{row.id}",
    )
    db.commit()
    db.refresh(row)
    return application_public(db, row)


def list_applications(db: Session, current: CurrentUser, scope: str = "mine") -> list[dict]:
    """按范围列出申请单。pending 只返回当前用户能审的。"""
    stmt = select(PermissionApplication).order_by(PermissionApplication.id.desc())
    if scope == "mine":
        stmt = stmt.where(PermissionApplication.applicant_id == current.id)
    elif scope == "pending":
        stmt = stmt.where(PermissionApplication.status == "pending")
        rows = db.scalars(stmt).all()
        return [application_public(db, a) for a in rows if _can_review_application(db, current, a)]
    elif scope == "all":
        if not current.is_admin:
            raise BizException.forbidden("仅管理员可查看全部审批记录")
    else:
        stmt = stmt.where(PermissionApplication.applicant_id == current.id)
    return [application_public(db, a) for a in db.scalars(stmt).all()]


def get_application(db: Session, current: CurrentUser, application_id: int) -> dict:
    a = db.get(PermissionApplication, int(application_id))
    if a is None:
        raise BizException.not_found("权限申请")
    if a.applicant_id != current.id and not _can_review_application(db, current, a):
        raise BizException.forbidden("无权查看该申请")
    return application_public(db, a)


def review(
    db: Session,
    current: CurrentUser,
    application_id: int,
    approved: bool,
    comment: str,
    actions: list[str] | None = None,
) -> dict:
    a = db.get(PermissionApplication, int(application_id))
    if a is None:
        raise BizException.not_found("权限申请")
    if a.status != "pending":
        raise BizException.bad_request(f"当前状态 {a.status} 不可审批")
    if not _can_review_application(db, current, a):
        raise BizException.forbidden("无权限审批该分组下的权限申请")
    a.reviewer_id = current.id
    a.review_comment = (comment or "").strip()
    a.reviewed_at = datetime.now()
    is_role = a.apply_type == APPLY_ROLE
    grant: list[str] = []
    if not is_role:
        grant = _parse_actions(actions, fallback=_parse_actions(a.granted_actions, fallback=EXECUTE_GRANT))
    if approved:
        a.status = "approved"
        if not is_role:
            a.granted_actions = ",".join(grant)
        _grant_approved(db, a, grant)
    else:
        a.status = "rejected"
    write_audit(
        db,
        "access.approve" if approved else "access.reject",
        "permission_application",
        a.id,
        f"{'通过' if approved else '驳回'} {_application_scope_text(a)} 的权限申请"
        + (f" 实授 {a.granted_actions}" if approved and not is_role else ""),
        user_id=current.id,
        username=current.username,
    )
    verb = "已通过" if approved else "已驳回"
    if is_role:
        grant_label = f"角色 {a.pipeline_name}" if approved else "无"
    else:
        grant_label = format_action_labels(grant) if approved else "无"
    emit(
        db,
        "access.review",
        user_ids=[a.applicant_id],
        title=f"权限申请 #{a.id} {verb}",
        content=(
            f"范围：{_application_scope_text(a)}\n"
            f"结果：{verb}\n"
            f"实授：{grant_label}\n"
            f"审批人：{current.username}\n"
            f"意见：{a.review_comment or '（无）'}"
        ),
        link="/permissions?tab=history",
        related_id=a.id,
        dedupe_key=f"access.review:{a.id}",
    )
    db.commit()
    db.refresh(a)
    from app.modules.ai.followup import complete_access_watches

    complete_access_watches(db, a)
    return application_public(db, a)


def update_pending(
    db: Session,
    current: CurrentUser,
    application_id: int,
    actions: list[str] | None = None,
    reason: str | None = None,
) -> dict:
    a = db.get(PermissionApplication, int(application_id))
    if a is None:
        raise BizException.not_found("权限申请")
    if a.status != "pending":
        raise BizException.bad_request(f"当前状态 {a.status} 不可修改")
    if not _can_review_application(db, current, a):
        raise BizException.forbidden("无权限修改该权限申请")
    if a.apply_type == APPLY_ROLE and actions is not None:
        raise BizException.bad_request("角色申请不能改拟授动作，通过即加入该角色")
    if actions is not None:
        a.granted_actions = ",".join(_parse_actions(actions))
    if reason is not None:
        a.reason = reason.strip()
    write_audit(
        db,
        "access.update",
        "permission_application",
        a.id,
        f"修改申请 #{a.id} 拟授 {a.granted_actions}",
        user_id=current.id,
        username=current.username,
    )
    db.commit()
    db.refresh(a)
    return application_public(db, a)


def cancel(db: Session, current: CurrentUser, application_id: int) -> dict:
    a = db.get(PermissionApplication, int(application_id))
    if a is None:
        raise BizException.not_found("权限申请")
    if a.applicant_id != current.id and not current.is_admin:
        raise BizException.forbidden("只能撤销自己的申请")
    if a.status != "pending":
        raise BizException.bad_request(f"当前状态 {a.status} 不可撤销")
    a.status = "cancelled"
    a.reviewed_at = datetime.now()
    a.review_comment = "申请人撤销"
    write_audit(
        db,
        "access.cancel",
        "permission_application",
        a.id,
        f"撤销 {_application_scope_text(a)} 的权限申请",
        user_id=current.id,
        username=current.username,
    )
    db.commit()
    db.refresh(a)
    from app.modules.ai.followup import complete_access_watches

    complete_access_watches(db, a)
    return application_public(db, a)
