from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.response import BizException
from app.db.base import Base
from app.modules.auth.models import Permission, Role, UserRole
from app.modules.pipeline.models import Pipeline
from app.modules.pm.models import ProjectMember
from app.modules.project.models import Group, Project
from app.modules.project.service import delete_project


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Project.__table__,
            Group.__table__,
            Pipeline.__table__,
            Role.__table__,
            UserRole.__table__,
            Permission.__table__,
            ProjectMember.__table__,
        ],
    )
    return Session(engine)


def _empty_project(db: Session) -> Project:
    project = Project(name="测试项目", code="TEST")
    db.add(project)
    db.flush()
    db.add_all(
        [
            Group(project_id=project.id, name="生产", type="prod"),
            Group(project_id=project.id, name="测试", type="test"),
        ]
    )
    db.commit()
    db.refresh(project)
    return project


def test_delete_empty_project_removes_default_groups() -> None:
    db = _db()
    project = _empty_project(db)
    delete_project(db, project.id)
    assert db.get(Project, project.id) is None
    assert db.scalars(select(Group.id).where(Group.project_id == project.id)).all() == []


def test_delete_project_blocked_when_pipeline_exists() -> None:
    db = _db()
    project = _empty_project(db)
    group = db.scalars(select(Group).where(Group.project_id == project.id)).first()
    assert group is not None
    db.add(Pipeline(project_id=project.id, group_id=group.id, name="demo-ci", yaml=""))
    db.commit()
    try:
        delete_project(db, project.id)
        raise AssertionError("should not delete")
    except BizException as exc:
        assert "流水线" in str(exc)
        assert "demo-ci" in str(exc)
    assert db.get(Project, project.id) is not None


def test_delete_project_blocked_by_recycle_bin() -> None:
    db = _db()
    project = _empty_project(db)
    group = db.scalars(select(Group).where(Group.project_id == project.id)).first()
    assert group is not None
    db.add(
        Pipeline(
            project_id=project.id,
            group_id=group.id,
            name="old-ci",
            yaml="",
            status="deleted",
            deleted_at=datetime.now(),
        )
    )
    db.commit()
    try:
        delete_project(db, project.id)
        raise AssertionError("should not delete")
    except BizException as exc:
        assert "回收站" in str(exc)
    assert db.get(Project, project.id) is not None
