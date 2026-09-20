"""认证与 RBAC 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import R
from app.db.session import get_db
from app.modules.auth import service
from app.modules.auth.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    ResetPasswordRequest,
    SetEmailRequest,
    TotpLoginRequest,
    UserInfo,
)

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_ip(request: Request) -> str:
    """取登录来源 IP。优先 X-Forwarded-For 第一段（经 Nginx 时才是浏览器地址）。"""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return ""


@router.post("/login", response_model=R[LoginResponse], summary="登录")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """校验账号密码。平台开了双因子时只发 pending_token，还要再走 /login/totp。"""
    return R.ok(service.authenticate(db, body.username, body.password, client_ip=_client_ip(request)))


@router.post("/login/totp", response_model=R[LoginResponse], summary="双因子验证码登录")
def login_totp(body: TotpLoginRequest, request: Request, db: Session = Depends(get_db)):
    """用 pending 票 + Authenticator 6 位码换正式会话。首次成功即完成绑定。"""
    return R.ok(
        service.complete_totp_login(
            db, body.pending_token, body.code, client_ip=_client_ip(request)
        )
    )


@router.post("/forgot-password", summary="申请重置密码（仅本地账号）")
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """无论账号在不在都同一句成功，避免被用来枚举用户。LDAP 无本地密码的不发信。"""
    service.request_password_reset(db, body.username, body.origin)
    return R.ok(message="如果该账号可以找回，重置邮件已发出。请检查邮箱。")


@router.post("/reset-password", summary="用邮件链接设置新密码")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """令牌一次性。改密同时清掉 TOTP，丢了手机的人下次重新扫码。"""
    service.reset_password_with_token(db, body.token, body.password)
    return R.ok(message="密码已重置，请使用新密码登录。若开启了双因子，需要重新扫码绑定。")


@router.put("/email", response_model=R[UserInfo], summary="管理员首次登录配置找回邮箱")
def set_email(
    body: SetEmailRequest,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """本地管理员必须留下真实邮箱，否则丢了密码或手机无法走邮件找回。"""
    return R.ok(service.set_admin_email(db, current, body.email))


@router.get("/me", response_model=R[UserInfo], summary="当前用户信息")
def me(current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return R.ok(service.get_profile(db, current))


@router.post("/logout", summary="登出")
def logout():
    # 无状态 JWT，登出由前端清除 token 即可
    return R.ok()
