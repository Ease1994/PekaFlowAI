"""双因子登录：关着时仍直接出 token；开着时要扫码/输码；pending 票不能当会话。"""
from __future__ import annotations

import pyotp
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, verify_token
from app.core.response import BizException
from app.core.security import decode_access_token, hash_password
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.auth.service import authenticate, complete_totp_login, get_profile
from app.modules.auth.totp import qr_svg, unseal_secret
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import update_settings


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__, PlatformSetting.__table__])
    return Session(engine)


def _admin(db: Session, *, email: str = "") -> User:
    user = User(
        username="admin",
        display_name="系统管理员",
        email=email,
        password_hash=hash_password("admin123"),
        is_admin=True,
        source="local",
        status="active",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_login_without_2fa_issues_session() -> None:
    db = _db()
    _admin(db)
    resp = authenticate(db, "admin", "admin123")
    assert resp.status == "ok"
    assert resp.token
    assert not resp.pending_token
    payload = decode_access_token(resp.token)
    assert payload.get("purpose") is None
    assert payload["username"] == "admin"


def test_login_with_2fa_requires_setup_then_code() -> None:
    db = _db()
    user = _admin(db)
    update_settings(db, {"totp_2fa_enabled": "true"})

    first = authenticate(db, "admin", "admin123")
    assert first.status == "totp_setup"
    assert first.pending_token
    assert not first.token
    assert first.otpauth_uri.startswith("otpauth://")
    assert "<svg" in first.qr_svg.lower()
    pending = decode_access_token(first.pending_token)
    assert pending["purpose"] == "mfa_pending"
    assert verify_token(first.pending_token) is None

    db.refresh(user)
    secret = unseal_secret(user.totp_pending_secret)
    assert secret

    try:
        complete_totp_login(db, first.pending_token, "000000")
        raise AssertionError("wrong code should fail")
    except BizException as exc:
        assert exc.code == 400

    ok = complete_totp_login(db, first.pending_token, pyotp.TOTP(secret).now())
    assert ok.status == "ok"
    assert ok.token
    assert ok.user and ok.user.totp_enrolled
    assert decode_access_token(ok.token).get("purpose") is None

    db.refresh(user)
    assert user.totp_enrolled is True
    assert not user.totp_pending_secret

    again = authenticate(db, "admin", "admin123")
    assert again.status == "totp_required"
    assert not again.qr_svg
    enrolled_secret = unseal_secret(user.totp_secret)
    done = complete_totp_login(db, again.pending_token, pyotp.TOTP(enrolled_secret).now())
    assert done.status == "ok"


def test_turn_off_2fa_skips_code() -> None:
    db = _db()
    _admin(db)
    update_settings(db, {"totp_2fa_enabled": "true"})
    pending = authenticate(db, "admin", "admin123")
    update_settings(db, {"totp_2fa_enabled": "false"})
    resp = complete_totp_login(db, pending.pending_token, "000000")
    assert resp.status == "ok"
    assert resp.token
    direct = authenticate(db, "admin", "admin123")
    assert direct.status == "ok"
    assert direct.token


def test_qr_svg_is_inline() -> None:
    markup = qr_svg("otpauth://totp/demo?secret=MFRGGZDFMZTWQ2LK")
    assert markup.startswith("<svg") or "<svg" in markup[:80]
    assert "<?xml" not in markup


def test_profile_must_set_email_for_local_admin() -> None:
    db = _db()
    user = _admin(db, email="")
    info = get_profile(db, CurrentUser(id=user.id, username=user.username, is_admin=True))
    assert info.must_set_email is True
    user.email = "admin@mail.test"
    db.commit()
    info = get_profile(db, CurrentUser(id=user.id, username=user.username, is_admin=True))
    assert info.must_set_email is False
