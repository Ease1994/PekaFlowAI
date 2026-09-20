"""顶栏依赖探测：挂了也返回结构，不把异常抛成 500。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.health.service import probe_deps, reset_health_cache
from app.modules.settings.models import PlatformSetting


def test_probe_deps_returns_components(monkeypatch) -> None:
    reset_health_cache()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlatformSetting.__table__])
    db = Session(engine)
    monkeypatch.setattr("app.modules.health.service._probe_redis", lambda: {
        "name": "Redis", "status": "down", "detail": "Connection refused",
    })
    monkeypatch.setattr("app.modules.health.service._probe_es", lambda: {
        "name": "Elasticsearch", "status": "down", "detail": "timeout",
    })
    payload = probe_deps(db, force=True)
    assert payload["status"] == "degraded"
    names = {c["name"] for c in payload["components"]}
    assert names == {"MySQL", "Redis", "Elasticsearch"}
    assert any(a["name"] == "Redis" for a in payload["alerts"])


class _DummySession:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_probe_es_uses_account_and_info_not_head_ping(monkeypatch) -> None:
    """HTTPS + 账号的集群：写入走 _bulk，探测必须带账号并 GET /，不能再用 ping()。"""
    import sys
    from types import ModuleType

    from app.modules.health.service import _probe_es

    captured: dict = {}

    class FakeES:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def info(self):
            return {"cluster_name": "qx-es"}

        def ping(self):
            raise AssertionError("health must not use ping()")

        def close(self):
            captured["closed"] = True

    fake_mod = ModuleType("elasticsearch")
    fake_mod.Elasticsearch = FakeES
    monkeypatch.setitem(sys.modules, "elasticsearch", fake_mod)
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(
        "app.modules.settings.get_all_settings",
        lambda _db: {
            "es_hosts": "https://172.17.12.18:9200",
            "es_username": "elastic",
            "es_password": "secret",
        },
    )
    out = _probe_es()
    assert out["status"] == "ok"
    assert "https://172.17.12.18:9200" in out["detail"]
    assert "qx-es" in out["detail"]
    assert captured["kwargs"]["basic_auth"] == ("elastic", "secret")
    assert captured["kwargs"]["verify_certs"] is False
    assert captured.get("closed") is True
    assert "ping" not in out["detail"]
