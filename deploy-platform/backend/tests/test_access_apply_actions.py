# -*- coding: utf-8 -*-
"""权限申请默认查看+执行，点名其它动作时按拟授落库。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission
from app.db.base import Base
from app.modules.access.service import (
    APPLICATION_ACTIONS,
    apply_execute,
    parse_requested_actions,
    review,
)
from app.modules.ai.models import AiWatch
from app.modules.auth.models import Permission, PermissionApplication, Role, User, UserRole
from app.modules.pipeline.models import Pipeline
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
            Permission.__table__,
            PermissionApplication.__table__,
            Role.__table__,
            UserRole.__table__,
            AiWatch.__table__,
        ],
    )
    return Session(engine)


def _seed(db: Session) -> dict:
    admin = User(username="admin", display_name="管理员", password_hash="x", is_admin=True)
    user = User(username="dev", display_name="开发", password_hash="x", is_admin=False)
    db.add_all([admin, user])
    db.flush()
    proj = Project(name="订单", code="order")
    db.add(proj)
    db.flush()
    grp = Group(project_id=proj.id, name="生产", type="prod")
    db.add(grp)
    db.flush()
    pipe = Pipeline(project_id=proj.id, group_id=grp.id, name="order-prod", yaml="", status="active")
    db.add(pipe)
    db.commit()
    return {"admin": admin, "user": user, "proj": proj, "grp": grp, "pipe": pipe}


def test_parse_requested_actions_default_and_named() -> None:
    assert parse_requested_actions("申请 DMS 执行权限") is None
    assert parse_requested_actions("申请编辑权限") == ["read", "update"]
    assert parse_requested_actions("申请所有权限") == APPLICATION_ACTIONS


def test_default_apply_is_read_execute(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    row = apply_execute(db, current, ids["pipe"].id, "要跑")
    assert row["granted_action_list"] == ["read", "execute"]


def test_named_update_is_kept_and_granted(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    row = apply_execute(db, current, ids["pipe"].id, "要改", actions=["update"])
    assert row["granted_action_list"] == ["read", "update"]
    reviewed = review(db, admin, row["id"], True, "可以")
    assert reviewed["granted_action_list"] == ["read", "update"]
    me = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    assert check_permission(db, me, "pipeline", ids["pipe"].id, "update") is True
    assert check_permission(db, me, "pipeline", ids["pipe"].id, "execute") is False


def test_pipeline_approve_lands_on_group(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    row = apply_execute(db, current, ids["pipe"].id, "要审", actions=["read", "approve"])
    review(db, admin, row["id"], True, "可以")
    me = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    assert check_permission(db, me, "group", ids["grp"].id, "approve") is True
    assert not db.query(Permission).filter(
        Permission.user_id == current.id,
        Permission.resource_type == "pipeline",
        Permission.action == "approve",
    ).all()


def test_already_has_execute_can_still_apply_update(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    db.add(
        Permission(
            user_id=ids["user"].id,
            resource_type="pipeline",
            resource_id=ids["pipe"].id,
            action="execute",
            effect="allow",
        )
    )
    db.add(
        Permission(
            user_id=ids["user"].id,
            resource_type="pipeline",
            resource_id=ids["pipe"].id,
            action="read",
            effect="allow",
        )
    )
    db.commit()
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    row = apply_execute(db, current, ids["pipe"].id, "还要改", actions=["update"])
    assert "update" in row["granted_action_list"]


def test_apply_notice_uses_web_source_not_ai(monkeypatch) -> None:
    captured: dict = {}

    def fake_emit(*_a, **k):
        captured["content"] = k.get("content") or ""

    monkeypatch.setattr("app.modules.access.service.emit", fake_emit)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    apply_execute(db, current, ids["pipe"].id, "页面提交")
    assert "入口：手动申请" in captured["content"]
    assert "AI 助手" not in captured["content"]


def test_apply_notice_uses_ai_source_when_from_assistant(monkeypatch) -> None:
    captured: dict = {}

    def fake_emit(*_a, **k):
        captured["content"] = k.get("content") or ""

    monkeypatch.setattr("app.modules.access.service.emit", fake_emit)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.current_source", lambda: "ai")
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    apply_execute(db, current, ids["pipe"].id, "助手代交")
    assert "入口：AI 助手" in captured["content"]
