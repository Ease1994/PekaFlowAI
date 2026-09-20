"""项目与分组路由（按项目隔离）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import (
    CurrentUser,
    check_permission,
    get_current_admin,
    get_current_user,
    require_project_visible,
    visible_project_ids,
)
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.project.models import Group, Project

router = APIRouter(tags=["项目与分组"])


# ---- 项目 ----
@router.get("/projects", summary="项目列表（按权限隔离）")
def list_projects(db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    visible = visible_project_ids(db, current)
    if visible is None:
        projects = db.scalars(select(Project).order_by(Project.id)).all()
    elif not visible:
        return R.ok([])
    else:
        projects = db.scalars(
            select(Project).where(Project.id.in_(visible)).order_by(Project.id)
        ).all()
    return R.ok(list(projects))


@router.post("/projects", summary="创建项目（仅管理员）")
def create_project(
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    exists = db.scalar(select(Project).where(Project.code == body["code"]))
    if exists:
        raise BizException.bad_request(f"项目编码已存在: {body['code']}")
    p = Project(name=body["name"], code=body["code"], description=body.get("description", ""))
    db.add(p)
    db.flush()
    # 新项目至少有生产和测试两套环境，否则流水线无处可挂。
    # UAT / 预发 / 开发需要的话在项目详情里再加，不替每个项目预创建一堆空组。
    db.add_all(
        [
            Group(
                project_id=p.id, name="生产", type="prod",
                approval_required=True, allow_self_approval=True,
            ),
            Group(project_id=p.id, name="测试", type="test", approval_required=False),
        ]
    )
    db.commit()
    db.refresh(p)
    return R.ok(p)


@router.get("/projects/{project_id}", summary="项目详情")
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    p = db.get(Project, project_id)
    if p is None:
        raise BizException.not_found("项目")
    require_project_visible(db, current, project_id)
    from app.core.response import _serialize

    d = _serialize(p)
    d["can_create_pipeline"] = check_permission(db, current, "project", project_id, "create")
    d["can_delete_group"] = check_permission(db, current, "project", project_id, "delete")
    return R.ok(d)


@router.put("/projects/{project_id}", summary="更新项目（仅管理员）")
def update_project(
    project_id: int,
    body: dict,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    p = db.get(Project, project_id)
    if p is None:
        raise BizException.not_found("项目")
    p.name = body.get("name", p.name)
    p.description = body.get("description", p.description)
    db.commit()
    db.refresh(p)
    return R.ok(p)


@router.delete("/projects/{project_id}", summary="删除项目（仅管理员）")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_admin),
):
    from app.modules.project.service import delete_project as remove_project

    remove_project(db, project_id)
    return R.ok()


# ---- 分组 ----
@router.get("/groups", summary="分组列表（按项目隔离）")
def list_groups(
    project_id: int | None = None,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    if project_id:
        require_project_visible(db, current, project_id)
    stmt = select(Group).order_by(Group.id)
    if project_id:
        stmt = stmt.where(Group.project_id == project_id)
    else:
        visible = visible_project_ids(db, current)
        if visible is not None:
            if not visible:
                return R.ok([])
            stmt = stmt.where(Group.project_id.in_(visible))
    return R.ok(list(db.scalars(stmt).all()))


@router.post("/groups", summary="创建分组")
def create_group(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    from app.core.env import SKIP_NODE_PUSH_APPROVAL, normalize_env

    project_id = body["project_id"]
    if not check_permission(db, current, "project", project_id, "create"):
        raise BizException.forbidden(f"无权限：在项目 #{project_id} 创建分组")
    gtype = normalize_env(body.get("type"), default="test", field="环境")
    # test/dev 默认免审；prod/uat/staging/自定义一律要审（失败关闭）
    need_approval = gtype not in SKIP_NODE_PUSH_APPROVAL
    g = Group(
        project_id=project_id,
        name=(body.get("name") or "").strip(),
        type=gtype,
        description=body.get("description", ""),
        approval_required=body.get("approval_required", need_approval),
        allow_self_approval=body.get("allow_self_approval", need_approval),
        allow_emergency_bypass=body.get("allow_emergency_bypass", False),
        pm_approval_required=body.get("pm_approval_required", False),
        change_window=(body.get("change_window") or "") or None,
    )
    if not g.name:
        raise BizException.bad_request("分组名不能为空")
    dup = db.scalar(
        select(Group.id).where(Group.project_id == project_id, Group.name == g.name)
    )
    if dup:
        raise BizException.bad_request(f"本项目已有名为「{g.name}」的环境分组")
    db.add(g)
    db.commit()
    db.refresh(g)
    return R.ok(g)


@router.put("/groups/{group_id}", summary="更新分组")
def update_group(
    group_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    g = db.get(Group, group_id)
    if g is None:
        raise BizException.not_found("分组")
    if not check_permission(db, current, "project", g.project_id, "update"):
        raise BizException.forbidden(f"无权限：更新项目 #{g.project_id} 的分组")
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise BizException.bad_request("分组名不能为空")
        if name != g.name:
            dup = db.scalar(
                select(Group.id).where(
                    Group.project_id == g.project_id,
                    Group.name == name,
                    Group.id != group_id,
                )
            )
            if dup:
                raise BizException.bad_request(f"本项目已有名为「{name}」的环境分组")
        g.name = name
    if "type" in body:
        from app.core.env import normalize_env
        from app.modules.pipeline.models import Pipeline

        new_type = normalize_env(body.get("type"), default="", field="环境")
        if not new_type:
            raise BizException.bad_request("环境不能为空")
        if new_type != g.type:
            # 改环境码等于整组流水线换隔离域：构建机、节点、审批默认值都会对不上。
            # 回收站里的线恢复后也还挂着这个 group_id，所以软删的也算。
            hung = db.scalar(select(Pipeline.id).where(Pipeline.group_id == group_id))
            if hung:
                raise BizException.bad_request(
                    f"「{g.name}」下还有流水线（含回收站），不能改环境码。"
                    "请先把流水线挪到别的环境分组，或彻底删除后再改"
                )
            g.type = new_type
    g.description = body.get("description", g.description)
    if "approval_required" in body:
        g.approval_required = body["approval_required"]
    if "allow_self_approval" in body:
        g.allow_self_approval = body["allow_self_approval"]
    if "allow_emergency_bypass" in body:
        g.allow_emergency_bypass = body["allow_emergency_bypass"]
    if "pm_approval_required" in body:
        g.pm_approval_required = bool(body["pm_approval_required"])
    if "change_window" in body:
        g.change_window = (body.get("change_window") or "") or None
    db.commit()
    db.refresh(g)
    return R.ok(g)


@router.delete("/groups/{group_id}", summary="删除环境分组")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """有流水线挂着就不能删：挪走或先删流水线。空组才能拿掉。

    回收站里的线恢复后还挂着这个 group_id，所以软删的也挡住——
    不然组删了、线从回收站捞回来，所属环境分组就丢了。
    """
    from app.modules.pipeline.models import Pipeline

    g = db.get(Group, group_id)
    if g is None:
        raise BizException.not_found("分组")
    if not check_permission(db, current, "project", g.project_id, "delete"):
        raise BizException.forbidden(f"无权限：删除项目 #{g.project_id} 的分组")
    busy = db.scalar(select(Pipeline.id).where(Pipeline.group_id == group_id))
    if busy:
        raise BizException.bad_request(
            f"「{g.name}」下还有流水线（含回收站），不能删除。"
            "请先把流水线挪到别的环境分组，或从回收站彻底删除"
        )
    db.delete(g)
    db.commit()
    return R.ok()


@router.get("/env-kinds", summary="环境码表（分组/构建机/节点共用）")
def list_env_kinds(_: CurrentUser = Depends(get_current_user)):
    from app.core.env import KNOWN, SKIP_NODE_PUSH_APPROVAL

    return R.ok(
        [
            {
                "code": code,
                "label": label,
                "skip_node_push_approval": code in SKIP_NODE_PUSH_APPROVAL,
                "default_approval": code not in SKIP_NODE_PUSH_APPROVAL,
            }
            for code, label in KNOWN.items()
        ]
    )
