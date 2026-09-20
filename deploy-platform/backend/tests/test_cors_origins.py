"""CORS 白名单来自 CORS_ORIGINS，不再默认 *。"""
from __future__ import annotations

from app.core.cors import cors_allow_origins, origin_allowed


def test_cors_origins_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://192.0.2.10:8000, https://ci.example/")

    class Fake:
        def __enter__(self):
            raise RuntimeError("no db")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("app.db.session.SessionLocal", lambda: Fake())
    origins = cors_allow_origins()
    assert origins == ["http://192.0.2.10:8000", "https://ci.example"]
    assert origin_allowed("http://192.0.2.10:8000")
    assert not origin_allowed("http://evil.example")
    assert "*" not in origins
