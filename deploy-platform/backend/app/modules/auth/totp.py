"""TOTP 双因子：生成密钥、校验一次性口令、画出 Authenticator 扫码图。

标准是 RFC 6238，和 Google Authenticator / 微软 Authenticator 通用。
密钥落库前用平台 AES 封存，JWT 里不带明文密钥。
"""
from __future__ import annotations

import io

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

from app.core.security import decrypt, encrypt


def new_secret() -> str:
    """生成 Base32 密钥，给 Authenticator 绑定用。"""
    return pyotp.random_base32()


def seal_secret(secret: str) -> str:
    """把 TOTP 密钥封成 cipher.iv，写入用户表。"""
    cipher, iv = encrypt(secret)
    return f"{cipher}.{iv}"


def unseal_secret(sealed: str) -> str:
    """解开用户表里的 TOTP 密钥。格式不对或解不开返回空串。"""
    raw = (sealed or "").strip()
    if not raw or "." not in raw:
        return ""
    cipher, _, iv = raw.partition(".")
    if not cipher or not iv:
        return ""
    try:
        return decrypt(cipher, iv)
    except Exception:  # noqa: BLE001
        return ""


def verify_code(secret: str, code: str, *, window: int = 1) -> bool:
    """校验 6 位口令。window=1 允许前后各一个 30 秒窗口，避免手机时钟差一点。"""
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(digits) != 6:
        return False
    totp = pyotp.TOTP(secret)
    return bool(totp.verify(digits, valid_window=window))


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """Authenticator 识别的 otpauth:// 链接。"""
    from app.modules.settings.branding import DEFAULT_DISPLAY_NAME

    label = (account or "user").strip() or "user"
    brand = (issuer or DEFAULT_DISPLAY_NAME).strip() or DEFAULT_DISPLAY_NAME
    return pyotp.TOTP(secret).provisioning_uri(name=label, issuer_name=brand)


def qr_svg(uri: str) -> str:
    """把 otpauth 链接画成 SVG，登录页直接内嵌，不必再引前端二维码库。"""
    img = qrcode.make(uri, image_factory=SvgPathImage, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf)
    raw = buf.getvalue().decode("utf-8")
    # 去掉 XML 声明，方便前端当 innerHTML 塞进 div
    if raw.startswith("<?xml"):
        raw = raw.split(">", 1)[-1]
    return raw.strip()
