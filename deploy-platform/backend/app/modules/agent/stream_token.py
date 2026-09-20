"""日志 SSE 用的短时票。

浏览器 EventSource 不能自定义 Header，不能把 30 天登录 JWT 放进 URL。
先用正式会话 POST 换一张 60 秒的 stream_token，只绑定这一次任务或这一次发布。
"""
from __future__ import annotations

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.core.security import create_access_token, decode_access_token

STREAM_PURPOSE = "log_stream"
STREAM_TTL_SECONDS = 60


def issue_stream_token(user: CurrentUser, *, task_id: int | None = None, release_id: int | None = None) -> str:
    """签发仅用于日志 SSE 的短时票。task_id / release_id 必须且只能填一个。"""
    extra: dict = {"purpose": STREAM_PURPOSE}
    if task_id is not None:
        extra["task_id"] = int(task_id)
    if release_id is not None:
        extra["release_id"] = int(release_id)
    return create_access_token(
        user.id,
        user.username,
        user.is_admin,
        expire_seconds=STREAM_TTL_SECONDS,
        extra=extra,
    )


def user_from_stream_token(
    token: str,
    *,
    task_id: int | None = None,
    release_id: int | None = None,
) -> CurrentUser:
    """校验 stream_token：过期、用途不对、绑错资源一律 401。"""
    if not token:
        raise BizException.unauthorized("缺少或无效的令牌")
    try:
        payload = decode_access_token(token)
    except Exception as exc:  # noqa: BLE001
        raise BizException.unauthorized("缺少或无效的令牌") from exc
    if payload.get("purpose") != STREAM_PURPOSE:
        raise BizException.unauthorized("缺少或无效的令牌")
    if task_id is not None and int(payload.get("task_id") or 0) != int(task_id):
        raise BizException.unauthorized("缺少或无效的令牌")
    if release_id is not None and int(payload.get("release_id") or 0) != int(release_id):
        raise BizException.unauthorized("缺少或无效的令牌")
    return CurrentUser(
        id=int(payload.get("sub") or 0),
        username=str(payload.get("username") or ""),
        is_admin=bool(payload.get("adm", False)),
    )
