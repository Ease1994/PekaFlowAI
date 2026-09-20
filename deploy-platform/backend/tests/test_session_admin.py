"""会话权限以库为准：后台改管理员开关后，旧 JWT 立刻按新身份生效。"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import get_current_admin, get_current_user, verify_token
from app.core.response import BizException
from app.core.security import create_access_token
from app.db.base import Base
from app.modules.auth.models import User


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__])
    return Session(engine)


def _user(db: Session, *, is_admin: bool, status: str = "active") -> User:
    """造一个可登录账号。password_hash 本测不校验，只占非空列。"""
    row = User(
        username="ldap-user",
        display_name="LDAP 用户",
        email="user@mail.test",
        password_hash="x",
        is_admin=is_admin,
        source="ldap",
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/account/users"),
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_jwt_admin_claim_false_but_db_true() -> None:
    """LDAP 用户登录时还不是管理员，后台打开开关后当前会话必须立刻放行。"""
    db = _db()
    row = _user(db, is_admin=True)
    token = create_access_token(row.id, row.username, is_admin=False)
    current = get_current_user(_request(), _creds(token), db)
    assert current.id == row.id
    assert current.is_admin is True
    assert get_current_admin(current).is_admin is True
    via_sse = verify_token(token, db)
    assert via_sse is not None
    assert via_sse.is_admin is True


def test_jwt_admin_claim_true_but_db_false() -> None:
    """降权必须立刻收回，不能拿着旧管理员票继续进系统管理。"""
    db = _db()
    row = _user(db, is_admin=False)
    token = create_access_token(row.id, row.username, is_admin=True)
    current = get_current_user(_request(), _creds(token), db)
    assert current.is_admin is False
    try:
        get_current_admin(current)
        raise AssertionError("降权后旧 JWT 不应再当管理员")
    except BizException as exc:
        assert exc.code == 403
    via_sse = verify_token(token, db)
    assert via_sse is not None
    assert via_sse.is_admin is False


def test_disabled_user_rejected_even_with_valid_jwt() -> None:
    """禁用账号不能靠未过期的登录票继续调接口。"""
    db = _db()
    row = _user(db, is_admin=True, status="disabled")
    token = create_access_token(row.id, row.username, is_admin=True)
    try:
        get_current_user(_request(), _creds(token), db)
        raise AssertionError("禁用账号应 401")
    except BizException as exc:
        assert exc.code == 401
        assert "禁用" in exc.message
    assert verify_token(token, db) is None
