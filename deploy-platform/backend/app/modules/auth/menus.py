"""侧栏菜单：工作台/系统管理按锁定范围，资源与工具按用户取消。

工作台给所有登录用户，系统管理固定仅管理员，这两块不能改。
资源与工具默认每个登录用户都能看见；管理员在菜单管理里按人关掉后，该用户侧栏和对应接口一起收掉。
业务数据权限仍走用户授权，不在这里勾项目或流水线。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.modules.auth.models import User, UserMenuDeny
from app.modules.settings.models import PlatformSetting

# 落在 platform_setting 里的 JSON：{"artifacts":"admin","ai":"all"}。锁定项忽略这份配置。
SETTING_KEY = "menu_audience"
# all = 登录即可看见；admin = 仅管理员。锁定项不允许改。
AUDIENCE_ALL = "all"
AUDIENCE_ADMIN = "admin"


@dataclass(frozen=True)
class MenuItem:
    """侧栏上的一项。key 和前端路由一致。"""

    key: str
    label: str
    group: str
    group_label: str
    default_audience: str
    locked: bool
    console_id: int | None = None


# 和工作台 / 资源 / 系统三组对应侧栏从上到下的顺序。
MENU_ITEMS: tuple[MenuItem, ...] = (
    MenuItem("projects", "项目与流水线", "workbench", "工作台", AUDIENCE_ALL, True),
    MenuItem("pm", "项目工作台", "workbench", "工作台", AUDIENCE_ALL, True),
    MenuItem("deploy-requests", "发布计划", "workbench", "工作台", AUDIENCE_ALL, True),
    MenuItem("approvals", "发布审批", "workbench", "工作台", AUDIENCE_ALL, True),
    MenuItem("permissions", "权限管理", "workbench", "工作台", AUDIENCE_ALL, True),
    MenuItem("artifacts", "制品库", "tools", "资源与工具", AUDIENCE_ALL, False, 1),
    MenuItem("agents", "构建机管理", "tools", "资源与工具", AUDIENCE_ALL, False, 2),
    MenuItem("nodes", "节点管理", "tools", "资源与工具", AUDIENCE_ALL, False, 3),
    MenuItem("skills", "技能库", "tools", "资源与工具", AUDIENCE_ALL, False, 7),
    MenuItem("credentials", "凭证管理", "tools", "资源与工具", AUDIENCE_ALL, False, 4),
    MenuItem("models", "模型管理", "tools", "资源与工具", AUDIENCE_ALL, False, 5),
    MenuItem("ai", "AI Agent", "tools", "资源与工具", AUDIENCE_ALL, False, 6),
    MenuItem("handbook", "调用手册", "tools", "资源与工具", AUDIENCE_ALL, False),
    MenuItem("dashboard", "指标大盘", "system", "系统管理", AUDIENCE_ADMIN, True),
    MenuItem("releases", "发布管理", "system", "系统管理", AUDIENCE_ADMIN, True),
    MenuItem("users", "用户管理", "system", "系统管理", AUDIENCE_ADMIN, True),
    MenuItem("settings", "平台设置", "system", "系统管理", AUDIENCE_ADMIN, True),
)

# 旧的 console 授权 resource_id 仍用这张表显示名字，不再拿来控制侧栏。
CONSOLE_MODULES: dict[int, str] = {
    item.console_id: item.label for item in MENU_ITEMS if item.console_id is not None
}
_BY_KEY = {item.key: item for item in MENU_ITEMS}
# 资源与工具：未锁定，默认每人可见，只能按用户取消。
TOOL_KEYS: tuple[str, ...] = tuple(item.key for item in MENU_ITEMS if not item.locked)


def _parse_audience(raw: str) -> dict[str, str]:
    """读库里的 JSON。坏数据当没配过，回落到每项的默认可见范围。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in data.items():
        if key not in _BY_KEY:
            continue
        text = str(value).strip().lower()
        if text in {AUDIENCE_ALL, AUDIENCE_ADMIN}:
            out[str(key)] = text
    return out


def load_audience(db: Session) -> dict[str, str]:
    """当前生效的可见范围。锁定项永远用目录里的默认值。"""
    row = db.scalar(select(PlatformSetting).where(PlatformSetting.key == SETTING_KEY))
    stored = _parse_audience(row.value if row is not None else "")
    out: dict[str, str] = {}
    for item in MENU_ITEMS:
        if item.locked:
            out[item.key] = item.default_audience
        else:
            out[item.key] = stored.get(item.key, item.default_audience)
    return out


def save_audience(db: Session, audience: dict) -> dict[str, str]:
    """只保存未锁定项。锁定项即使传上来也忽略。"""
    cleaned: dict[str, str] = {}
    for key, value in (audience or {}).items():
        item = _BY_KEY.get(str(key))
        if item is None or item.locked:
            continue
        text = str(value).strip().lower()
        if text not in {AUDIENCE_ALL, AUDIENCE_ADMIN}:
            continue
        cleaned[item.key] = text
    payload = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    row = db.scalar(select(PlatformSetting).where(PlatformSetting.key == SETTING_KEY))
    if row is None:
        db.add(PlatformSetting(key=SETTING_KEY, value=payload, description="侧栏菜单可见范围"))
    else:
        row.value = payload
    from app.modules.audit.service import write as write_audit

    write_audit(db, "menu.audience", "menu", None, f"菜单可见范围 {payload}")
    db.commit()
    return load_audience(db)


def load_denied_keys(db: Session, user_id: int) -> set[str]:
    """读某个用户被取消的资源菜单。未知 key 丢掉，避免脏数据把侧栏打乱。"""
    rows = db.scalars(select(UserMenuDeny.menu_key).where(UserMenuDeny.user_id == user_id)).all()
    return {key for key in rows if key in TOOL_KEYS}


def _require_staff_user(db: Session, user_id: int) -> User:
    """菜单取消只对普通用户生效。找不到人或管理员都拒绝，避免误伤工作台。"""
    user = db.get(User, user_id)
    if user is None:
        raise BizException.not_found("用户")
    if user.is_admin:
        raise BizException.bad_request("管理员始终可见全部菜单，不能取消")
    return user


def save_denied_keys(db: Session, user_id: int, denied: list) -> list[str]:
    """用传入列表整表替换该用户的取消项。空列表 = 恢复默认，八个资源菜单都可见。

    只接受资源与工具的 key；工作台、系统管理、未知名字一律丢掉。
    """
    _require_staff_user(db, user_id)
    cleaned: list[str] = []
    for raw in denied:
        key = str(raw).strip()
        if key in TOOL_KEYS and key not in cleaned:
            cleaned.append(key)
    db.execute(delete(UserMenuDeny).where(UserMenuDeny.user_id == user_id))
    for key in cleaned:
        db.add(UserMenuDeny(user_id=user_id, menu_key=key))
    from app.modules.audit.service import write as write_audit

    write_audit(
        db,
        "menu.user_deny",
        "user",
        user_id,
        f"用户#{user_id} 取消菜单 {cleaned or '无'}",
    )
    db.commit()
    return cleaned


def user_tool_assignment(db: Session, user_id: int) -> dict:
    """菜单管理页：某个用户当前被取消 / 仍保留的资源菜单。"""
    user = db.get(User, user_id)
    if user is None:
        raise BizException.not_found("用户")
    denied = [] if user.is_admin else sorted(load_denied_keys(db, user_id))
    granted = [key for key in TOOL_KEYS if key not in denied]
    return {
        "user_id": user_id,
        "is_admin": bool(user.is_admin),
        "denied": denied,
        "granted": granted,
    }


def visible_keys(db: Session, *, is_admin: bool, user_id: int | None = None) -> list[str]:
    """当前用户侧栏该出现哪些 key。

    管理员看见全部。工作台、系统管理按锁定范围。
    资源与工具默认全员可见，再扣掉该用户在菜单管理里取消的项。
    """
    denied: set[str] = set()
    if user_id is not None and not is_admin:
        denied = load_denied_keys(db, user_id)
    keys: list[str] = []
    for item in MENU_ITEMS:
        if is_admin:
            keys.append(item.key)
            continue
        if item.locked:
            if item.default_audience == AUDIENCE_ALL:
                keys.append(item.key)
            continue
        if item.key not in denied:
            keys.append(item.key)
    return keys


def catalog_public(db: Session, *, is_admin: bool, user_id: int | None = None) -> dict:
    """给前端的菜单树 + 当前用户可见列表。"""
    audience = load_audience(db)
    items = []
    for item in MENU_ITEMS:
        who = audience.get(item.key, item.default_audience)
        items.append(
            {
                "key": item.key,
                "label": item.label,
                "group": item.group,
                "group_label": item.group_label,
                "audience": who,
                "locked": item.locked,
            }
        )
    return {
        "items": items,
        "visible": visible_keys(db, is_admin=is_admin, user_id=user_id),
    }
