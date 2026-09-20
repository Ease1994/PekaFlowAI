"""本地账号邮箱找回密码：LDAP 不发信；重置成功清掉 TOTP；管理员必须配真实邮箱。"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.core.security import hash_password, verify_password
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.auth.service import (
    authenticate,
    request_password_reset,
    reset_password_with_token,
    set_admin_email,
)
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import update_settings


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, PlatformSetting.__table__])
    return Session(engine)


def _local(db: Session, *, email: str = "admin@mail.test", admin: bool = True) -> User:
    user = User(
        username="admin",
        display_name="系统管理员",
        email=email,
        password_hash=hash_password("admin123"),
        is_admin=admin,
        source="local",
        status="active",
        totp_enrolled=True,
        totp_secret="sealed.placeholder",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_forgot_password_sends_mail_for_local_account(monkeypatch) -> None:
    db = _db()
    _local(db)
    sent: dict = {}

    def fake_send(to_addr: str, subject: str, body: str, link: str = "") -> bool:
        sent["to"] = to_addr
        sent["link"] = link
        return True

    monkeypatch.setattr("app.modules.notify.mail.send_mail", fake_send)
    request_password_reset(db, "admin", origin="http://front.test")
    assert sent["to"] == "admin@mail.test"
    assert sent["link"].startswith("http://front.test/reset-password?token=")


def test_forgot_password_prefers_public_app_base(monkeypatch) -> None:
    db = _db()
    _local(db)
    update_settings(db, {"public_app_base": "https://deploy.example.com/"})
    sent: dict = {}
    monkeypatch.setattr(
        "app.modules.notify.mail.send_mail",
        lambda to_addr, subject, body, link="": sent.update(link=link) or True,
    )
    request_password_reset(db, "admin@mail.test", origin="http://localhost:5173")
    assert sent["link"].startswith("https://deploy.example.com/reset-password?token=")


def test_forgot_password_skips_ldap_without_local_password(monkeypatch) -> None:
    db = _db()
    db.add(
        User(
            username="ldapuser",
            display_name="域用户",
            email="ldap@company.com",
            password_hash="",
            is_admin=False,
            source="ldap",
            status="active",
        )
    )
    db.commit()
    called = {"n": 0}
    monkeypatch.setattr(
        "app.modules.notify.mail.send_mail",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    request_password_reset(db, "ldapuser", origin="http://front.test")
    request_password_reset(db, "nobody", origin="http://front.test")
    assert called["n"] == 0


def test_forgot_password_skips_placeholder_email(monkeypatch) -> None:
    db = _db()
    _local(db, email="admin@deploy.local")
    called = {"n": 0}
    monkeypatch.setattr(
        "app.modules.notify.mail.send_mail",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    request_password_reset(db, "admin", origin="http://front.test")
    assert called["n"] == 0


def test_reset_password_clears_totp(monkeypatch) -> None:
    db = _db()
    user = _local(db)
    sent: dict = {}
    monkeypatch.setattr(
        "app.modules.notify.mail.send_mail",
        lambda to_addr, subject, body, link="": sent.update(link=link) or True,
    )
    request_password_reset(db, "admin", origin="http://front.test")
    token = sent["link"].split("token=", 1)[-1]
    reset_password_with_token(db, token, "newpass-123")
    db.refresh(user)
    assert verify_password("newpass-123", user.password_hash)
    assert user.totp_enrolled is False
    assert user.totp_secret == ""
    assert user.password_reset_hash == ""
    try:
        reset_password_with_token(db, token, "another-pass")
        raise AssertionError("token should be one-time")
    except BizException as exc:
        assert exc.code == 400
    resp = authenticate(db, "admin", "newpass-123")
    assert resp.status == "ok"


def test_reset_expired_link() -> None:
    db = _db()
    user = _local(db)
    raw = "expired-token-value"
    user.password_reset_hash = hashlib.sha256(raw.encode()).hexdigest()
    user.password_reset_expires_at = datetime.now() - timedelta(minutes=1)
    db.commit()
    try:
        reset_password_with_token(db, raw, "newpass-123")
        raise AssertionError("expired should fail")
    except BizException as exc:
        assert exc.code == 400


def test_admin_must_set_real_email() -> None:
    db = _db()
    user = _local(db, email="")
    current = CurrentUser(id=user.id, username=user.username, is_admin=True)
    try:
        set_admin_email(db, current, "admin@example.com")
        raise AssertionError("placeholder domain should fail")
    except BizException as exc:
        assert exc.code == 400
    info = set_admin_email(db, current, "ops@mail.test")
    assert info.email == "ops@mail.test"
    assert info.must_set_email is False
    other = User(
        username="dev",
        display_name="开发",
        email="dev@mail.test",
        password_hash=hash_password("dev-pass-1"),
        is_admin=False,
        source="local",
    )
    db.add(other)
    db.commit()
    try:
        set_admin_email(
            db,
            CurrentUser(id=other.id, username=other.username, is_admin=False),
            "dev2@mail.test",
        )
        raise AssertionError("non-admin should fail")
    except BizException as exc:
        assert exc.code == 403
