"""确认卡片的一次性签名。

模型提出的操作要经过前端再回到 /ai/act，中间这一段是可以被改写的：
把卡片里的 pipeline_id 换成另一条再点确认，用户看到的和实际执行的就不是同一件事。
签名把「谁、哪个会话、什么操作、什么参数」绑死，确认阶段只认原样返回的那张卡。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from app.core.config import settings
from app.core.response import BizException

TOKEN_TTL_SECONDS = 30 * 60

# 已经用过的 nonce。单进程内存即可满足小团队部署；进程重启后最坏退化成
# 「同一张卡在 TTL 内可重复确认」，而参数改写仍然被签名挡住。
_used: dict[str, float] = {}


def _canonical(user_id: int, conversation_id: int, action_type: str, payload: dict) -> str:
    return json.dumps(
        {"u": int(user_id), "c": int(conversation_id), "t": action_type, "p": payload or {}},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _sign(expire_at: int, nonce: str, body: str) -> str:
    msg = f"{expire_at}.{nonce}.{body}".encode()
    return hmac.new(settings.jwt_secret.encode(), msg, hashlib.sha256).hexdigest()


def issue(user_id: int, conversation_id: int, action_type: str, payload: dict) -> str:
    expire_at = int(time.time()) + TOKEN_TTL_SECONDS
    nonce = secrets.token_hex(8)
    body = _canonical(user_id, conversation_id, action_type, payload)
    return f"v1.{expire_at}.{nonce}.{_sign(expire_at, nonce, body)}"


def sign_actions(user_id: int, conversation_id: int, actions: list[dict] | None) -> list[dict]:
    """给一批确认卡片盖章，返回带 token 的新列表。"""
    out = []
    for a in actions or []:
        item = dict(a)
        item["token"] = issue(user_id, conversation_id, str(a.get("type") or ""), a.get("payload") or {})
        out.append(item)
    return out


def _purge(now: float) -> None:
    for key, expire_at in list(_used.items()):
        if expire_at < now:
            _used.pop(key, None)


def verify(token: str, user_id: int, conversation_id: int, action_type: str, payload: dict) -> None:
    """校验并作废一张卡片；不通过一律抛错（fail closed）。"""
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "v1":
        raise BizException.bad_request("确认信息无效，请重新向助手发起该操作")
    _, raw_expire, nonce, signature = parts
    try:
        expire_at = int(raw_expire)
    except ValueError:
        raise BizException.bad_request("确认信息无效，请重新向助手发起该操作") from None

    now = time.time()
    if expire_at < now:
        raise BizException.bad_request("确认卡片已过期，请重新向助手发起该操作")

    body = _canonical(user_id, conversation_id, action_type, payload)
    if not hmac.compare_digest(signature, _sign(expire_at, nonce, body)):
        raise BizException.bad_request("确认内容与助手提出的操作不一致，已拒绝执行")

    _purge(now)
    if nonce in _used:
        raise BizException.bad_request("该确认卡片已执行过，请重新向助手发起该操作")
    _used[nonce] = expire_at
