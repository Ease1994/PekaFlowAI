# -*- coding: utf-8 -*-
"""角色申请：提交、重复待审、已是成员拦截、通过后写 UserRole。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, check_permission
from app.core.response import BizException
from app.db.base import Base
from app.modules.access.service import (
    APPLY_ROLE,
    apply_role,
    catalog,
    list_applications,
    my_entitlements,
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
    role = Role(
        project_id=proj.id,
        name="开发者",
        description="能看能跑",
        permissions=json.dumps({"pipeline": ["read", "execute"]}, ensure_ascii=False),
    )
    db.add_all([pipe, role])
    db.commit()
    return {"admin": admin, "user": user, "proj": proj, "grp": grp, "pipe": pipe, "role": role}


def test_catalog_includes_roles_without_members(monkeypatch) -> None:
    db = _db()
    ids = _seed(db)
    data = catalog(db)
    roles = data.get("roles") or []
    assert any(r["id"] == ids["role"].id and r["name"] == "开发者" for r in roles)
    assert all("users" not in r for r in roles)


def test_apply_role_and_approve_writes_user_role(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    row = apply_role(db, current, role_id=ids["role"].id, reason="要加入")
    assert row["apply_type"] == APPLY_ROLE
    assert row["role_id"] == ids["role"].id
    assert row["scope"] == "role"
    assert "开发者" in row["scope_text"]
    reviewed = review(db, admin, row["id"], True, "可以")
    assert reviewed["status"] == "approved"
    member = db.query(UserRole).filter(
        UserRole.user_id == current.id, UserRole.role_id == ids["role"].id
    ).one()
    assert member is not None
    me = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    assert check_permission(db, me, "pipeline", ids["pipe"].id, "execute") is True
    assert not db.query(Permission).filter(Permission.user_id == current.id).all()


def test_duplicate_pending_role_and_already_member(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    first = apply_role(db, current, role_id=ids["role"].id, reason="一次")
    with pytest.raises(BizException) as pending:
        apply_role(db, current, role_id=ids["role"].id, reason="二次")
    assert "待审批" in pending.value.message
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    review(db, admin, first["id"], True, "可以")
    with pytest.raises(BizException) as member:
        apply_role(db, current, role_id=ids["role"].id, reason="再来")
    assert "无需再申请" in member.value.message


def test_admin_cannot_apply_role(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    with pytest.raises(BizException) as exc:
        apply_role(db, admin, role_id=ids["role"].id, reason="不用")
    assert "管理员" in exc.value.message


def test_list_all_forbidden_for_staff(monkeypatch) -> None:
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    with pytest.raises(BizException) as exc:
        list_applications(db, current, "all")
    assert exc.value.code == 403


def test_my_entitlements_groups_direct_and_role(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.access.service.emit", lambda *a, **k: None)
    monkeypatch.setattr("app.modules.access.service.write_audit", lambda *a, **k: None)
    db = _db()
    ids = _seed(db)
    current = CurrentUser(id=ids["user"].id, username="dev", is_admin=False)
    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    apply_role(db, current, role_id=ids["role"].id, reason="角色")
    pending = list_applications(db, current, "mine")
    review(db, admin, pending[0]["id"], True, "可以")
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
    mine = my_entitlements(db, current)
    assert mine["is_admin"] is False
    assert mine["projects"]
    proj = mine["projects"][0]
    assert proj["project_name"] == "订单"
    assert any(r["name"] == "开发者" for r in proj["roles"])
    assert any(d["resource_type"] == "pipeline" for d in proj["direct"])
    admin_view = my_entitlements(db, admin)
    assert admin_view["is_admin"] is True
    assert admin_view["projects"] == []
