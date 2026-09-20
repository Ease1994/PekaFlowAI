"""FastAPI 依赖注入：当前用户、数据库会话、权限校验。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.core.security import decode_access_token
from app.db.session import get_db
from app.modules.auth.models import Permission, Role, User, UserRole

# HTTP Bearer 认证
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    """当前登录用户上下文。"""

    id: int
    username: str
    is_admin: bool = False


def _current_from_row(u: User) -> CurrentUser:
    """用库里的账号生成请求上下文。管理员、用户名、禁用状态都以库为准。"""
    if (u.status or "active") != "active":
        raise BizException.unauthorized("账号已禁用，请联系管理员")
    return CurrentUser(id=u.id, username=u.username, is_admin=bool(u.is_admin))


def _current_from_session_jwt(db: Session, payload: dict) -> CurrentUser:
    """JWT 只证明「这个人登录过」；权限以当时库里的用户为准。"""
    user_id = int(payload.get("sub") or 0)
    if user_id <= 0:
        raise BizException.unauthorized("令牌无效或已过期")
    u = db.get(User, user_id)
    if u is None:
        raise BizException.unauthorized("账号不存在，请重新登录")
    return _current_from_row(u)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """解析 JWT 或 API Token，返回当前用户，并绑定操作审计上下文。

    登录 JWT 里的 adm 只是签发时的快照。用户管理里改管理员开关后，
    当前会话必须立刻按库生效，不能要求对方退出再登录。
    """
    if credentials is None:
        raise BizException.unauthorized("缺少认证令牌")
    token = credentials.credentials

    # 1. API Token（qx_ 前缀，拥有与用户同等权限）
    if token.startswith("qx_"):
        from datetime import datetime

        from app.core.security import hash_api_token
        from app.modules.auth.models import ApiToken

        at = (
            db.query(ApiToken)
            .filter(ApiToken.token_hash == hash_api_token(token), ApiToken.revoked.is_(False))
            .first()
        )
        if at is None:
            raise BizException.unauthorized("API Token 无效")
        if at.expires_at is not None and at.expires_at < datetime.now():
            raise BizException.unauthorized("API Token 已过期")
        u = db.get(User, at.user_id)
        if u is None:
            raise BizException.unauthorized("API Token 关联用户不存在")
        user = _current_from_row(u)
        _bind_audit(request, user, via_api_token=True)
        return user

    # 2. JWT：验签后按 sub 读库，不用票面上的 adm
    try:
        payload = decode_access_token(token)
    except Exception:
        raise BizException.unauthorized("令牌无效或已过期")

    if payload.get("purpose") in {"mfa_pending", "log_stream"}:
        # 密码过了、验证码还没过的短时票，以及日志 SSE 票，都不能当正式会话用
        raise BizException.unauthorized("请先完成双因子验证" if payload.get("purpose") == "mfa_pending" else "令牌无效或已过期")
    user = _current_from_session_jwt(db, payload)
    _bind_audit(request, user, via_api_token=False)
    return user


def _bind_audit(request: Request, user: CurrentUser, *, via_api_token: bool) -> None:
    """操作人记当前用户；source 标明入口，避免把用户名写成 AI。"""
    from app.modules.audit.context import bind_request

    path = request.url.path or ""
    if path.startswith("/api/v1/ai") or path.startswith("/mcp"):
        source = "ai"
    elif via_api_token:
        source = "api"
    else:
        source = "web"
    ip = request.client.host if request.client else ""
    bind_request(source=source, user_id=user.id, username=user.username, ip=ip)


class PermissionChecker:
    """细粒度 RBAC 校验器（资源级 + 操作级）。"""

    def __init__(self, resource_type: str, action: str):
        self.resource_type = resource_type
        self.action = action

    def __call__(
        self,
        resource_id: int,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> bool:
        if user.is_admin:
            return True

        perms = (
            db.query(Permission)
            .filter(Permission.user_id == user.id)
            .all()
        )

        allowed = False
        for p in perms:
            if p.action not in (self.action, "*"):
                continue
            matched = False
            if p.resource_type == "pipeline":
                matched = self.resource_type == "pipeline" and p.resource_id == resource_id
            elif p.resource_type == "group":
                matched = self.resource_type in ("group", "pipeline") and p.resource_id == resource_id
            elif p.resource_type == "project":
                matched = p.resource_id == resource_id
            if not matched:
                continue
            if p.effect == "deny":
                return False
            if p.effect == "allow":
                allowed = True
        if not allowed:
            raise BizException.forbidden(
                f"无权限：{self.resource_type} #{resource_id} 的 {self.action} 操作"
            )
        return True


def require_permission(resource_type: str, action: str) -> PermissionChecker:
    """构建权限校验依赖。"""
    return PermissionChecker(resource_type, action)


def _resolve_project_id(db: Session, resource_type: str, resource_id: int) -> int | None:
    """把任意资源映射回所属项目 id（角色权限作用域是项目）。"""
    if resource_type == "project":
        return resource_id
    if resource_type == "group":
        from app.modules.project.models import Group

        g = db.get(Group, resource_id)
        return g.project_id if g else None
    if resource_type == "pipeline":
        from app.modules.pipeline.models import Pipeline

        p = db.get(Pipeline, resource_id)
        return p.project_id if p else None
    if resource_type == "repository":
        from app.modules.repository.models import Repository

        r = db.get(Repository, resource_id)
        return r.project_id if r else None
    if resource_type == "credential":
        from app.modules.credential.models import Credential

        c = db.get(Credential, resource_id)
        return c.project_id if c else None
    if resource_type == "release":
        from app.modules.pipeline.models import Release

        r = db.get(Release, resource_id)
        return r.pipeline.project_id if r else None
    return None


def _role_projects_granting(
    db: Session,
    user: CurrentUser,
    resource_type: str,
    action: str,
    *,
    allow_wildcard: bool = True,
) -> set[int]:
    """用户靠项目角色能操作该类资源的项目 id。同一请求只查一次角色表。"""
    import json

    cache = db.info.setdefault("_role_action_projects", {})
    key = (user.id, resource_type, action, allow_wildcard)
    if key in cache:
        return cache[key]
    granted: set[int] = set()
    for project_id, raw in (
        db.query(Role.project_id, Role.permissions)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user.id)
        .all()
    ):
        try:
            perms = json.loads(raw or "{}")
        except Exception:
            perms = {}
        actions = perms.get(resource_type, [])
        if action in actions or (allow_wildcard and "*" in actions):
            granted.add(project_id)
    cache[key] = granted
    return granted


def _check_role_permission(
    db: Session,
    user: CurrentUser,
    resource_type: str,
    resource_id: int,
    action: str,
    *,
    allow_wildcard: bool = True,
) -> bool:
    """检查用户在资源所属项目下，是否有任一角色授予了该操作权限。"""
    project_id = _resolve_project_id(db, resource_type, resource_id)
    if project_id is None:
        return False
    return project_id in _role_projects_granting(
        db, user, resource_type, action, allow_wildcard=allow_wildcard
    )


def _user_permissions(db: Session, user_id: int) -> list[Permission]:
    """取用户的权限行，按请求缓存。

    check_permission 每调一次就把该用户的权限全量拉一遍，而列表接口是按资源逐条调的：
    visible_pipeline_ids 对每条流水线调一次，_pipeline_caps 再每条调五次。几千条流水线
    就是上万次一模一样的查询，列表接口从秒级掉到分钟级。

    db 是每请求一个 Session，缓存挂在它的 info 上，请求结束自然失效；
    同一请求内改了权限的，由 invalidate_permission_cache 显式清掉。
    """
    cache = db.info.setdefault("_perm_cache", {})
    if user_id not in cache:
        cache[user_id] = db.query(Permission).filter(Permission.user_id == user_id).all()
    return cache[user_id]


def warm_permission_cache(db: Session, user_ids: list[int]) -> None:
    """一次把多个用户的权限行灌进缓存。

    给「遍历一批用户逐个判权」的场景用（比如挑审批人）：不预热的话，
    每个用户各查一次，几百个用户就是几百次查询。
    """
    cache = db.info.setdefault("_perm_cache", {})
    missing = [uid for uid in set(user_ids) if uid not in cache]
    if not missing:
        return
    for uid in missing:
        cache[uid] = []
    for p in db.query(Permission).filter(Permission.user_id.in_(missing)).all():
        cache[p.user_id].append(p)


def invalidate_permission_cache(db: Session, user_id: int | None = None) -> None:
    """改动权限后清缓存，避免同一请求里赋完权又按旧数据判权。"""
    cache = db.info.get("_perm_cache")
    if cache:
        if user_id is None:
            cache.clear()
        else:
            cache.pop(user_id, None)
    db.info.pop("_perm_result_cache", None)
    db.info.pop("_role_action_projects", None)


def check_permission(
    db: Session,
    user: CurrentUser,
    resource_type: str,
    resource_id: int,
    action: str,
    *,
    allow_wildcard: bool = True,
) -> bool:
    """细粒度权限校验（返回 bool，不抛异常）。

    支持：
    - 三级继承：project（覆盖旗下所有）→ group（覆盖旗下 pipeline）→ pipeline（精确）。
    - action 通配符 "*"。
    - 项目级角色（Role/UserRole）：用户在资源所属项目下拥有的任一角色授予该操作即放行。

    allow_wildcard=False 用于那种「必须是明确授予的」权限，比如豁免发布审批：
    这类权限的意义就在于它和编辑权是分开的，要是被 "*" 顺带捎上，
    当初给某人一条流水线的全部权限，就等于悄悄给了他免审批发布的口子。
    """
    if user.is_admin:
        return True

    result_cache = db.info.setdefault("_perm_result_cache", {})
    cache_key = (user.id, resource_type, resource_id, action, allow_wildcard)
    if cache_key in result_cache:
        return result_cache[cache_key]

    perms = _user_permissions(db, user.id)
    allowed = False
    # 该节点所属的分组，懒加载：只有真遇到组授权才查，且一次判权最多查一遍
    node_groups: set[int] | None = None
    accepted = (action, "*") if allow_wildcard else (action,)
    for p in perms:
        if p.action not in accepted:
            continue

        matched = False
        if p.resource_type == "pipeline":
            matched = resource_type == "pipeline" and p.resource_id == resource_id
        elif p.resource_type == "node":
            # 节点不挂在项目下，没有继承一说：授了哪台就是哪台。
            # 拿到项目权限不等于能往这个项目用到的生产机上写文件
            matched = resource_type == "node" and p.resource_id == resource_id
        elif p.resource_type == "node_group":
            # 组授权覆盖组内所有节点：机器入组即生效，扩容不用挨个补授权
            if resource_type == "node":
                if node_groups is None:
                    node_groups = groups_of_node(db, resource_id)
                matched = p.resource_id in node_groups
            elif resource_type == "node_group":
                matched = p.resource_id == resource_id
        elif p.resource_type == "console":
            matched = resource_type == "console" and p.resource_id == resource_id
        elif p.resource_type == "group":
            if resource_type == "group":
                matched = p.resource_id == resource_id
            elif resource_type == "pipeline":
                from app.modules.pipeline.models import Pipeline
                pipe = db.get(Pipeline, resource_id)
                matched = pipe is not None and pipe.group_id == p.resource_id
        elif p.resource_type == "project":
            if resource_type == "project":
                matched = p.resource_id == resource_id
            elif resource_type == "pipeline":
                from app.modules.pipeline.models import Pipeline
                pipe = db.get(Pipeline, resource_id)
                matched = pipe is not None and pipe.project_id == p.resource_id
            elif resource_type == "group":
                from app.modules.project.models import Group
                g = db.get(Group, resource_id)
                matched = g is not None and g.project_id == p.resource_id

        if not matched:
            continue
        if p.effect == "deny":
            result_cache[cache_key] = False
            return False
        if p.effect == "allow":
            allowed = True
    if allowed:
        result_cache[cache_key] = True
        return True

    # 角色权限兜底
    ok = _check_role_permission(
        db, user, resource_type, resource_id, action, allow_wildcard=allow_wildcard
    )
    result_cache[cache_key] = ok
    return ok


def groups_of_node(db: Session, node_id: int) -> set[int]:
    """某个节点所属的分组 id。"""
    from app.modules.agent.models import NodeGroupMember

    return {
        gid
        for (gid,) in db.query(NodeGroupMember.group_id)
        .filter(NodeGroupMember.agent_id == node_id)
        .all()
    }


def deployable_node_ids(db: Session, user: CurrentUser) -> set[int] | None:
    """用户能下发文件的节点 id 集合；管理员返回 None 表示全部。

    列表场景专用。逐台调 check_permission 的话，几百台就是几百次全表扫权限 +
    几百次查分组，页面会明显卡住；这里权限表和分组成员各查一次就够了。
    判定规则与 check_permission 保持一致：直授的节点 + 已授权分组里的节点，
    再减去 deny。
    """
    if user.is_admin:
        return None
    from app.modules.agent.models import NodeGroupMember

    perms = (
        db.query(Permission)
        .filter(
            Permission.user_id == user.id,
            Permission.resource_type.in_(("node", "node_group")),
            Permission.action.in_(("deploy", "*")),
        )
        .all()
    )
    if not perms:
        return set()

    allow_groups = {p.resource_id for p in perms if p.resource_type == "node_group" and p.effect == "allow"}
    deny_groups = {p.resource_id for p in perms if p.resource_type == "node_group" and p.effect == "deny"}
    allowed = {p.resource_id for p in perms if p.resource_type == "node" and p.effect == "allow"}
    denied = {p.resource_id for p in perms if p.resource_type == "node" and p.effect == "deny"}

    if allow_groups or deny_groups:
        members = (
            db.query(NodeGroupMember.group_id, NodeGroupMember.agent_id)
            .filter(NodeGroupMember.group_id.in_(allow_groups | deny_groups))
            .all()
        )
        for gid, aid in members:
            if gid in deny_groups:
                denied.add(aid)
            else:
                allowed.add(aid)
    # deny 一票否决，和 check_permission 里遇到 deny 直接 return False 对齐
    return allowed - denied


def visible_project_ids(db: Session, user: CurrentUser) -> set[int] | None:
    """返回用户可见的项目 id 集合；管理员返回 None（表示全部可见）。

    可见规则：用户对「项目本身 / 项目下的分组 / 项目下的流水线」持有任意 allow 权限，
    即认为该项目可见（能从 pipeline/group 权限反推所属项目）。
    例如 dev.order 只有某条流水线的 execute 权限，也能看到该项目（否则无法点执行）。
    """
    if user.is_admin:
        return None

    perms = [p for p in _user_permissions(db, user.id) if p.effect == "allow"]
    project_ids: set[int] = set()
    pipeline_ids: set[int] = set()
    group_ids: set[int] = set()
    for p in perms:
        if p.resource_type == "project":
            project_ids.add(p.resource_id)
        elif p.resource_type == "pipeline":
            pipeline_ids.add(p.resource_id)
        elif p.resource_type == "group":
            group_ids.add(p.resource_id)

    if pipeline_ids:
        from app.modules.pipeline.models import Pipeline

        for (pid,) in (
            db.query(Pipeline.project_id).filter(Pipeline.id.in_(pipeline_ids)).all()
        ):
            project_ids.add(pid)
    if group_ids:
        from app.modules.project.models import Group

        for g in db.query(Group).filter(Group.id.in_(group_ids)).all():
            project_ids.add(g.project_id)

    # 角色反推：用户拥有角色的项目也可见
    for (pid,) in (
        db.query(Role.project_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .filter(UserRole.user_id == user.id)
        .all()
    ):
        project_ids.add(pid)

    return project_ids


def _pipeline_read_acl(
    db: Session, user: CurrentUser
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """拆出流水线 read 的 allow/deny 集合，规则与 check_permission 一致。

    deny 命中即否决；allow 可以来自流水线本身、所属分组或所属项目。
    """
    accepted = ("read", "*")
    allow: dict[str, set[int]] = {"pipeline": set(), "group": set(), "project": set()}
    deny: dict[str, set[int]] = {"pipeline": set(), "group": set(), "project": set()}
    for p in _user_permissions(db, user.id):
        if p.action not in accepted or p.resource_type not in allow:
            continue
        bucket = deny if p.effect == "deny" else allow if p.effect == "allow" else None
        if bucket is None:
            continue
        bucket[p.resource_type].add(p.resource_id)
    return allow, deny


def visible_pipeline_ids(db: Session, user: CurrentUser) -> set[int] | None:
    """用户可见的流水线 id；管理员返回 None 表示全部。

    项目可见不等于项目下每条流水线都可见：只申请并赋权了一条，就只能看到那一条。
    项目/分组级权限或项目角色仍会覆盖旗下流水线（与 check_permission 继承一致）。
    列表场景用集合运算，不再对每条流水线调一次判权、也不把 YAML 载入内存。
    """
    if user.is_admin:
        return None
    vis = visible_project_ids(db, user)
    if vis is not None and not vis:
        return set()
    from app.modules.pipeline.models import Pipeline

    allow, deny = _pipeline_read_acl(db, user)
    role_projects = _role_projects_granting(db, user, "pipeline", "read")
    q = db.query(Pipeline.id, Pipeline.project_id, Pipeline.group_id)
    if vis is not None:
        q = q.filter(Pipeline.project_id.in_(vis))
    visible: set[int] = set()
    for pid, project_id, group_id in q.all():
        if pid in deny["pipeline"] or group_id in deny["group"] or project_id in deny["project"]:
            continue
        if (
            pid in allow["pipeline"]
            or group_id in allow["group"]
            or project_id in allow["project"]
            or project_id in role_projects
        ):
            visible.add(pid)
    return visible


def require_pipeline_visible(db: Session, user: CurrentUser, pipeline_id: int) -> None:
    """无该流水线查看权则 403，避免靠项目可见性读到未授权流水线。

    详情/执行只判这一条，不先算出用户可见的全部流水线 id。
    """
    if user.is_admin:
        return
    if not check_permission(db, user, "pipeline", pipeline_id, "read"):
        raise BizException.forbidden(f"无权限访问流水线 #{pipeline_id}")


def require_project_visible(db: Session, user: CurrentUser, project_id: int) -> None:
    """校验用户对某项目可见，不可见则抛 403。"""
    ids = visible_project_ids(db, user)
    if ids is not None and project_id not in ids:
        raise BizException.forbidden(f"无权限访问项目 #{project_id}")


def verify_token(token: str, db: Session | None = None) -> CurrentUser | None:
    """从 token 解析用户（供 SSE、jar 下载等无法用 HTTPBearer 的场景）。

    传入 db 时管理员与禁用状态以库为准，和 get_current_user 同一套规则。
    未传 db 时只认 JWT 声明，给不方便拿会话的单测用。
    """
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    if payload.get("purpose") in {"mfa_pending", "log_stream"}:
        return None
    if db is not None:
        try:
            return _current_from_session_jwt(db, payload)
        except BizException:
            return None
    user_id = int(payload.get("sub", 0))
    username = payload.get("username", "")
    is_admin = bool(payload.get("adm", False))
    return CurrentUser(id=user_id, username=username, is_admin=is_admin)


def get_current_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """仅管理员可访问的依赖。非管理员直接 403。"""
    if not user.is_admin:
        raise BizException.forbidden("仅管理员可执行此操作")
    return user


def sees_menu(db: Session, user: CurrentUser, *keys: str) -> bool:
    """当前用户侧栏是否能看见 keys 里的任一菜单。管理员始终为真。"""
    if user.is_admin:
        return True
    from app.modules.auth.menus import visible_keys

    visible = set(visible_keys(db, is_admin=False, user_id=user.id))
    return any(key in visible for key in keys)


def assert_visible_menu(db: Session, user: CurrentUser, *keys: str) -> None:
    """看不见对应菜单则 403。用来把「谁能安装」绑到菜单管理，而不是另造一套权限表。"""
    if sees_menu(db, user, *keys):
        return
    raise BizException.forbidden("当前账号看不到对应菜单，无法执行该操作")
