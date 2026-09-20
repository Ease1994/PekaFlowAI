"""日志 SSE 只接受 60 秒 stream_token，不能拿登录 JWT 顶。"""
from __future__ import annotations

from app.core.deps import CurrentUser, verify_token
from app.core.response import BizException
from app.core.security import create_access_token
from app.modules.agent.stream_token import STREAM_TTL_SECONDS, issue_stream_token, user_from_stream_token


def test_stream_token_binds_task_and_rejects_session_jwt() -> None:
    user = CurrentUser(id=7, username="admin", is_admin=True)
    token = issue_stream_token(user, task_id=42)
    got = user_from_stream_token(token, task_id=42)
    assert got.id == 7
    assert STREAM_TTL_SECONDS == 60
    assert verify_token(token) is None
    session = create_access_token(7, "admin", True, expire_seconds=3600)
    try:
        user_from_stream_token(session, task_id=42)
        raise AssertionError("session jwt must not work as stream token")
    except BizException as exc:
        assert exc.code == 401
    try:
        user_from_stream_token(token, task_id=99)
        raise AssertionError("wrong task id")
    except BizException as exc:
        assert exc.code == 401
    try:
        user_from_stream_token(token, release_id=1)
        raise AssertionError("task token is not a release token")
    except BizException as exc:
        assert exc.code == 401
