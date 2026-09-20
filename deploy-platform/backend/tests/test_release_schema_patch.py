# -*- coding: utf-8 -*-
"""存量库缺列时，先按模型补齐，再走 ORM 回填。"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db.schema_sync import sync_model_columns
from app.main import _ensure_release_build_number, _withdraw_orphan_approvals


def _legacy_engine(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy_release.db'}")
    monkeypatch.setattr("app.db.session.engine", engine)
    monkeypatch.setattr("app.db.session.SessionLocal", sessionmaker(bind=engine))
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE `release` (
                    id INTEGER PRIMARY KEY,
                    pipeline_id INTEGER NOT NULL,
                    group_id INTEGER,
                    build_number INTEGER DEFAULT 0,
                    status VARCHAR(16) DEFAULT 'success'
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO `release` (id, pipeline_id, group_id, build_number, status) "
                "VALUES (10, 1, 1, 0, 'success'), (11, 1, 1, 0, 'pending'), "
                "(12, 2, 1, 3, 'success')"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE approval (
                    id INTEGER PRIMARY KEY,
                    release_id INTEGER NOT NULL,
                    approver_id INTEGER NOT NULL,
                    status VARCHAR(16) DEFAULT 'pending',
                    comment TEXT DEFAULT ''
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO approval (id, release_id, approver_id, status) "
                "VALUES (1, 10, 1, 'pending'), (2, 11, 1, 'pending')"
            )
        )
    return engine


def test_sync_then_orm_backfill_on_legacy_release(tmp_path, monkeypatch):
    engine = _legacy_engine(tmp_path, monkeypatch)
    added = sync_model_columns(engine)
    assert "release.deploy_request_id" in added
    assert "release.business_summary" in added

    cols = {c["name"] for c in inspect(engine).get_columns("release")}
    assert "deploy_request_id" in cols
    assert "approval_mode" not in cols  # 没这张旧 pipeline 表就不要乱建

    _ensure_release_build_number()
    _withdraw_orphan_approvals()

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, pipeline_id, build_number FROM `release` ORDER BY id")
        ).fetchall()
        statuses = dict(
            conn.execute(text("SELECT release_id, status FROM approval")).fetchall()
        )
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id[10] == (1, 1)
    assert by_id[11] == (1, 2)
    assert by_id[12] == (2, 3)
    assert statuses[10] == "cancelled"
    assert statuses[11] == "pending"


def test_orm_without_sync_still_fails(tmp_path, monkeypatch):
    """没先补列就走 ORM，必须仍是 1054 这类错，避免以后又把顺序写反。"""
    _legacy_engine(tmp_path, monkeypatch)
    try:
        _ensure_release_build_number()
    except Exception as e:  # noqa: BLE001
        assert "deploy_request_id" in str(e) or "no such column" in str(e).lower()
        return
    raise AssertionError("缺列时 ORM 回填居然成功了")
