"""权限管理服务（细粒度 RBAC 的增删改查）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.menus import CONSOLE_MODULES
from app.modules.auth.models import Permission, User
from app.modules.pipeline.models import Pipeline
from app.modules.project.models import Group, Project

# 各资源类型支持的操作。
# project / group 上的 execute、approve 靠 check_permission 的三级继承生效：
# 授在项目上 = 该项目下所有流水线，授在分组上 = 该分组下所有流水线，
# 这样给某人「整个项目的执行权」不用一条条勾流水线。
#
# approval_exempt = 允许把流水线设成「豁免发布审批」。刻意和 update 分开：
# 谁能改流水线 ≠ 谁能决定这条线免审批，否则被审批的人可以自己给自己开绿灯。
# 它也不吃 "*" 通配符（见 check_permission 的 allow_wildcard），必须明确授予。
RESOURCE_ACTIONS: dict[str, list[str]] = {
    "project": ["read", "create", "update", "delete", "execute", "approve", "approval_exempt"],
    "group": ["read", "create", "update", "delete", "execute", "approve", "approval_exempt"],
    "pipeline": ["read", "create", "update", "delete", "execute", "approval_exempt"],
    # 节点不参与项目的三级继承：拿到项目权限不等于能往这个项目用到的生产机上写文件。
    # deploy = 允许绕开流水线直接往这台机器的允许目录里写文件。
    # 机器多了以后按台授权维护不动，所以另开一级 node_group：授在组上，
    # 覆盖组内所有节点，机器进出组权限自动跟着走
    "node": ["deploy"],
    "node_group": ["deploy"],
    # 历史 console 授权仍能查名字。侧栏可见范围改走菜单管理，不再按人勾页面。
    "console": ["read"],
}

ACTION_LABELS: dict[str, str] = {
    "read": "查看",
    "create": "创建",
    "update": "更新",
    "delete": "删除",
    "execute": "执行",
    "approve": "审批",
    "approval_exempt": "豁免审批",
    "deploy": "下发文件",
    "*": "全部",
}


def list_users(db: Session) -> list[dict]:
    users = db.scalars(select(User).order_by(User.id)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "is_admin": u.is_admin,
            "source": getattr(u, "source", None) or "local",
            "status": getattr(u, "status", None) or "active",
            "email": u.email,
        }
        for u in users
    ]


def resource_name(db: Session, resource_type: str, resource_id: int) -> str:
    if resource_type == "project":
        p = db.get(Project, resource_id)
        return p.name if p else f"项目#{resource_id}"
    if resource_type == "group":
        g = db.get(Group, resource_id)
        return g.name if g else f"分组#{resource_id}"
    if resource_type == "pipeline":
        p = db.get(Pipeline, resource_id)
        return p.name if p else f"流水线#{resource_id}"
    if resource_type == "node":
        from app.modules.agent.models import BuildAgent

        a = db.get(BuildAgent, resource_id)
        return a.name if a else f"节点#{resource_id}"
    if resource_type == "node_group":
        from app.modules.agent.models import NodeGroup

        g = db.get(NodeGroup, resource_id)
        return g.name if g else f"节点组#{resource_id}"
    if resource_type == "console":
        return CONSOLE_MODULES.get(resource_id, f"控制台#{resource_id}")
    return f"资源#{resource_id}"


def resource_names(db: Session, resource_type: str, ids: set[int]) -> dict[int, str]:
    """批量取资源名。逐条 resource_name() 在列表里就是 N+1。"""
    if not ids:
        return {}
    model = None
    if resource_type == "project":
        model = Project
    elif resource_type == "group":
        model = Group
    elif resource_type == "pipeline":
        model = Pipeline
    elif resource_type == "node":
        from app.modules.agent.models import BuildAgent

        model = BuildAgent
    elif resource_type == "node_group":
        from app.modules.agent.models import NodeGroup

        model = NodeGroup
    elif resource_type == "console":
        return {rid: CONSOLE_MODULES[rid] for rid in ids if rid in CONSOLE_MODULES}
    if model is None:
        return {}
    rows = db.execute(select(model.id, model.name).where(model.id.in_(ids))).all()
    return {rid: name for rid, name in rows}


def list_permissions(db: Session, user_id: int | None = None, limit: int = 2000) -> list[dict]:
    """列权限。不带 user_id 时只返回前 limit 条——整张表可能有几十万行。"""
    stmt = select(Permission).order_by(Permission.id)
    if user_id is not None:
        stmt = stmt.where(Permission.user_id == user_id)
    perms = db.scalars(stmt.limit(limit)).all()
    if not perms:
        return []

    users = {
        uid: (uname, dname)
        for uid, uname, dname in db.execute(
            select(User.id, User.username, User.display_name).where(
                User.id.in_({p.user_id for p in perms})
            )
        ).all()
    }
    names: dict[str, dict[int, str]] = {}
    for rtype in {p.resource_type for p in perms}:
        names[rtype] = resource_names(
            db, rtype, {p.resource_id for p in perms if p.resource_type == rtype}
        )

    result = []
    for p in perms:
        uname, dname = users.get(p.user_id, (str(p.user_id), ""))
        result.append(
            {
                "id": p.id,
                "user_id": p.user_id,
                "username": uname,
                "display_name": dname or "",
                "resource_type": p.resource_type,
                "resource_id": p.resource_id,
                "resource_name": names.get(p.resource_type, {}).get(
                    p.resource_id, f"资源#{p.resource_id}"
                ),
                "action": p.action,
                "action_label": ACTION_LABELS.get(p.action, p.action),
                "effect": p.effect,
            }
        )
    return result


def _drop_perm_cache(db: Session, user_ids: list[int]) -> None:
    """权限一变就把请求内的判权缓存清掉，免得赋完权同一请求里还按旧数据判。"""
    from app.core.deps import invalidate_permission_cache

    for uid in set(user_ids):
        invalidate_permission_cache(db, uid)


def grant_permissions(
    db: Session,
    user_ids: list[int],
    resource_type: str,
    resource_ids: list[int],
    actions: list[str],
    *,
    commit: bool = True,
    audit: bool = True,
) -> dict:
    """批量赋权（笛卡尔积：用户 × 资源 × 操作），已存在的跳过。"""
    if not user_ids or not resource_ids or not actions:
        return {"created": 0}
    # 只查这次要写的那部分。原来是无条件 select(Permission)：为了判重把整张权限表
    # 拉进内存，几百用户 × 几百流水线 × 几种操作就是几十万行，赋一次权就能把内存打满
    existing: set[tuple] = {
        row
        for row in db.execute(
            select(
                Permission.user_id,
                Permission.resource_type,
                Permission.resource_id,
                Permission.action,
            ).where(
                Permission.user_id.in_(user_ids),
                Permission.resource_type == resource_type,
                Permission.resource_id.in_(resource_ids),
                Permission.action.in_(actions),
            )
        ).all()
    }
    created = 0
    for uid in user_ids:
        for rid in resource_ids:
            for action in actions:
                key = (uid, resource_type, rid, action)
                if key in existing:
                    continue
                db.add(
                    Permission(
                        user_id=uid,
                        resource_type=resource_type,
                        resource_id=rid,
                        action=action,
                        effect="allow",
                    )
                )
                existing.add(key)
                created += 1
    from app.modules.audit.service import write as write_audit

    if audit and created:
        write_audit(
            db,
            "permission.grant",
            resource_type,
            resource_ids[0] if len(resource_ids) == 1 else None,
            f"赋权 users={user_ids} resources={resource_ids} actions={actions} created={created}",
        )
    if commit:
        db.commit()
    _drop_perm_cache(db, user_ids)
    return {"created": created}


def revoke_permissions(db: Session, permission_ids: list[int]) -> dict:
    """批量删除权限。"""
    deleted = 0
    touched: list[int] = []
    for pid in permission_ids:
        p = db.get(Permission, pid)
        if p is not None:
            touched.append(p.user_id)
            db.delete(p)
            deleted += 1
    _drop_perm_cache(db, touched)
    if deleted:
        from app.modules.audit.service import write as write_audit

        write_audit(
            db,
            "permission.revoke",
            "permission",
            permission_ids[0] if len(permission_ids) == 1 else None,
            f"撤销权限 ids={permission_ids} deleted={deleted}",
        )
    db.commit()
    return {"deleted": deleted}
