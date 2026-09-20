"""项目 + 流水线的导入导出。

只给管理员用，不走项目/流水线细粒度授权。导出只带配置：项目、环境分组、流水线 YAML。
不带发布记录、制品、凭证密文、权限。导入按项目 code、流水线名对齐。
重名时由调用方指定覆盖、新建 `_copy`，或跳过这条。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.modules.pipeline.models import Pipeline
from app.modules.pipeline.service import (
    PIPELINE_HIDDEN,
    assert_approval_mode,
    assert_editor_view,
    create_pipeline,
    unique_pipeline_name,
    update_pipeline,
)
from app.modules.pipeline.sub_pipeline import rewrite_run_pipeline_ids, sync_yaml_pipeline_name
from app.modules.project.models import Group, Project

FORMAT = "rp-catalog"
FORMAT_VERSION = 1

# 导入决策：覆盖、按 duplicate 规则新建 name_copy、或跳过这条重名流水线
ACTION_OVERWRITE = "overwrite"
ACTION_COPY = "copy"
ACTION_SKIP = "skip"
ACTION_CREATE = "create"
CONFLICT_ACTIONS = (ACTION_OVERWRITE, ACTION_COPY, ACTION_SKIP)


def item_key(project_code: str, pipeline_name: str) -> str:
    """导入决策表的主键。项目编码里不会出现这个分隔符。"""
    return f"{(project_code or '').strip()}::{(pipeline_name or '').strip()}"


def _require_admin(current: CurrentUser) -> None:
    """导入导出不进权限管理，只认管理员账号。"""
    if not current.is_admin:
        raise BizException.forbidden("只有管理员可以导入导出项目和流水线")


def parse_bundle(raw: object) -> dict:
    """校验导出包外壳。正文项目列表允许为空，但格式必须对。"""
    if not isinstance(raw, dict):
        raise BizException.bad_request("导入文件不是 JSON 对象")
    if raw.get("format") != FORMAT:
        raise BizException.bad_request(
            f"无法识别的文件格式（需要 {FORMAT}，实际是 {raw.get('format') or '空'}）"
        )
    try:
        version = int(raw.get("format_version") or 0)
    except (TypeError, ValueError) as exc:
        raise BizException.bad_request("format_version 无效") from exc
    if version < 1:
        raise BizException.bad_request("不支持的导出包版本")
    projects = raw.get("projects")
    if not isinstance(projects, list):
        raise BizException.bad_request("导出包缺少 projects 列表")
    return raw


def _group_payload(g: Group) -> dict:
    """环境分组配置。导入时按同名对齐，没有就新建，不覆盖已有分组的审批策略。"""
    return {
        "name": g.name,
        "type": g.type or "test",
        "description": g.description or "",
        "approval_required": bool(g.approval_required),
        "allow_self_approval": bool(g.allow_self_approval),
        "allow_emergency_bypass": bool(g.allow_emergency_bypass),
        "change_window": g.change_window or "",
        "pm_approval_required": bool(g.pm_approval_required),
    }


def _pipeline_payload(p: Pipeline, group: Group | None) -> dict:
    """一条流水线的可搬迁配置。source_id 只给导入时重写 run-pipeline 用。"""
    return {
        "source_id": p.id,
        "name": p.name,
        "description": p.description or "",
        "group_name": group.name if group is not None else "",
        "group_type": (group.type if group is not None else "") or "",
        "editor_view": p.editor_view or "form",
        "approval_mode": p.approval_mode or "inherit",
        "yaml": p.yaml or "",
    }


def export_catalog(
    db: Session,
    current: CurrentUser,
    *,
    project_ids: list[int] | None = None,
    pipeline_ids: list[int] | None = None,
) -> dict:
    """导出选定或全部项目和流水线。仅管理员。"""
    _require_admin(current)

    wanted_projects = {int(i) for i in (project_ids or []) if i}
    wanted_pipes = {int(i) for i in (pipeline_ids or []) if i}

    stmt = select(Pipeline).where(Pipeline.status.notin_(PIPELINE_HIDDEN)).order_by(Pipeline.id)
    pipes = list(db.scalars(stmt).all())
    if wanted_pipes:
        pipes = [p for p in pipes if p.id in wanted_pipes]
    if wanted_projects:
        pipes = [p for p in pipes if p.project_id in wanted_projects]
        extra_ids = wanted_projects
    else:
        extra_ids = {p.project_id for p in pipes}

    projects = list(db.scalars(select(Project).order_by(Project.id)).all())
    if wanted_projects or wanted_pipes:
        projects = [p for p in projects if p.id in extra_ids]
    if wanted_projects and not wanted_pipes:
        projects = [p for p in projects if p.id in wanted_projects]

    groups = list(db.scalars(select(Group).order_by(Group.id)).all())
    groups_by_project: dict[int, list[Group]] = {}
    group_by_id = {g.id: g for g in groups}
    for g in groups:
        groups_by_project.setdefault(g.project_id, []).append(g)

    pipes_by_project: dict[int, list[Pipeline]] = {}
    for p in pipes:
        pipes_by_project.setdefault(p.project_id, []).append(p)

    out_projects = []
    for proj in projects:
        out_projects.append(
            {
                "code": proj.code,
                "name": proj.name,
                "description": proj.description or "",
                "pm_enabled": bool(proj.pm_enabled),
                "groups": [_group_payload(g) for g in groups_by_project.get(proj.id, [])],
                "pipelines": [
                    _pipeline_payload(p, group_by_id.get(p.group_id))
                    for p in pipes_by_project.get(proj.id, [])
                ],
            }
        )
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projects": out_projects,
    }


def _find_project(db: Session, code: str) -> Project | None:
    code = (code or "").strip()
    if not code:
        return None
    return db.scalar(select(Project).where(Project.code == code))


def _find_pipeline(db: Session, project_id: int, name: str) -> Pipeline | None:
    name = (name or "").strip()
    if not name:
        return None
    return db.scalars(
        select(Pipeline).where(
            Pipeline.project_id == project_id,
            Pipeline.name == name,
            Pipeline.status.notin_(PIPELINE_HIDDEN),
        )
    ).first()


def preview_import(db: Session, current: CurrentUser, bundle: object) -> dict:
    """只读预览：列出将新建的项目，以及同名流水线冲突，供用户选覆盖或副本。仅管理员。"""
    _require_admin(current)
    data = parse_bundle(bundle)
    new_projects: list[dict] = []
    conflicts: list[dict] = []
    creates: list[dict] = []
    skipped: list[dict] = []
    for proj in data.get("projects") or []:
        if not isinstance(proj, dict):
            continue
        code = str(proj.get("code") or "").strip()
        if not code:
            skipped.append({"reason": "项目编码为空", "name": proj.get("name") or ""})
            continue
        existing = _find_project(db, code)
        if existing is None:
            new_projects.append(
                {
                    "code": code,
                    "name": str(proj.get("name") or code),
                    "pipeline_count": len(proj.get("pipelines") or []),
                    "can_create": True,
                }
            )
        target_id = existing.id if existing is not None else 0
        for item in proj.get("pipelines") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                skipped.append({"reason": "流水线名为空", "project_code": code})
                continue
            row = {
                "key": item_key(code, name),
                "project_code": code,
                "project_name": str(proj.get("name") or code),
                "name": name,
                "group_name": str(item.get("group_name") or ""),
            }
            if existing is None:
                creates.append(row)
                continue
            found = _find_pipeline(db, target_id, name)
            if found is None:
                creates.append(row)
            else:
                row["existing_id"] = found.id
                conflicts.append(row)
    return {
        "new_projects": new_projects,
        "conflicts": conflicts,
        "creates": creates,
        "skipped": skipped,
        "conflict_count": len(conflicts),
        "create_count": len(creates),
    }


def _ensure_project(db: Session, payload: dict, current: CurrentUser) -> Project:
    """按 code 找到项目；没有则管理员新建，并带上导出里的环境分组。"""
    code = str(payload.get("code") or "").strip()
    existing = _find_project(db, code)
    if existing is not None:
        return existing
    if not current.is_admin:
        raise BizException.forbidden(f"没有权限创建项目 {code}")
    name = str(payload.get("name") or code).strip() or code
    proj = Project(
        name=name,
        code=code,
        description=str(payload.get("description") or ""),
        pm_enabled=bool(payload.get("pm_enabled")),
    )
    db.add(proj)
    db.flush()
    groups = payload.get("groups") or []
    if not isinstance(groups, list) or not groups:
        db.add_all(
            [
                Group(
                    project_id=proj.id,
                    name="生产",
                    type="prod",
                    approval_required=True,
                    allow_self_approval=True,
                ),
                Group(project_id=proj.id, name="测试", type="test", approval_required=False),
            ]
        )
    else:
        for g in groups:
            if not isinstance(g, dict):
                continue
            _ensure_group(db, proj, g, create_missing=True)
    db.flush()
    return proj


def _ensure_group(db: Session, project: Project, payload: dict, *, create_missing: bool) -> Group | None:
    """同项目按分组名对齐。没有且允许创建时按导出配置新建。"""
    name = str(payload.get("name") or "").strip()
    gtype = str(payload.get("type") or "").strip()
    groups = list(db.scalars(select(Group).where(Group.project_id == project.id)).all())
    if name:
        for g in groups:
            if g.name == name:
                return g
    if gtype and not name:
        typed = [g for g in groups if (g.type or "") == gtype]
        if len(typed) == 1:
            return typed[0]
    if not create_missing or not name:
        return groups[0] if groups else None
    from app.core.env import SKIP_NODE_PUSH_APPROVAL, normalize_env

    env = normalize_env(gtype or "test", default="test", field="环境")
    need_approval = env not in SKIP_NODE_PUSH_APPROVAL
    g = Group(
        project_id=project.id,
        name=name,
        type=env,
        description=str(payload.get("description") or ""),
        approval_required=bool(payload.get("approval_required", need_approval)),
        allow_self_approval=bool(payload.get("allow_self_approval", need_approval)),
        allow_emergency_bypass=bool(payload.get("allow_emergency_bypass", False)),
        change_window=(payload.get("change_window") or "") or None,
        pm_approval_required=bool(payload.get("pm_approval_required", False)),
    )
    db.add(g)
    db.flush()
    return g


def _pick_group(db: Session, project: Project, item: dict) -> Group:
    """流水线要挂的分组：先同名，再同类型，再项目里第一条。缺了就按导出新建。"""
    g = _ensure_group(
        db,
        project,
        {"name": item.get("group_name") or "", "type": item.get("group_type") or ""},
        create_missing=True,
    )
    if g is None:
        raise BizException.bad_request(f"项目 {project.code} 没有可用的环境分组")
    return g


def _approval_for_import(raw: object, *, can_exempt: bool) -> str:
    """导入时的审批模式：没豁免权就把 exempt 收成 inherit，和复制流水线同一口径。"""
    mode = str(raw or "inherit").strip() or "inherit"
    if mode == "exempt" and not can_exempt:
        return "inherit"
    return assert_approval_mode(mode, can_exempt=can_exempt)


def apply_import(
    db: Session,
    current: CurrentUser,
    bundle: object,
    decisions: dict[str, str] | None = None,
) -> dict:
    """按预览结果落地。冲突项必须在 decisions 里给出 overwrite、copy 或 skip。仅管理员。"""
    _require_admin(current)
    data = parse_bundle(bundle)
    preview = preview_import(db, current, data)
    choices = {str(k): str(v) for k, v in (decisions or {}).items()}
    missing = [c["key"] for c in preview["conflicts"] if choices.get(c["key"]) not in CONFLICT_ACTIONS]
    if missing:
        raise BizException.bad_request(
            f"有 {len(missing)} 条重名流水线尚未选择「覆盖」「新建副本」或「跳过」，请先确认"
        )

    results: list[dict] = []
    id_map: dict[int, int] = {}
    imported: list[Pipeline] = []

    for proj_payload in data.get("projects") or []:
        if not isinstance(proj_payload, dict):
            continue
        code = str(proj_payload.get("code") or "").strip()
        if not code:
            continue
        try:
            project = _ensure_project(db, proj_payload, current)
        except BizException as exc:
            results.append({"project_code": code, "action": "skip", "reason": exc.message})
            continue
        for g in proj_payload.get("groups") or []:
            if isinstance(g, dict):
                _ensure_group(db, project, g, create_missing=True)

        for item in proj_payload.get("pipelines") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = item_key(code, name)
            source_id = int(item.get("source_id") or 0)
            found = _find_pipeline(db, project.id, name)
            action = ACTION_CREATE
            if found is not None:
                action = choices.get(key, "")
                if action == ACTION_SKIP:
                    results.append(
                        {
                            "key": key,
                            "name": name,
                            "project_code": code,
                            "action": ACTION_SKIP,
                            "reason": "用户选择跳过",
                        }
                    )
                    continue
                if action not in (ACTION_OVERWRITE, ACTION_COPY):
                    results.append(
                        {
                            "key": key,
                            "name": name,
                            "project_code": code,
                            "action": ACTION_SKIP,
                            "reason": "未选择如何处理重名",
                        }
                    )
                    continue
            try:
                pipe = _import_one(
                    db,
                    current,
                    project,
                    item,
                    action=action,
                    existing=found,
                )
            except BizException as exc:
                results.append(
                    {
                        "key": key,
                        "name": name,
                        "project_code": code,
                        "action": "error",
                        "reason": exc.message,
                    }
                )
                continue
            imported.append(pipe)
            if source_id > 0:
                id_map[source_id] = pipe.id
            results.append(
                {
                    "key": key,
                    "name": pipe.name,
                    "project_code": code,
                    "action": action if found is not None else ACTION_CREATE,
                    "id": pipe.id,
                }
            )

    # 全部落库后再改 run-pipeline 目标，避免源环境数字 id 指到本环境别人的线
    for pipe in imported:
        new_yaml = rewrite_run_pipeline_ids(pipe.yaml or "", id_map)
        new_yaml = sync_yaml_pipeline_name(new_yaml, pipe.name)
        if new_yaml != (pipe.yaml or ""):
            try:
                update_pipeline(
                    db,
                    pipe.id,
                    {"yaml": new_yaml},
                    is_admin=current.is_admin,
                    skip_nested_check=True,
                    operator_id=current.id,
                )
            except BizException:
                continue

    return {
        "imported": len([r for r in results if r.get("id")]),
        "results": results,
        "new_projects": preview["new_projects"],
        "conflict_count": preview["conflict_count"],
    }


def _import_one(
    db: Session,
    current: CurrentUser,
    project: Project,
    item: dict,
    *,
    action: str,
    existing: Pipeline | None,
) -> Pipeline:
    """导入一条流水线：新建、覆盖或 _copy。跳过不会走进这里。"""
    group = _pick_group(db, project, item)
    yaml_text = str(item.get("yaml") or "")
    description = str(item.get("description") or "")
    editor_view = assert_editor_view(item.get("editor_view") or "form")
    approval_mode = _approval_for_import(item.get("approval_mode"), can_exempt=True)
    name = str(item.get("name") or "").strip()

    if action == ACTION_OVERWRITE:
        if existing is None:
            raise BizException.bad_request(f"没有可覆盖的流水线「{name}」")
        payload = {
            "description": description,
            "yaml": yaml_text,
            "editor_view": editor_view,
            "approval_mode": approval_mode,
        }
        if existing.group_id != group.id:
            payload["group_id"] = group.id
        return update_pipeline(
            db,
            existing.id,
            payload,
            is_admin=True,
            can_exempt=True,
            operator_id=current.id,
            skip_nested_check=True,
        )

    new_name = name
    if action == ACTION_COPY:
        new_name = unique_pipeline_name(db, project.id, f"{name}_copy")
    yaml_text = sync_yaml_pipeline_name(yaml_text, new_name)
    return create_pipeline(
        db,
        {
            "project_id": project.id,
            "group_id": group.id,
            "name": new_name,
            "description": description,
            "yaml": yaml_text,
            "editor_view": editor_view,
            "approval_mode": approval_mode,
        },
        current.id,
        can_exempt=True,
        skip_nested_check=True,
    )
