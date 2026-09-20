# -*- coding: utf-8 -*-
"""查发布状态：running 只收进行中，success/failed 各走各的，不传则都给。"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.ai.skills.observe import _get_release_status
from app.modules.pipeline.models import Pipeline, Release
from app.modules.project.models import Group, Project


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Project.__table__, Group.__table__, Pipeline.__table__, Release.__table__])
    return Session(engine)


def _seed(db: Session) -> int:
    p = Project(name="订单", code="order")
    db.add(p)
    db.flush()
    g = Group(project_id=p.id, name="测试", type="test")
    db.add(g)
    db.flush()
    pipe = Pipeline(project_id=p.id, group_id=g.id, name="order-ci", yaml="")
    db.add(pipe)
    db.flush()
    db.add_all(
        [
            Release(pipeline_id=pipe.id, group_id=g.id, status="running", version="1"),
            Release(pipeline_id=pipe.id, group_id=g.id, status="success", version="2"),
            Release(pipeline_id=pipe.id, group_id=g.id, status="failed", version="3"),
            Release(pipeline_id=pipe.id, group_id=g.id, status="queued", version="4"),
        ]
    )
    db.commit()
    return pipe.id


def test_status_filter_running_and_failed():
    db = _db()
    _seed(db)
    admin = SimpleNamespace(id=1, is_admin=True)
    running = _get_release_status(db, admin, {"status": "running"})
    statuses = {r["status"] for r in running["releases"]}
    assert statuses <= {"running", "queued", "pending", "assigned", "rolling_back"}
    assert "running" in statuses
    assert "queued" in statuses
    assert "success" not in statuses
    assert "failed" not in statuses

    failed = _get_release_status(db, admin, {"status": "failed"})
    assert {r["status"] for r in failed["releases"]} == {"failed"}

    all_rows = _get_release_status(db, admin, {"status": "all", "latest": False, "limit": 10})
    assert {r["status"] for r in all_rows["releases"]} >= {"running", "success", "failed", "queued"}


def test_named_pipeline_defaults_to_latest_release():
    db = _db()
    pid = _seed(db)
    admin = SimpleNamespace(id=1, is_admin=True)
    latest = _get_release_status(db, admin, {"keyword": "order-ci"})
    assert [r["status"] for r in latest["releases"]] == ["queued"]
    assert latest["releases"][0]["pipeline_id"] == pid
    hist = _get_release_status(db, admin, {"keyword": "order-ci", "latest": False, "limit": 10})
    assert {r["status"] for r in hist["releases"]} >= {"running", "success", "failed", "queued"}


def test_keyword_still_hits_when_other_pipelines_are_newer():
    db = _db()
    pid = _seed(db)
    admin = SimpleNamespace(id=1, is_admin=True)
    pipe = db.get(Pipeline, pid)
    other = Pipeline(project_id=pipe.project_id, group_id=pipe.group_id, name="noise-ci", yaml="")
    db.add(other)
    db.flush()
    for i in range(25):
        db.add(Release(pipeline_id=other.id, group_id=1, status="success", version=str(i)))
    db.commit()
    hit = _get_release_status(db, admin, {"keyword": "order-ci"})
    assert len(hit["releases"]) == 1
    assert hit["releases"][0]["pipeline_id"] == pid
    assert hit["releases"][0]["status"] == "queued"
