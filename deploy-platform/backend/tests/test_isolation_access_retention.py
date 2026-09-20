"""生产/测试强制隔离、回收站超期留历史、节点备份缺省、整项目权限申请。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.env import assert_same_env, machine_env
from app.core.response import BizException
from app.db.base import Base
from app.modules.access.service import (
    APPLY_GROUP_EXECUTE,
    APPLY_PROJECT_EXECUTE,
    apply_execute,
    apply_group_execute,
    apply_project_execute,
    catalog_for_apply,
    review,
)
from app.modules.agent.task_service import resolve_keep_backups
from app.modules.ai.models import AiWatch
from app.modules.auth.models import Permission, PermissionApplication, Role, User, UserRole
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pipeline.service import (
    PIPELINE_PURGED,
    _expire_pipeline_from_recycle,
    purge_expired_pipelines,
)
from app.modules.pipeline.sub_pipeline import assert_same_pipeline_env
from app.modules.project.models import Group, Project


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Project.__table__,
            Group.__table__,
            Pipeline.__table__,
            Release.__table__,
            Permission.__table__,
            PermissionApplication.__table__,
            Role.__table__,
            UserRole.__table__,
            AiWatch.__table__,
        ],
    )
    return Session(engine)


def _seed(db: Session) -> dict[str, int]:
    user = User(username="u1", display_name="U", password_hash="x", is_admin=True)
    db.add(user)
    db.flush()
    proj = Project(name="DMS 经销商协同运营平台", code="dms")
    db.add(proj)
    db.flush()
    test_g = Group(project_id=proj.id, name="测试", type="test")
    prod_g = Group(project_id=proj.id, name="生产", type="prod")
    db.add_all([test_g, prod_g])
    db.flush()
    test_p = Pipeline(project_id=proj.id, group_id=test_g.id, name="dms-test", yaml="", status="active")
    prod_p = Pipeline(project_id=proj.id, group_id=prod_g.id, name="dms-prod", yaml="", status="active")
    db.add_all([test_p, prod_p])
    db.commit()
    return {
        "user": user.id,
        "project": proj.id,
        "test_pipe": test_p.id,
        "prod_pipe": prod_p.id,
    }


def test_machine_env_empty_is_not_prod() -> None:
    assert machine_env("") == ""
    assert machine_env(None) == ""
    assert machine_env("prod") == "prod"


def test_assert_same_env_blocks_test_and_prod() -> None:
    try:
        assert_same_env("test", "prod", action="调用")
        raise AssertionError("should reject")
    except BizException as e:
        assert "环境隔离" in e.message


def test_sub_pipeline_cannot_cross_env() -> None:
    db = _db()
    ids = _seed(db)
    caller = db.get(Pipeline, ids["test_pipe"])
    target = db.get(Pipeline, ids["prod_pipe"])
    try:
        assert_same_pipeline_env(db, caller, target)
        raise AssertionError("should reject")
    except BizException as e:
        assert "环境隔离" in e.message


def test_recycle_expire_keeps_release_rows() -> None:
    db = _db()
    ids = _seed(db)
    pipe = db.get(Pipeline, ids["prod_pipe"])
    pipe.status = "deleted"
    pipe.deleted_at = datetime.now() - timedelta(days=31)
    rel = Release(pipeline_id=pipe.id, group_id=pipe.group_id, version="v1", status="success")
    db.add(rel)
    db.commit()
    n = purge_expired_pipelines(db, retention_days=30)
    assert n == 1
    db.refresh(pipe)
    assert pipe.status == PIPELINE_PURGED
    assert pipe.yaml == ""
    assert db.get(Release, rel.id) is not None


def test_expire_helper_does_not_delete_pipeline_row() -> None:
    db = _db()
    ids = _seed(db)
    pipe = db.get(Pipeline, ids["prod_pipe"])
    _expire_pipeline_from_recycle(db, pipe)
    db.commit()
    assert db.get(Pipeline, pipe.id) is not None


def test_keep_backups_missing_defaults_to_twenty() -> None:
    assert resolve_keep_backups(None) == 20
    assert resolve_keep_backups("") == 20
    assert resolve_keep_backups("abc") == 20
    assert resolve_keep_backups(0) == 1
    assert resolve_keep_backups(5) == 5
    assert resolve_keep_backups(999) == 50


def test_apply_project_execute_is_one_application(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    row = apply_project_execute(db, current, project_id=ids["project"], reason="整项目")
    assert row["apply_type"] == APPLY_PROJECT_EXECUTE
    assert row["pipeline_id"] == 0
    pending = db.query(PermissionApplication).filter(PermissionApplication.applicant_id == current.id).all()
    assert len(pending) == 1


def test_review_project_execute_grants_project_scope(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    applicant = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    admin_user = User(username="admin", display_name="A", password_hash="x", is_admin=True)
    db.add(admin_user)
    db.commit()
    admin = CurrentUser(id=admin_user.id, username="admin", is_admin=True)
    row = apply_project_execute(db, applicant, project_id=ids["project"], reason="整项目")
    reviewed = review(db, admin, row["id"], True, "ok")
    assert reviewed["status"] == "approved"
    perms = db.query(Permission).filter(
        Permission.user_id == applicant.id,
        Permission.resource_type == "project",
        Permission.resource_id == ids["project"],
    ).all()
    assert {p.action for p in perms} >= {"read", "execute"}


def test_apply_group_execute_is_one_application(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    row = apply_group_execute(db, current, project_id=ids["project"], env="test", reason="测试环境")
    assert row["apply_type"] == APPLY_GROUP_EXECUTE
    assert row["pipeline_id"] == 0
    assert row["scope"] == "group"
    pending = db.query(PermissionApplication).filter(PermissionApplication.applicant_id == current.id).all()
    assert len(pending) == 1


def test_review_group_execute_grants_group_scope(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    applicant = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    admin_user = User(username="admin", display_name="A", password_hash="x", is_admin=True)
    db.add(admin_user)
    db.commit()
    admin = CurrentUser(id=admin_user.id, username="admin", is_admin=True)
    row = apply_group_execute(db, applicant, project_id=ids["project"], env="test", reason="测试环境")
    reviewed = review(db, admin, row["id"], True, "ok")
    assert reviewed["status"] == "approved"
    perms = db.query(Permission).filter(
        Permission.user_id == applicant.id,
        Permission.resource_type == "group",
    ).all()
    assert {p.action for p in perms} >= {"read", "execute"}
    assert not db.query(Permission).filter(
        Permission.user_id == applicant.id,
        Permission.resource_type == "project",
    ).all()


def test_pending_project_blocks_pipeline_apply(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    apply_project_execute(db, current, project_id=ids["project"], reason="整项目")
    try:
        apply_execute(db, current, ids["test_pipe"], "单条")
        raise AssertionError("should reject")
    except BizException as e:
        assert "待审批" in e.message


def test_catalog_for_apply_has_no_pipelines(monkeypatch) -> None:
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"], username="u1", is_admin=False)
    data = catalog_for_apply(db, current)
    assert "pipelines" not in data
    assert data["projects"]
    assert data["projects"][0]["groups"]
