"""平台配置导入导出。

空文件、种类不对、超限都不得改库。导入只覆盖文件里写明的可写项。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.audit.models import AuditLog
from app.modules.settings.defaults import DEFAULT_SETTINGS
from app.modules.settings.models import PlatformSetting
from app.modules.settings.service import (
    EXPORT_KIND,
    EXPORT_VERSION,
    FILE_BACKED_KEYS,
    MAX_IMPORT_BYTES,
    export_document,
    get_setting,
    import_settings,
    update_settings,
)


def _db() -> Session:
    """内存库，只建配置表和审计表。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlatformSetting.__table__, AuditLog.__table__])
    return Session(engine)


def _envelope(settings: dict) -> dict:
    """拼一份导出文件外壳。"""
    return {"kind": EXPORT_KIND, "version": EXPORT_VERSION, "settings": settings}


def test_export_document_lists_writable_keys_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_DISPLAY_NAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    db = _db()
    db.add(PlatformSetting(key="header_image_mime", value="image/png"))
    db.commit()
    update_settings(db, {"platform_display_name": "演示平台", "smtp_password": "secret-value"})
    doc = export_document(db)
    assert doc["kind"] == EXPORT_KIND
    assert doc["version"] == EXPORT_VERSION
    assert set(doc["settings"]) == set(DEFAULT_SETTINGS) - FILE_BACKED_KEYS
    assert "header_image_mime" not in doc["settings"]
    assert doc["settings"]["platform_display_name"] == "演示平台"
    assert doc["settings"]["smtp_password"] == "secret-value"


def test_import_roundtrip_and_partial_does_not_wipe_other_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("HEADER_IMAGE_MIME", raising=False)
    db = _db()
    db.add(PlatformSetting(key="header_image_mime", value="image/png"))
    db.commit()
    update_settings(db, {"smtp_from": "keep@example.com", "redis_host": "old-host"})

    doc = export_document(db)
    doc["settings"]["redis_host"] = "10.0.0.8"
    doc["settings"]["totp_2fa_enabled"] = True
    doc["settings"]["session_expire_days"] = 9
    applied, count = import_settings(db, doc, user_id=7, username="admin")
    assert applied["redis_host"] == "10.0.0.8"
    assert applied["smtp_from"] == "keep@example.com"
    assert applied["totp_2fa_enabled"] == "true"
    assert applied["session_expire_days"] == "9"
    assert applied["header_image_mime"] == "image/png"
    assert count == len(doc["settings"])

    # 文件里没写的项保持原值；顶栏 MIME 和未知键不能借导入改掉。
    partial, partial_count = import_settings(
        db,
        _envelope(
            {
                "smtp_from": "next@example.com",
                "header_image_mime": "image/gif",
                "not_a_setting": "x",
            }
        ),
    )
    assert partial_count == 1
    assert partial["smtp_from"] == "next@example.com"
    assert partial["redis_host"] == "10.0.0.8"
    assert get_setting(db, "header_image_mime") == "image/png"

    row = db.scalar(select(AuditLog).where(AuditLog.action == "settings.import"))
    assert row is not None
    assert row.username == "admin"
    assert "smtp_password" in (row.detail or "")
    assert "secret-value" not in (row.detail or "")


def test_import_still_clamps_and_rejects_mysql_log_storage() -> None:
    db = _db()
    applied, _ = import_settings(
        db,
        _envelope({"session_expire_days": 0, "audit_retention_days": "7", "log_storage": "mysql"}),
    )
    assert applied["session_expire_days"] == "1"
    assert applied["audit_retention_days"] == "30"
    assert applied["log_storage"] == "es"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"kind": "other", "version": 1, "settings": {"smtp_from": "a@b.c"}},
        {"kind": EXPORT_KIND, "version": "1", "settings": {"smtp_from": "a@b.c"}},
        {"kind": EXPORT_KIND, "version": 2, "settings": {"smtp_from": "a@b.c"}},
        {"kind": EXPORT_KIND, "version": 1, "settings": {}},
        {"kind": EXPORT_KIND, "version": 1, "settings": []},
        {"kind": EXPORT_KIND, "version": 1, "settings": {"header_image_mime": "image/png"}},
        {"kind": EXPORT_KIND, "version": 1, "settings": {"session_expire_days": 1.5}},
        {"kind": EXPORT_KIND, "version": 1, "settings": {"smtp_from": {"host": "a"}}},
        {
            "kind": EXPORT_KIND,
            "version": 1,
            "settings": {"smtp_from": "a" * (MAX_IMPORT_BYTES + 1)},
        },
    ],
)
def test_bad_import_does_not_change_settings(payload: object) -> None:
    db = _db()
    update_settings(db, {"smtp_from": "keep@example.com"})
    with pytest.raises(BizException) as exc:
        import_settings(db, payload)
    assert exc.value.code == 400
    assert get_setting(db, "smtp_from") == "keep@example.com"


def test_too_many_keys_rejected_without_write() -> None:
    db = _db()
    update_settings(db, {"smtp_from": "keep@example.com"})
    settings = {f"extra-{i}": "v" for i in range(len(DEFAULT_SETTINGS) + 40)}
    settings["smtp_from"] = "changed@example.com"
    with pytest.raises(BizException) as exc:
        import_settings(db, _envelope(settings))
    assert exc.value.code == 400
    assert get_setting(db, "smtp_from") == "keep@example.com"
