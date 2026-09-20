"""统一身份：一个本地用户可同时用 LDAP 与企微登录（对齐 GitLab / Azure AD 账号绑定）。

匹配顺序：已绑定 wecom_userid → 邮箱（忽略大小写）→ 短账号 / sAMAccountName / 邮箱前缀。
命中多个账号时合并到主账号（权限、角色一并迁过去），禁止再开第二个用户。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.auth.models import ApiToken, Permission, PermissionApplication, User, UserRole


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _local_part(email: str) -> str:
    email = (email or "").strip()
    if "@" not in email:
        return email.lower()
    return email.split("@", 1)[0].lower()


def _is_placeholder_email(email: str) -> bool:
    """空邮箱，或历史上用来凑数的假域名，都不能当真实身份去匹配/覆盖。"""
    e = _norm(email)
    if not e or "@" not in e:
        return not e
    domain = e.rsplit("@", 1)[-1]
    return domain in {"wecom.local", "ldap.local", "deploy.local", "localhost", "example.com"}


def identities_of(u: User) -> list[str]:
    out: list[str] = []
    if (u.source or "") == "ldap":
        out.append("ldap")
    if u.wecom_userid or (u.source or "") == "wecom":
        out.append("wecom")
    if u.password_hash:
        out.append("local")
    if not out:
        out.append(u.source or "local")
    return out


def find_candidates(
    db: Session,
    *,
    wecom_userid: str = "",
    usernames: list[str] | None = None,
    emails: list[str] | None = None,
) -> list[User]:
    found: dict[int, User] = {}

    def add(u: User | None) -> None:
        if u is not None and "__merged" not in (u.username or ""):
            found[u.id] = u

    wid = (wecom_userid or "").strip()
    if wid:
        add(db.scalar(select(User).where(User.wecom_userid == wid)))
        add(db.scalar(select(User).where(func.lower(User.wecom_userid) == wid.lower())))

    for mail in emails or []:
        mail = (mail or "").strip()
        if _is_placeholder_email(mail):
            continue
        add(db.scalar(select(User).where(func.lower(User.email) == mail.lower())))

    names: list[str] = []
    for n in usernames or []:
        n = (n or "").strip()
        if not n:
            continue
        names.append(n)
        if "@" in n:
            names.append(n.split("@", 1)[0])
    for mail in emails or []:
        lp = _local_part(mail)
        if lp and not _is_placeholder_email(mail):
            names.append(lp)
    if wid:
        names.append(wid)
        if "@" in wid:
            names.append(wid.split("@", 1)[0])

    seen_name: set[str] = set()
    for name in names:
        key = name.lower()
        if not key or key in seen_name:
            continue
        seen_name.add(key)
        add(db.scalar(select(User).where(func.lower(User.username) == key)))
        add(db.scalar(select(User).where(func.lower(User.wecom_userid) == key)))
    return list(found.values())


def _keeper_score(u: User) -> tuple:
    from sqlalchemy.orm import object_session

    db = object_session(u)
    perm_n = 0
    if db is not None:
        perm_n = len(db.scalars(select(Permission.id).where(Permission.user_id == u.id)).all())
    return (
        1 if u.is_admin else 0,
        1 if (u.status or "active") == "active" else 0,
        perm_n,
        1 if (u.source or "") == "ldap" else 0,
        1 if u.wecom_userid else 0,
        1 if u.password_hash else 0,
        -(u.id or 0),
    )


def pick_keeper(users: list[User]) -> User:
    return sorted(users, key=_keeper_score, reverse=True)[0]


def _dedupe_permissions(db: Session, user_id: int) -> None:
    from app.core.deps import invalidate_permission_cache

    rows = db.scalars(select(Permission).where(Permission.user_id == user_id).order_by(Permission.id)).all()
    seen: set[tuple] = set()
    for p in rows:
        key = (p.resource_type, p.resource_id, p.action, p.effect)
        if key in seen:
            db.delete(p)
        else:
            seen.add(key)
    invalidate_permission_cache(db, user_id)


def _dedupe_roles(db: Session, user_id: int) -> None:
    rows = db.scalars(select(UserRole).where(UserRole.user_id == user_id).order_by(UserRole.id)).all()
    seen: set[int] = set()
    for r in rows:
        if r.role_id in seen:
            db.delete(r)
        else:
            seen.add(r.role_id)


def merge_users(db: Session, keeper: User, duplicate: User) -> None:
    """把重复账号的权限与业务数据迁到主账号，然后禁用并改名，避免再登录到影子号。"""
    if keeper.id == duplicate.id:
        return
    from_id, to_id = duplicate.id, keeper.id

    db.query(Permission).filter(Permission.user_id == from_id).update({Permission.user_id: to_id})
    db.query(UserRole).filter(UserRole.user_id == from_id).update({UserRole.user_id: to_id})
    db.query(ApiToken).filter(ApiToken.user_id == from_id).update({ApiToken.user_id: to_id})
    db.query(PermissionApplication).filter(PermissionApplication.applicant_id == from_id).update(
        {PermissionApplication.applicant_id: to_id}
    )
    db.query(PermissionApplication).filter(PermissionApplication.reviewer_id == from_id).update(
        {PermissionApplication.reviewer_id: to_id}
    )

    from app.modules.audit.models import AuditLog
    from app.modules.notify.models import InAppNotice
    from app.modules.ai.models import AiConversation, AiWatch
    from app.modules.pipeline.models import Pipeline, Release
    from app.modules.credential.models import Credential
    from app.modules.artifact.models import Artifact

    db.query(InAppNotice).filter(InAppNotice.user_id == from_id).update({InAppNotice.user_id: to_id})
    db.query(AiConversation).filter(AiConversation.user_id == from_id).update({AiConversation.user_id: to_id})
    db.query(AiWatch).filter(AiWatch.user_id == from_id).update({AiWatch.user_id: to_id})
    db.query(AuditLog).filter(AuditLog.user_id == from_id).update({AuditLog.user_id: to_id})
    db.query(Release).filter(Release.operator_id == from_id).update({Release.operator_id: to_id})
    db.query(Pipeline).filter(Pipeline.created_by == from_id).update({Pipeline.created_by: to_id})
    db.query(Credential).filter(Credential.created_by == from_id).update({Credential.created_by: to_id})
    db.query(Artifact).filter(Artifact.created_by == from_id).update({Artifact.created_by: to_id})

    if duplicate.wecom_userid and not keeper.wecom_userid:
        keeper.wecom_userid = duplicate.wecom_userid
    if duplicate.email and _is_placeholder_email(keeper.email) and not _is_placeholder_email(duplicate.email):
        keeper.email = duplicate.email
    if duplicate.is_admin:
        keeper.is_admin = True
    if (duplicate.source or "") == "ldap":
        keeper.source = "ldap"

    _dedupe_permissions(db, to_id)
    _dedupe_roles(db, to_id)

    duplicate.status = "disabled"
    duplicate.wecom_userid = None
    dup_name = (duplicate.username or f"user{from_id}")[:40]
    duplicate.username = f"{dup_name}__merged{from_id}"[:64]
    duplicate.email = ""
    db.commit()
    db.refresh(keeper)


def bind_profile(
    db: Session,
    user: User,
    *,
    wecom_userid: str = "",
    email: str = "",
    display_name: str = "",
    username: str = "",
    source: str = "",
) -> User:
    """把本次登录拿到的身份绑到同一条用户上，不新建。"""
    if wecom_userid and not user.wecom_userid:
        taken = db.scalar(select(User).where(User.wecom_userid == wecom_userid, User.id != user.id))
        if taken is None:
            user.wecom_userid = wecom_userid[:128]
        else:
            keeper = pick_keeper([user, taken])
            loser = taken if keeper.id == user.id else user
            merge_users(db, keeper, loser)
            user = db.get(User, keeper.id) or keeper
            if not user.wecom_userid:
                user.wecom_userid = wecom_userid[:128]

    if email and not _is_placeholder_email(email):
        other = db.scalar(
            select(User).where(func.lower(User.email) == email.lower(), User.id != user.id)
        )
        if other is not None:
            keeper = pick_keeper([user, other])
            loser = other if keeper.id == user.id else user
            merge_users(db, keeper, loser)
            user = db.get(User, keeper.id) or keeper
        if _is_placeholder_email(user.email) or source in ("ldap", "wecom"):
            user.email = email[:128]

    if display_name and source in ("ldap", "wecom"):
        user.display_name = display_name[:64]
    elif display_name and (not user.display_name or user.display_name == user.username):
        user.display_name = display_name[:64]

    want = (username or "").strip()
    if want and "@" not in want and want.lower() != (user.username or "").lower():
        clash = db.scalar(select(User).where(func.lower(User.username) == want.lower(), User.id != user.id))
        if clash is None and (user.source or "") in ("wecom", "ldap", "") and "@" in (user.username or ""):
            user.username = want[:64]

    if source == "ldap":
        user.source = "ldap"
    elif source == "wecom" and (user.source or "local") == "local" and not user.password_hash:
        user.source = "wecom"

    db.commit()
    db.refresh(user)
    return user


def resolve_or_provision(
    db: Session,
    *,
    wecom_userid: str = "",
    usernames: list[str] | None = None,
    emails: list[str] | None = None,
    display_name: str = "",
    source: str,
    auto_provision: bool,
) -> User | None:
    """登录入口：找到唯一本地用户；多个则合并；没有则按需建档。"""
    candidates = find_candidates(db, wecom_userid=wecom_userid, usernames=usernames, emails=emails)
    user: User | None = None
    if candidates:
        user = pick_keeper(candidates)
        for other in candidates:
            if other.id != user.id:
                merge_users(db, user, other)
                user = db.get(User, user.id)

    canon_user = ""
    for n in usernames or []:
        n = (n or "").strip()
        if n and "@" not in n:
            canon_user = n
            break
    if not canon_user:
        for n in usernames or []:
            n = (n or "").strip()
            if n:
                canon_user = n.split("@", 1)[0]
                break
    if not canon_user and wecom_userid:
        canon_user = wecom_userid.split("@", 1)[0]
    real_email = ""
    for e in emails or []:
        if e and not _is_placeholder_email(e):
            real_email = e.strip()
            break

    if user is None:
        if not auto_provision:
            return None
        username = (canon_user or real_email.split("@")[0] if real_email else wecom_userid or "user")[:64]
        clash = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
        if clash is not None:
            return bind_profile(
                db,
                clash,
                wecom_userid=wecom_userid,
                email=real_email,
                display_name=display_name,
                username=canon_user,
                source=source,
            )
        user = User(
            username=username,
            display_name=(display_name or username)[:64],
            email=(real_email or "")[:128],
            password_hash="",
            is_admin=False,
            wecom_userid=(wecom_userid or None),
            source=source,
            status="active",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return bind_profile(
        db,
        user,
        wecom_userid=wecom_userid,
        email=real_email,
        display_name=display_name,
        username=canon_user,
        source=source,
    )


def lookup_ldap_hints(cfg: dict, hints: list[str]) -> dict:
    """企微登录时用 userid/邮箱去 AD 拿规范短账号和邮箱，便于绑到同一人。"""
    if cfg.get("ldap_enabled") != "true":
        return {}
    from app.modules.auth.ldap_service import ldap_search

    seen: set[str] = set()
    for h in hints:
        h = (h or "").strip()
        if not h or h.lower() in seen:
            continue
        seen.add(h.lower())
        try:
            entry = ldap_search(cfg, h)
        except Exception:
            continue
        if entry:
            return entry
    return {}
