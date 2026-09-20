"""企微应用消息：待审推送。登录仍走现有 OAuth，这里只负责把卡片推到手机。

默认关（wecom_notify_enabled=false）。没配 userid 的人收不到，不影响站内审批。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import struct
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.modules.auth.models import User
from app.modules.auth.wecom_service import QYAPI, _get_access_token

logger = logging.getLogger(__name__)


def notify_ready(cfg: dict) -> bool:
    return bool(
        cfg.get("wecom_notify_enabled") == "true"
        and cfg.get("wecom_corp_id")
        and cfg.get("wecom_secret")
        and str(cfg.get("wecom_agent_id", "")).strip()
    )


def _app_base(cfg: dict) -> str:
    raw = (cfg.get("wecom_app_base") or "").strip()
    if raw:
        return raw.rstrip("/")
    redirect = (cfg.get("wecom_redirect_uri") or "").strip()
    if not redirect:
        return ""
    parsed = urlparse(redirect)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def send_textcard(cfg: dict, userid: str, title: str, description: str, url: str) -> bool:
    if not notify_ready(cfg) or not userid:
        return False
    try:
        token = _get_access_token(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("企微 gettoken 失败，跳过推送：%s", e)
        return False
    payload = {
        "touser": userid,
        "msgtype": "textcard",
        "agentid": int(str(cfg.get("wecom_agent_id") or "0") or 0),
        "textcard": {
            "title": (title or "")[:128],
            "description": (description or "")[:512],
            "url": url or _app_base(cfg) or "https://work.weixin.qq.com",
            "btntxt": "去处理",
        },
        "enable_duplicate_check": 1,
        "duplicate_check_interval": 600,
    }
    try:
        r = httpx.post(f"{QYAPI}/message/send?access_token={token}", json=payload, timeout=15)
        data = r.json() if r.content else {}
        if data.get("errcode"):
            logger.warning("企微推送失败 userid=%s：%s", userid, data)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("企微推送异常 userid=%s：%s", userid, e)
        return False


def deliver_wecom(db: Session, user_id: int, title: str, content: str, link: str) -> bool:
    from app.modules.settings import get_all_settings

    cfg = get_all_settings(db)
    if not notify_ready(cfg):
        return False
    user = db.get(User, int(user_id))
    if user is None or not user.wecom_userid:
        return False
    base = _app_base(cfg)
    url = link if (link or "").startswith("http") else f"{base}{link or '/pm'}"
    return send_textcard(cfg, user.wecom_userid, title, content, url)


def verify_callback_echo(
    token: str,
    encoding_aes_key: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    """企微配置回调 URL 时的 GET 校验。配齐 token + EncodingAESKey 才能过。"""
    items = sorted([token or "", timestamp or "", nonce or "", echostr or ""])
    expect = hashlib.sha1("".join(items).encode("utf-8")).hexdigest()
    if expect != (msg_signature or ""):
        raise ValueError("签名不符")
    key = _aes_key(encoding_aes_key)
    raw = _aes_decrypt(echostr, key)
    # 16 随机 + 4 网络序长度 + msg + corp_id
    msg_len = struct.unpack(">I", raw[16:20])[0]
    return raw[20 : 20 + msg_len].decode("utf-8")


def _aes_key(encoding_aes_key: str) -> bytes:
    pad = encoding_aes_key + "=" * ((4 - len(encoding_aes_key) % 4) % 4)
    key = base64.b64decode(pad)
    if len(key) != 32:
        raise ValueError("EncodingAESKey 无效")
    return key


def _aes_decrypt(cipher_b64: str, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    data = base64.b64decode(cipher_b64)
    iv = key[:16]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    return plain[:-pad]
