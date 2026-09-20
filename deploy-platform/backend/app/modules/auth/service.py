"""认证与 RBAC 服务。"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.core.security import create_access_token, hash_password, verify_password
from app.modules.auth.identity import _is_placeholder_email, identities_of
from app.modules.auth.models import Permission, User
from app.modules.auth.schemas import LoginResponse, UserInfo
from app.modules.auth.totp import (
    new_secret,
    provisioning_uri,
    qr_svg,
    seal_secret,
    unseal_secret,
    verify_code,
)
from app.modules.settings.branding import session_expire_seconds

_PENDING_TTL = 5 * 60
_RESET_TTL_HOURS = 2
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _user_info(user: User) -> UserInfo:
    """登录和 /me 共用的用户视图。must_set_email 只约束本地管理员。"""
    local_admin = bool(user.is_admin) and "local" in identities_of(user)
    return UserInfo(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email or "",
        is_admin=user.is_admin,
        source=user.source or "local",
        totp_enrolled=bool(user.totp_enrolled),
        must_set_email=local_admin and _is_placeholder_email(user.email or ""),
    )


def _issue_session(db: Session, user: User) -> LoginResponse:
    """密码（以及双因子）都过了之后签发正式会话。有效期跟平台 Session 天数走。"""
    user.last_login_at = datetime.now()
    db.commit()
    token = create_access_token(
        user.id, user.username, user.is_admin, expire_seconds=session_expire_seconds(db)
    )
    return LoginResponse(token=token, user=_user_info(user), status="ok")


def _issue_pending(user: User) -> str:
    """密码已对、双因子未完成：短时票，不能当业务 JWT 用。"""
    return create_access_token(
        user.id,
        user.username,
        user.is_admin,
        expire_seconds=_PENDING_TTL,
        extra={"purpose": "mfa_pending"},
    )


def _user_from_pending(db: Session, pending_token: str) -> User:
    from app.core.security import decode_access_token

    try:
        payload = decode_access_token(pending_token)
    except Exception as exc:  # noqa: BLE001
        raise BizException.unauthorized("双因子验证已过期，请重新登录") from exc
    if payload.get("purpose") != "mfa_pending":
        raise BizException.unauthorized("请重新登录后再输入验证码")
    user = db.get(User, int(payload.get("sub") or 0))
    if user is None or (user.status or "active") != "active":
        raise BizException.unauthorized("账号不可用，请重新登录")
    return user


def _totp_enabled(db: Session) -> bool:
    from app.modules.settings import get_all_settings

    return get_all_settings(db).get("totp_2fa_enabled", "false") == "true"


def _issuer_name(db: Session) -> str:
    from app.modules.settings import get_all_settings
    from app.modules.settings.branding import clamp_display_name

    return clamp_display_name(get_all_settings(db).get("platform_display_name"))


def authenticate(
    db: Session,
    username: str,
    password: str,
    *,
    client_ip: str = "",
) -> LoginResponse:
    """校验账号密码。平台开了双因子则只发 pending_token，正式会话等验证码。

    同一用户名+IP 密码错 5 次锁 15 分钟。对外始终「用户名或密码错误」。
    """
    from app.modules.auth import lockout
    from app.modules.settings import get_all_settings

    settings = get_all_settings(db)
    name = (username or "").strip()
    ldap_on = settings.get("ldap_enabled", "false") == "true"
    ip = (client_ip or "").strip()

    if lockout.password_locked(name, ip):
        raise BizException.unauthorized("用户名或密码错误")

    # 1. 短账号走本地密码，带 @ 的走 LDAP；本地失败且开了 LDAP 再试域
    if "@" in name:
        if not ldap_on:
            lockout.record_password_failure(name, ip)
            raise BizException.unauthorized(
                "LDAP 未启用。请用本地短账号登录，或让管理员在「平台设置」打开 LDAP。"
            )
        try:
            user = _authenticate_ldap(db, name, password, settings)
        except BizException as e:
            if e.code == 401:
                lockout.record_password_failure(name, ip)
                raise BizException.unauthorized("用户名或密码错误") from e
            raise
    else:
        user = db.scalar(select(User).where(func.lower(User.username) == name.lower()))
        if user is not None and "__merged" in (user.username or ""):
            user = None
        if user is None or not (user.password_hash or "") or not verify_password(password, user.password_hash):
            if ldap_on:
                try:
                    user = _authenticate_ldap(db, name, password, settings)
                except BizException as e:
                    if e.code == 401:
                        lockout.record_password_failure(name, ip)
                        raise BizException.unauthorized("用户名或密码错误") from e
                    raise
            else:
                lockout.record_password_failure(name, ip)
                raise BizException.unauthorized("用户名或密码错误")

    lockout.clear_password_failures(name, ip)

    if (user.status or "active") != "active":
        raise BizException.forbidden("账号已禁用，请联系管理员")

    # 2. 未开双因子：直接签发正式会话（有效期跟 Session 天数走）
    if not _totp_enabled(db):
        return _issue_session(db, user)

    # 3. 已绑定只问 6 位码；未绑定先发二维码，密钥只放库里的 pending 字段
    pending = _issue_pending(user)
    if user.totp_enrolled and unseal_secret(user.totp_secret):
        return LoginResponse(
            status="totp_required",
            pending_token=pending,
            user=_user_info(user),
            message="请输入 Authenticator 中的 6 位验证码",
        )

    secret = new_secret()
    user.totp_pending_secret = seal_secret(secret)
    db.commit()
    uri = provisioning_uri(secret, user.username, _issuer_name(db))
    return LoginResponse(
        status="totp_setup",
        pending_token=pending,
        user=_user_info(user),
        otpauth_uri=uri,
        qr_svg=qr_svg(uri),
        message="请用 Authenticator 扫描二维码，然后输入当前 6 位验证码完成绑定",
    )


def complete_totp_login(
    db: Session,
    pending_token: str,
    code: str,
    *,
    client_ip: str = "",
) -> LoginResponse:
    """用 pending 票 + 6 位码换正式会话。首次扫码成功后把 pending 密钥升为正式密钥。

    同一用户名+IP 验证码错 8 次作废 pending，必须重新登录。
    """
    from app.modules.auth import lockout

    if lockout.pending_voided(pending_token):
        raise BizException.unauthorized("双因子验证已锁定，请重新登录")
    user = _user_from_pending(db, pending_token)
    ip = (client_ip or "").strip()
    if lockout.totp_locked(user.username, ip):
        raise BizException.unauthorized("双因子验证已锁定，请重新登录")
    # 设置中心里途关掉双因子：pending 票还在的话直接放行，不必卡在扫码页
    if not _totp_enabled(db):
        lockout.clear_totp_failures(user.username, ip)
        return _issue_session(db, user)
    enrolled = bool(user.totp_enrolled) and bool(unseal_secret(user.totp_secret))
    sealed = user.totp_secret if enrolled else user.totp_pending_secret
    secret = unseal_secret(sealed)
    if not secret or not verify_code(secret, code):
        n = lockout.record_totp_failure(user.username, ip, pending_token)
        if n >= lockout.TOTP_LIMIT:
            raise BizException.unauthorized("双因子验证已锁定，请重新登录")
        # 用 400 而不是 401：错码不是会话过期，前端不必清掉 pending 票、也不必跳回登录页
        raise BizException.bad_request("验证码错误，请再试一次")
    lockout.clear_totp_failures(user.username, ip)
    if not enrolled:
        # 第一次扫码验证成功，pending 密钥转正，后面登录只问 6 位码
        user.totp_secret = user.totp_pending_secret
        user.totp_enrolled = True
        user.totp_pending_secret = ""
    return _issue_session(db, user)


def _can_reset_locally(user: User) -> bool:
    """只有本系统密码账号能走邮箱找回。LDAP / 纯企微去域控或企微改密。"""
    ids = identities_of(user)
    if "ldap" in ids and not (user.password_hash or ""):
        return False
    if not (user.password_hash or ""):
        return False
    return True


def request_password_reset(db: Session, username: str, origin: str = "") -> None:
    """接受找回请求。无论账号在不在都同一句成功，避免被用来枚举用户。"""
    from app.modules.notify.mail import send_mail
    from app.modules.settings import get_all_settings

    # 1. 用短账号或邮箱找人；找不到、禁用、LDAP 无本地密码、占位邮箱：静默返回
    name = (username or "").strip()
    user = None
    if name:
        user = db.scalar(select(User).where(func.lower(User.username) == name.lower()))
        if user is None and "@" in name:
            user = db.scalar(select(User).where(func.lower(User.email) == name.lower()))
    if (
        user is None
        or (user.status or "active") != "active"
        or not _can_reset_locally(user)
        or _is_placeholder_email(user.email or "")
    ):
        return
    # 2. 只存 sha256，邮件里带明文 token，用过即废
    raw = secrets.token_urlsafe(32)
    user.password_reset_hash = hashlib.sha256(raw.encode()).hexdigest()
    user.password_reset_expires_at = datetime.now() + timedelta(hours=_RESET_TTL_HOURS)
    db.commit()
    # 3. 链接根地址：平台配置 > 前端传来的 Origin > 企微卡片根
    cfg = get_all_settings(db)
    base = (cfg.get("public_app_base") or "").strip().rstrip("/")
    if not base:
        base = (origin or "").strip().rstrip("/")
    if not base:
        from app.modules.notify.mail import _public_origin

        base = _public_origin(cfg)
    if not base:
        return
    link = f"{base}/reset-password?token={raw}"
    send_mail(
        user.email,
        "重置登录密码",
        f"你正在重置「{user.username}」的登录密码。链接 { _RESET_TTL_HOURS } 小时内有效。"
        "如果不是你本人操作，请忽略这封邮件。重置成功后需要重新绑定 Authenticator。",
        link=link,
    )


def reset_password_with_token(db: Session, token: str, password: str) -> None:
    """用邮件令牌改密，并清掉双因子，避免手机丢了之后旧 Authenticator 还能挡住登录。"""
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    user = db.scalar(select(User).where(User.password_reset_hash == digest))
    if user is None or not user.password_reset_expires_at:
        raise BizException.bad_request("重置链接无效或已使用")
    if user.password_reset_expires_at < datetime.now():
        raise BizException.bad_request("重置链接已过期，请重新申请")
    if not _can_reset_locally(user):
        raise BizException.bad_request("该账号不能在本系统重置密码")
    user.password_hash = hash_password(password)
    user.password_reset_hash = ""
    user.password_reset_expires_at = None
    user.totp_enrolled = False
    user.totp_secret = ""
    user.totp_pending_secret = ""
    db.commit()


def set_admin_email(db: Session, current: CurrentUser, email: str) -> UserInfo:
    """本地管理员第一次登录必须留下真实邮箱，否则丢了密码或手机无法找回。"""
    user = db.get(User, current.id)
    if user is None:
        raise BizException.not_found("用户")
    if not user.is_admin:
        raise BizException.forbidden("只有管理员需要在首次登录配置找回邮箱")
    addr = (email or "").strip()
    if not _EMAIL_RE.match(addr) or _is_placeholder_email(addr):
        raise BizException.bad_request("请填写真实邮箱，例如 admin@company.com")
    taken = db.scalar(
        select(User).where(func.lower(User.email) == addr.lower(), User.id != user.id)
    )
    if taken is not None:
        raise BizException.bad_request("该邮箱已被其他账号使用")
    user.email = addr
    db.commit()
    return _user_info(user)


def get_profile(db: Session, current: CurrentUser) -> UserInfo:
    """当前登录用户资料。must_set_email 随邮箱是否占位一起算。"""
    user = db.get(User, current.id)
    if user is None:
        raise BizException.not_found("用户")
    return _user_info(user)


def _authenticate_ldap(db: Session, username: str, password: str, settings: dict) -> User:
    """LDAP 认证后绑定到唯一本地用户（已有企微账号则合并，不新建）。"""
    from app.modules.auth.identity import resolve_or_provision
    from app.modules.auth.ldap_service import ldap_authenticate, ldap_search

    try:
        result = ldap_authenticate(username, password, settings)
    except Exception as e:  # noqa: BLE001
        raise BizException.service_unavailable(
            f"LDAP 服务不可用：{e}。请检查平台设置里的服务器地址、绑定账号和密码。"
        ) from e

    if result is None:
        try:
            found = ldap_search(settings, username)
        except Exception:
            found = None
        if found is None:
            raise BizException.unauthorized(
                "在 AD 中找不到该用户。请用邮箱、UPN 或域账号登录，"
                "并确认「用户搜索基准 DN」覆盖该人所在 OU。"
            )
        raise BizException.unauthorized("LDAP 密码错误")

    email = (result.get("email") or "").strip()
    sam = (result.get("username") or "").strip()
    user = resolve_or_provision(
        db,
        wecom_userid="",
        usernames=[sam, username],
        emails=[email],
        display_name=result.get("display_name") or sam or username,
        source="ldap",
        auto_provision=settings.get("ldap_auto_provision", "true") == "true",
    )
    if user is None:
        raise BizException.forbidden("该 LDAP 用户尚未导入本系统，请让管理员在「用户管理」中导入")
    return user


def check_permission(db: Session, user: CurrentUser, resource_type: str, resource_id: int, action: str) -> bool:
    """权限校验（admin 直接放行）。"""
    if user.is_admin:
        return True
    perms = db.scalars(select(Permission).where(Permission.user_id == user.id)).all()
    allowed = False
    for p in perms:
        if p.action not in (action, "*"):
            continue
        matched = False
        if p.resource_type == "pipeline":
            matched = resource_type == "pipeline" and p.resource_id == resource_id
        elif p.resource_type == "group":
            matched = resource_type in ("group", "pipeline") and p.resource_id == resource_id
        elif p.resource_type == "project":
            matched = p.resource_id == resource_id
        if not matched:
            continue
        if p.effect == "deny":
            return False
        if p.effect == "allow":
            allowed = True
    return allowed


def grant_permission(db: Session, user_id: int, resource_type: str, resource_id: int, action: str, effect: str = "allow") -> Permission:
    """授予权限。"""
    p = Permission(
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        effect=effect,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    from app.core.deps import invalidate_permission_cache

    invalidate_permission_cache(db, user_id)
    return p
