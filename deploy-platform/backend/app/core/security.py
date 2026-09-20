"""安全工具：密码哈希、JWT、AES 凭证加密。"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
import bcrypt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from app.core.config import settings

# ---- 密码哈希（bcrypt，避开 passlib 与 bcrypt 4.x 的兼容问题）----


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# ---- JWT ----
def create_access_token(
    user_id: int,
    username: str,
    is_admin: bool = False,
    expire_seconds: int | None = None,
    extra: dict | None = None,
) -> str:
    """签发登录 JWT。

    expire_seconds 来自平台设置的 Session 过期时间；未传则回退启动项 jwt_expire_seconds。
    只影响这一张新 token，已经发出去的旧会话仍按签发时写入的 exp。
    extra 给短时专用票：mfa_pending（双因子待验证）、log_stream（日志 SSE）。
    这两种票进不了普通业务接口。
    """
    seconds = int(expire_seconds) if expire_seconds is not None else int(settings.jwt_expire_seconds)
    purpose = (extra or {}).get("purpose")
    if purpose == "mfa_pending":
        min_seconds, max_seconds = 60, 15 * 60
    elif purpose == "log_stream":
        min_seconds, max_seconds = 30, 120
    else:
        min_seconds, max_seconds = 3600, 720 * 3600
    seconds = max(min_seconds, min(seconds, max_seconds))
    expire = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    payload = {
        "sub": str(user_id),
        "username": username,
        "adm": is_admin,
        "exp": expire,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---- API Token ----
def hash_api_token(raw: str) -> str:
    """API token 的 sha256 哈希（只存哈希，不存明文）。"""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_api_token() -> tuple[str, str]:
    """生成 API token，返回 (明文, 哈希)。明文只在创建时返回一次。"""
    raw = "qx_" + secrets.token_urlsafe(32)
    return raw, hash_api_token(raw)


# ---- AES 凭证加密（AES-256-CBC）----
def _derive_key(secret: str | None = None) -> bytes:
    """从 aes_key 派生 32 字节密钥。不传就用当前配置的那把。"""
    return hashlib.sha256((secret if secret is not None else settings.aes_key).encode()).digest()


def encrypt(plain: str) -> tuple[str, str]:
    """AES-256-CBC 加密，返回 (密文base64, iv_base64)。"""
    key = _derive_key()
    iv = hashlib.md5(settings.aes_key.encode()).digest()  # 16 字节 IV（演示用）
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()


def decrypt(cipher_b64: str, iv_b64: str, *, key_override: str | None = None) -> str:
    """AES-256-CBC 解密。key_override 用于拿历史密钥解旧密文（迁移场景）。"""
    key = _derive_key(key_override)
    iv = base64.b64decode(iv_b64)
    ciphertext = base64.b64decode(cipher_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode()


def mask_secret(secret: str | None) -> str:
    """脱敏显示。"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "***" + secret[-4:]
