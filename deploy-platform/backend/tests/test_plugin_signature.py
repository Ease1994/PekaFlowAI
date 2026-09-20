"""第三方流水线插件必须验签；内置插件跳过。"""
from __future__ import annotations

import base64
import io
import json
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.harness import signing
from app.modules.settings.models import PlatformSetting
from app.modules.store.models import Plugin
from app.modules.store.plugin_service import require_third_party_signature


TASK = {
    "name": "third-echo",
    "version": "1.0.0",
    "entrypoint": "python task.py",
}


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlatformSetting.__table__, Plugin.__table__])
    return Session(engine)


def _register_key(db: Session, key_id: str, private_key: Ed25519PrivateKey) -> None:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    db.add(PlatformSetting(key=signing.SETTING_KEY, value=json.dumps({key_id: base64.b64encode(raw).decode()})))
    db.commit()


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return buf.getvalue()


def _signed_zip(private_key: Ed25519PrivateKey, key_id: str, meta: dict) -> bytes:
    files = {"task.json": json.dumps(meta, ensure_ascii=False), "task.py": "print(1)\n"}
    unsigned = _zip(files)
    with zipfile.ZipFile(io.BytesIO(unsigned)) as archive:
        digest = signing.content_digest(archive)
    sig = base64.b64encode(private_key.sign(signing.signing_payload(meta, digest))).decode()
    files["manifest.sig"] = json.dumps({"key_id": key_id, "signature": sig})
    return _zip(files)


def test_unsigned_third_party_rejected() -> None:
    db = _db()
    data = _zip({"task.json": json.dumps(TASK), "task.py": "print(1)\n"})
    try:
        require_third_party_signature(db, data, "third-echo")
        raise AssertionError("unsigned zip must be rejected")
    except BizException as exc:
        assert exc.code == 400
        assert "manifest.sig" in exc.message


def test_signed_third_party_accepted() -> None:
    private_key = Ed25519PrivateKey.generate()
    db = _db()
    _register_key(db, "release-team", private_key)
    data = _signed_zip(private_key, "release-team", TASK)
    require_third_party_signature(db, data, "third-echo")


def test_builtin_skips_signature() -> None:
    db = _db()
    data = _zip({"task.json": json.dumps({"name": "git-checkout"}), "task.py": "x"})
    require_third_party_signature(db, data, "git-checkout")
