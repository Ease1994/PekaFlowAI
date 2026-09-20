"""平台品牌、登录 Session、审计保留：夹值、顶栏图落盘、签发过期时间。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.core.security import create_access_token, decode_access_token
from app.db.base import Base
from app.modules.audit.service import clamp_retention_days
from app.modules.settings import branding
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import update_settings

# 1x1 透明 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlatformSetting.__table__])
    return Session(engine)


def test_clamp_display_and_session() -> None:
    assert branding.clamp_display_name("  发布部署平台  ") == "PekaFlowAI"
    assert branding.clamp_display_name("") == "PekaFlowAI"
    assert branding.clamp_display_name("  PekaFlow  ") == "PekaFlowAI"
    assert branding.clamp_display_name("  PekaFlowAI  ") == "PekaFlowAI"
    assert len(branding.clamp_display_name("测" * 80)) == branding.DISPLAY_NAME_MAX
    assert branding.clamp_from_name("") == branding.DEFAULT_FROM_NAME
    assert branding.clamp_session_expire_days("7") == 7
    assert branding.clamp_session_expire_days("0") == 1
    assert branding.clamp_session_expire_days("99") == 30
    assert branding.clamp_session_expire_days("abc") == 1
    assert branding.clamp_public_app_base(" http://a.test/ ") == "http://a.test"
    assert branding.clamp_public_app_base("") == ""
    assert clamp_retention_days("365") == 365
    assert clamp_retention_days("1") == 30
    assert clamp_retention_days("x") == 180


def test_clamp_notice() -> None:
    assert branding.clamp_notice_text("  今晚发版  ") == "今晚发版"
    assert branding.clamp_notice_text("") == ""
    assert len(branding.clamp_notice_text("告" * 400)) == branding.NOTICE_TEXT_MAX
    assert branding.clamp_notice_color("orange") == "orange"
    assert branding.clamp_notice_color("BLUE") == "red"
    assert branding.clamp_notice_color("") == "red"


def test_update_settings_clamps_session_and_audit() -> None:
    db = _db()
    out = update_settings(
        db,
        {
            "session_expire_days": "0",
            "audit_retention_days": "7",
            "platform_display_name": "  QX CI  ",
            "smtp_from_name": "  发件人  ",
            "header_notice_text": "  系统维护  ",
            "header_notice_color": "gold",
            "header_image_mime": "image/png",
        },
    )
    assert out["session_expire_days"] == "1"
    assert out["audit_retention_days"] == "30"
    assert out["platform_display_name"] == "QX CI"
    assert out["smtp_from_name"] == "发件人"
    assert out["header_notice_text"] == "系统维护"
    assert out["header_notice_color"] == "gold"
    # MIME 只能由上传接口写，整表保存带上必须忽略
    assert out["header_image_mime"] == ""


def test_public_branding_includes_header() -> None:
    db = _db()
    update_settings(
        db,
        {
            "platform_display_name": "示例发布",
            "header_notice_text": "今晚 22:00 发版",
            "header_notice_color": "volcano",
        },
    )
    assert branding.public_branding(db) == {
        "display_name": "示例发布",
        "header_image_url": "",
        "header_notice_text": "今晚 22:00 发版",
        "header_notice_color": "volcano",
    }


def test_header_image_roundtrip(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "header-image"
    monkeypatch.setattr(branding, "HEADER_IMAGE_DIR", tmp_path)
    monkeypatch.setattr(branding, "HEADER_IMAGE_PATH", image_path)
    db = _db()
    out = branding.save_header_image(db, _PNG)
    assert out["header_has_image"] is True
    assert out["header_image_url"].startswith("/api/v1/settings/header-image?v=")
    assert image_path.is_file()
    path, mime = branding.header_image_file(db)
    assert path == image_path
    assert mime == "image/png"
    branding.delete_header_image(db)
    assert not image_path.exists()
    assert branding.public_branding(db)["header_image_url"] == ""


def test_header_image_rejects_html() -> None:
    try:
        branding.detect_image_mime(b"<html><script>alert(1)</script>")
        raise AssertionError("should reject")
    except BizException as exc:
        assert exc.code == 400


def test_session_expire_seconds_from_settings() -> None:
    db = _db()
    update_settings(db, {"session_expire_days": "2"})
    assert branding.session_expire_seconds(db) == 2 * 86400


def test_create_access_token_uses_expire_seconds() -> None:
    token = create_access_token(1, "admin", True, expire_seconds=3600)
    payload = decode_access_token(token)
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    delta = (exp - datetime.now(timezone.utc)).total_seconds()
    assert 3500 < delta < 3700
