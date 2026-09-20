"""用户管理：本地用户 + LDAP 导入/启用停用。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.core.security import hash_password
from app.modules.auth.models import User


def public(u: User) -> dict:
    from app.modules.auth.identity import identities_of

    source = u.source or ("wecom" if u.wecom_userid else ("ldap" if not (u.password_hash or "") else "local"))
    ident = identities_of(u)
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name,
        "email": u.email,
        "is_admin": bool(u.is_admin),
        "source": source,
        "identities": ident,
        "status": u.status or "active",
        "wecom_userid": u.wecom_userid or "",
        "last_login_at": u.last_login_at.isoformat() if getattr(u, "last_login_at", None) else "",
        "created_at": u.created_at.isoformat() if u.created_at else "",
        "has_password": bool(u.password_hash),
    }


def list_users(db: Session, keyword: str = "", source: str = "", status: str = "") -> list[dict]:
    rows = db.scalars(select(User).order_by(User.id)).all()
    out = [public(u) for u in rows]
    kw = (keyword or "").strip().lower()
    if kw:
        out = [u for u in out if kw in f"{u['username']} {u['display_name']} {u['email']}".lower()]
    out = [u for u in out if "__merged" not in (u.get("username") or "")]
    if source:
        out = [u for u in out if u["source"] == source or source in (u.get("identities") or [])]
    if status:
        out = [u for u in out if u["status"] == status]
    return out


def create_local(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str = "",
    email: str = "",
    is_admin: bool = False,
) -> dict:
    username = (username or "").strip()
    if not username or "@" in username:
        raise BizException.bad_request("本地用户名不能为空，且不要带 @（带 @ 的走 LDAP）")
    if len(password or "") < 6:
        raise BizException.bad_request("密码至少 6 位")
    if db.scalar(select(User).where(User.username == username)):
        raise BizException.bad_request("用户名已存在")
    if email and db.scalar(select(User).where(User.email == email)):
        raise BizException.bad_request("邮箱已被占用")
    u = User(
        username=username,
        display_name=(display_name or username).strip(),
        email=(email or "").strip(),
        password_hash=hash_password(password),
        is_admin=bool(is_admin),
        source="local",
        status="active",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return public(u)


def update_user(
    db: Session,
    current: CurrentUser,
    user_id: int,
    *,
    display_name: str | None = None,
    email: str | None = None,
    is_admin: bool | None = None,
    status: str | None = None,
) -> dict:
    u = db.get(User, user_id)
    if u is None:
        raise BizException.not_found("用户")
    if display_name is not None:
        u.display_name = display_name.strip()
    if email is not None:
        email = email.strip()
        if email:
            other = db.scalar(select(User).where(User.email == email, User.id != u.id))
            if other:
                raise BizException.bad_request("邮箱已被占用")
        u.email = email
    if is_admin is not None:
        # 管理员开关只允许「给别人加/减」：自己关掉立刻没人能进系统管理页。
        if u.id == current.id and not is_admin:
            raise BizException.bad_request("不能取消自己的管理员")
        if u.is_admin and not is_admin:
            admins = list(db.scalars(select(User).where(User.is_admin.is_(True), User.status == "active")).all())
            if len(admins) <= 1:
                raise BizException.bad_request("至少保留一名启用中的管理员")
        u.is_admin = bool(is_admin)
    if status is not None:
        if status not in ("active", "disabled"):
            raise BizException.bad_request("状态只能是 active / disabled")
        if u.id == current.id and status == "disabled":
            raise BizException.bad_request("不能禁用当前登录账号")
        u.status = status
    db.commit()
    db.refresh(u)
    return public(u)


def reset_password(db: Session, user_id: int, password: str) -> dict:
    u = db.get(User, user_id)
    if u is None:
        raise BizException.not_found("用户")
    from app.modules.auth.identity import identities_of

    if "ldap" in identities_of(u) and not u.password_hash:
        raise BizException.bad_request("LDAP 用户密码由域控管理，不能在本系统重置")
    if len(password or "") < 6:
        raise BizException.bad_request("密码至少 6 位")
    u.password_hash = hash_password(password)
    if not u.source:
        u.source = "local"
    db.commit()
    return {"ok": True}


def delete_user(db: Session, current: CurrentUser, user_id: int) -> dict:
    u = db.get(User, user_id)
    if u is None:
        raise BizException.not_found("用户")
    if u.id == current.id:
        raise BizException.bad_request("不能删除自己")
    if u.is_admin:
        raise BizException.bad_request("请先取消管理员再删除")
    db.delete(u)
    db.commit()
    return {"ok": True}


def import_ldap_user(db: Session, entry: dict) -> dict:
    from app.modules.auth.identity import resolve_or_provision

    username = (entry.get("username") or "").strip()
    email = (entry.get("email") or "").strip()
    if not username:
        raise BizException.bad_request("LDAP 条目缺少用户名")
    user = resolve_or_provision(
        db,
        wecom_userid="",
        usernames=[username, entry.get("upn") or ""],
        emails=[email],
        display_name=entry.get("display_name") or username,
        source="ldap",
        auto_provision=True,
    )
    if user is None:
        raise BizException.bad_request("导入失败")
    return public(user)


def find_local_for_ldap(db: Session, ldap_username: str, email: str, login_name: str) -> User | None:
    if email:
        u = db.scalar(select(User).where(User.email == email))
        if u:
            return u
    names = [ldap_username, login_name]
    if "@" in login_name:
        names.append(login_name.split("@", 1)[0])
    for name in names:
        if not name:
            continue
        u = db.scalar(select(User).where(User.username == name))
        if u:
            return u
    return None
