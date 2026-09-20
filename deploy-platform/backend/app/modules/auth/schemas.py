"""认证模块的 Pydantic Schema。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class TotpLoginRequest(BaseModel):
    """密码通过之后，用 Authenticator 的 6 位码换正式会话。"""

    pending_token: str = Field(..., description="密码校验通过后下发的短时票据")
    code: str = Field(..., description="Authenticator 当前 6 位数字")


class ForgotPasswordRequest(BaseModel):
    username: str = Field(..., description="本地用户名或邮箱")
    origin: str = Field("", description="前端当前站点根地址，用来拼重置链接")


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., description="邮件里的一次性重置令牌")
    password: str = Field(..., min_length=8, description="新密码，至少 8 位")


class SetEmailRequest(BaseModel):
    email: str = Field(..., description="管理员真实邮箱，用于找回密码")


class UserInfo(BaseModel):
    """登录成功或 /me 返回的当前用户。must_set_email 只对本地管理员为真。"""

    id: int
    username: str
    display_name: str = ""
    email: str = ""
    is_admin: bool = False
    source: str = "local"
    totp_enrolled: bool = False
    must_set_email: bool = False


class LoginResponse(BaseModel):
    """登录结果。status=ok 才有正式 token；双因子进行中只有 pending_token。"""

    token: str = ""
    user: UserInfo | None = None
    status: str = "ok"
    pending_token: str = ""
    otpauth_uri: str = ""
    qr_svg: str = ""
    message: str = ""
