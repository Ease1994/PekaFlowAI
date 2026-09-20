"""登录失败次数锁定：密码 5 次 / TOTP 8 次。"""
from __future__ import annotations

from app.core.response import BizException
from app.core.security import hash_password
from app.db.base import Base
from app.modules.auth import lockout
from app.modules.auth.models import User
from app.modules.auth.service import authenticate, complete_totp_login
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import update_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, PlatformSetting.__table__])
    return Session(engine)


def _admin(db: Session) -> User:
    user = User(
        username="admin",
        display_name="系统管理员",
        email="",
        password_hash=hash_password("admin123"),
        is_admin=True,
        source="local",
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_password_lockout_and_unlock(monkeypatch) -> None:
    lockout.reset_for_tests()
    monkeypatch.setattr(lockout, "_redis", lambda: None)
    db = _db()
    _admin(db)
    ip = "10.1.2.3"
    for _ in range(lockout.PASSWORD_LIMIT):
        try:
            authenticate(db, "admin", "wrong", client_ip=ip)
            raise AssertionError("wrong password should fail")
        except BizException as exc:
            assert exc.code == 401
            assert exc.message == "用户名或密码错误"
    try:
        authenticate(db, "admin", "admin123", client_ip=ip)
        raise AssertionError("locked should still look like bad password")
    except BizException as exc:
        assert exc.code == 401
        assert exc.message == "用户名或密码错误"
    lockout.clear_password_failures("admin", ip)
    ok = authenticate(db, "admin", "admin123", client_ip=ip)
    assert ok.status == "ok"


def test_unknown_user_same_message(monkeypatch) -> None:
    lockout.reset_for_tests()
    monkeypatch.setattr(lockout, "_redis", lambda: None)
    db = _db()
    try:
        authenticate(db, "no-such-user", "x", client_ip="1.1.1.1")
        raise AssertionError("should fail")
    except BizException as exc:
        assert exc.message == "用户名或密码错误"


def test_totp_lockout_voids_pending(monkeypatch) -> None:
    import pyotp

    lockout.reset_for_tests()
    monkeypatch.setattr(lockout, "_redis", lambda: None)
    db = _db()
    user = _admin(db)
    update_settings(db, {"totp_2fa_enabled": "true"})
    first = authenticate(db, "admin", "admin123", client_ip="8.8.8.8")
    assert first.status == "totp_setup"
    pending = first.pending_token
    for i in range(lockout.TOTP_LIMIT):
        try:
            complete_totp_login(db, pending, "000000", client_ip="8.8.8.8")
            raise AssertionError("wrong totp should fail")
        except BizException as exc:
            if i + 1 < lockout.TOTP_LIMIT:
                assert exc.code == 400
            else:
                assert exc.code == 401
                assert "重新登录" in exc.message
    try:
        db.refresh(user)
        secret = None
        from app.modules.auth.totp import unseal_secret

        secret = unseal_secret(user.totp_pending_secret)
        complete_totp_login(db, pending, pyotp.TOTP(secret).now(), client_ip="8.8.8.8")
        raise AssertionError("voided pending must not succeed")
    except BizException as exc:
        assert exc.code == 401
