"""调用级能力 broker。

隔离 Runner 里的第三方代码没有数据库连接，也拿不到平台凭证；它想读任何平台数据
都必须带着本次调用签发的 capability token 回到 broker。token 绑定 call_id、
调用者身份和一份 allowlist，过期即失效——所以一次调用拿到的权限没法留到下一次用。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.response import BizException

# 每个能力对应一个只读投影函数；工具不能声明这以外的能力
CAPABILITIES: dict[str, str] = {
    "pipeline:read": "读取调用者可见的流水线基本信息",
    "release:read": "读取调用者可见的发布单状态",
    "agent:read": "读取构建机在线状态",
    "artifact:read": "读取制品元数据（不含下载地址）",
}
DEFAULT_TTL_SEC = 300


@dataclass(frozen=True)
class CapabilityGrant:
    call_id: str
    user_id: int
    capabilities: tuple[str, ...]
    expires_at: int

    @property
    def token(self) -> str:
        return _sign(self)


def validate_capabilities(requested: list[str]) -> list[str]:
    unknown = sorted(set(requested) - CAPABILITIES.keys())
    if unknown:
        raise BizException.bad_request("声明了未知能力: " + "、".join(unknown))
    return sorted(set(requested))


def grant(call_id: str, user_id: int, capabilities: list[str], *, ttl_sec: int = DEFAULT_TTL_SEC) -> CapabilityGrant:
    return CapabilityGrant(
        call_id=call_id,
        user_id=user_id,
        capabilities=tuple(validate_capabilities(capabilities)),
        expires_at=int(time.time()) + max(1, ttl_sec),
    )


def _payload(item: CapabilityGrant) -> str:
    return json.dumps(
        {
            "call_id": item.call_id,
            "user_id": item.user_id,
            "capabilities": list(item.capabilities),
            "expires_at": item.expires_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sign(item: CapabilityGrant) -> str:
    body = _payload(item)
    digest = hmac.new(settings.jwt_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{digest}.{body}"


def verify(token: str) -> CapabilityGrant:
    digest, separator, body = (token or "").partition(".")
    if not separator:
        raise BizException.forbidden("能力令牌格式非法")
    expected = hmac.new(settings.jwt_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise BizException.forbidden("能力令牌签名不匹配")
    try:
        data = json.loads(body)
        item = CapabilityGrant(
            call_id=str(data["call_id"]),
            user_id=int(data["user_id"]),
            capabilities=tuple(data["capabilities"]),
            expires_at=int(data["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BizException.forbidden("能力令牌内容非法") from exc
    if item.expires_at < int(time.time()):
        raise BizException.forbidden("能力令牌已过期")
    return item


def invoke(db: Session, token: str, capability: str, params: dict[str, Any]) -> dict[str, Any]:
    """Runner 内的工具回调平台读数据的唯一入口。"""
    item = verify(token)
    if capability not in item.capabilities:
        raise BizException.forbidden(f"本次调用未授予能力 {capability}")
    handler = _HANDLERS.get(capability)
    if handler is None:
        raise BizException.bad_request(f"未实现的能力 {capability}")
    from app.modules.auth.models import User

    caller = db.get(User, item.user_id)
    if caller is None or caller.status != "active":
        raise BizException.forbidden("调用者已失效")
    return handler(db, caller, params or {})


def _pipelines(db: Session, caller, params: dict[str, Any]) -> dict[str, Any]:
    from app.modules.ai.context import visible_pipelines

    limit = min(int(params.get("limit") or 50), 200)
    rows = visible_pipelines(db, caller)[:limit]
    return {
        "pipelines": [
            {
                "id": p.id,
                "name": p.name,
                "project": project.name if project else "",
                "group": group.name if group else "",
            }
            for p, project, group in rows
        ]
    }


def _releases(db: Session, caller, params: dict[str, Any]) -> dict[str, Any]:
    from app.modules.ai.registry import dispatch

    return dispatch(db, "get_release_status", dict(params), caller)


def _agents(db: Session, caller, params: dict[str, Any]) -> dict[str, Any]:
    from app.modules.ai.registry import dispatch

    return dispatch(db, "list_agents", dict(params), caller)


def _artifacts(db: Session, caller, params: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.modules.artifact.models import Artifact

    release_id = int(params.get("release_id") or 0)
    if not release_id:
        raise BizException.bad_request("缺少 release_id")
    rows = db.scalars(select(Artifact).where(Artifact.release_id == release_id)).all()
    return {
        "artifacts": [
            {"id": a.id, "name": a.name, "size_bytes": a.size_bytes, "sha256": a.sha256}
            for a in rows
        ]
    }


_HANDLERS = {
    "pipeline:read": _pipelines,
    "release:read": _releases,
    "agent:read": _agents,
    "artifact:read": _artifacts,
}
