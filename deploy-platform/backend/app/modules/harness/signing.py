"""第三方 Agent Tool 包的 Ed25519 签名校验。

签名同时覆盖 manifest 的规范化 JSON 和包内容摘要：只签 manifest 的话，同一份
签名可以配任意代码；只签代码的话，能力声明可以被改。内容摘要按条目逐个哈希，
并排除 manifest.sig 自身——签名文件是签名的产物，不能再被签名覆盖。
公钥由管理员在平台设置里登记，未登记的签名者一律拒绝。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SETTING_KEY = "harness_tool_signing_keys"
SIGNATURE_NAME = "manifest.sig"


@dataclass(frozen=True)
class SignatureResult:
    verified: bool
    key_id: str = ""
    reason: str = ""


def content_digest(archive: zipfile.ZipFile) -> str:
    """包内容摘要：逐条目 (路径, 内容哈希) 排序后再哈希，排除签名文件本身。

    不直接用整包 sha256——zip 里存着签名文件，签名又要覆盖包内容，
    那样永远算不出一个自洽的值。
    """
    digest = hashlib.sha256()
    entries = sorted(
        name.replace("\\", "/")
        for name in archive.namelist()
        if not name.endswith("/") and name.replace("\\", "/") != SIGNATURE_NAME
    )
    for name in entries:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(archive.read(name)).hexdigest().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def signing_payload(manifest: dict[str, Any], content_sha256: str) -> bytes:
    """签名原文：manifest 排序序列化 + 内容摘要，任一改动都会使签名失效。"""
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{canonical}\n{content_sha256.lower()}".encode()


def trusted_keys(db) -> dict[str, str]:
    """已登记的签名公钥：key_id -> base64 编码的 Ed25519 公钥。"""
    from app.modules.settings import service as settings_service

    raw = settings_service.get_setting(db, SETTING_KEY, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if str(k) and str(v)}


def verify(
    db,
    manifest: dict[str, Any],
    content_sha256: str,
    signature: str,
    key_id: str,
) -> SignatureResult:
    if not signature or not key_id:
        return SignatureResult(False, key_id, "缺少签名或签名者标识")
    keys = trusted_keys(db)
    if not keys:
        return SignatureResult(False, key_id, "平台尚未登记任何工具签名公钥")
    encoded = keys.get(key_id)
    if not encoded:
        return SignatureResult(False, key_id, f"签名者 {key_id} 不在信任列表中")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded, validate=True))
        public_key.verify(
            base64.b64decode(signature, validate=True),
            signing_payload(manifest, content_sha256),
        )
    except (InvalidSignature, ValueError, binascii.Error) as exc:
        return SignatureResult(False, key_id, f"签名校验失败: {exc}")
    return SignatureResult(True, key_id)
