# -*- coding: utf-8 -*-
"""草稿删除、第三方插件从仓库删除。"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.store import draft_service, plugin_service
from app.modules.store.models import Plugin, PluginDraft


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Plugin.__table__, PluginDraft.__table__])
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _no_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_service, "_sync_harness", lambda *a, **k: None)


def _plugin(db: Session, *, name: str, package_path: str = "", installed: bool = False) -> Plugin:
    p = Plugin(
        name=name,
        display_name=name,
        package_path=package_path,
        installed=installed,
        enabled=installed,
        status="installed" if installed else "uploaded",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_assert_draft_access_rejects_other_user(db: Session) -> None:
    from types import SimpleNamespace

    draft = PluginDraft(name="demo", display_name="演示", status="pending", created_by=7)
    db.add(draft)
    db.commit()
    db.refresh(draft)

    draft_service.assert_draft_access(draft, SimpleNamespace(id=7, is_admin=False))
    draft_service.assert_draft_access(draft, SimpleNamespace(id=1, is_admin=True))
    with pytest.raises(BizException) as exc:
        draft_service.assert_draft_access(draft, SimpleNamespace(id=8, is_admin=False))
    assert exc.value.code == 403


def test_delete_draft_removes_row(db: Session) -> None:
    draft = PluginDraft(name="query-running", display_name="查运行中", status="pending")
    db.add(draft)
    db.commit()
    db.refresh(draft)

    draft_service.delete_draft(db, draft.id)

    assert db.get(PluginDraft, draft.id) is None


def test_delete_missing_draft_404(db: Session) -> None:
    with pytest.raises(BizException) as exc:
        draft_service.delete_draft(db, 999)
    assert exc.value.code == 404


def test_delete_third_party_plugin_removes_row_and_zip(db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_service, "PACKAGE_DIR", tmp_path)
    zip_path = tmp_path / "third-party-1.0.0.zip"
    zip_path.write_bytes(b"zip")
    p = _plugin(db, name="third-party-demo", package_path=str(zip_path), installed=True)

    plugin_service.delete_plugin(db, p.id)

    assert db.scalar(select(Plugin).where(Plugin.name == "third-party-demo")) is None
    assert not zip_path.exists()


def test_cannot_delete_builtin_plugin(db: Session) -> None:
    p = _plugin(db, name="git-checkout", installed=True)

    with pytest.raises(BizException) as exc:
        plugin_service.delete_plugin(db, p.id)
    assert exc.value.code == 400
    assert db.get(Plugin, p.id) is not None


def test_install_marks_plugin_runnable_immediately(db: Session, tmp_path, monkeypatch) -> None:
    """点安装只改库里的 installed，编排器马上能选，不必重启后端。"""
    zip_path = tmp_path / "echo-1.0.0.zip"
    zip_path.write_bytes(b"pk\x03\x04fake-zip")
    p = _plugin(db, name="echo-tool", package_path=str(zip_path), installed=False)
    monkeypatch.setattr(plugin_service, "require_third_party_signature", lambda *a, **k: None)

    out = plugin_service.install_plugin(db, p.id)

    assert out.installed is True
    assert out.enabled is True
    runnable = [
        row.name
        for row in db.scalars(select(Plugin).where(Plugin.installed.is_(True), Plugin.enabled.is_(True))).all()
        if plugin_service.is_runnable_job_plugin(row)
    ]
    assert "echo-tool" in runnable


def test_cannot_uninstall_builtin_plugin(db: Session) -> None:
    p = _plugin(db, name="maven-build", installed=True)

    with pytest.raises(BizException) as exc:
        plugin_service.uninstall_plugin(db, p.id)
    assert exc.value.code == 400
    assert db.get(Plugin, p.id).installed is True


def test_serialize_marks_catalog_and_disk_plugins_builtin(db: Session) -> None:
    disk = _plugin(db, name="git-checkout")
    catalog = _plugin(db, name="maven-build")
    node = _plugin(db, name="file-transfer")
    third = _plugin(db, name="acme-notify")

    assert plugin_service.serialize_plugin(disk)["builtin"] is True
    assert plugin_service.serialize_plugin(catalog)["builtin"] is True
    assert plugin_service.serialize_plugin(node)["builtin"] is True
    assert plugin_service.serialize_plugin(third)["builtin"] is False
