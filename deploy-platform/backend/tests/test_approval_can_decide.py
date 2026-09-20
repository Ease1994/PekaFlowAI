# -*- coding: utf-8 -*-
"""审批列表的通过/驳回按钮必须和 decide 接口同一套权限，不能只看「待审批」。"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401
from app.core.deps import CurrentUser
from app.db.base import Base
from app.modules.approval.models import Approval
from app.modules.approval.router import _can_decide
from app.modules.auth.models import Permission, User
from app.modules.pipeline.models import Pipeline, Release
from app.modules.project.models import Group, Project


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db: Session) -> dict:
    requester = User(username="alice", display_name="Alice", password_hash="x", is_admin=False)
    admin = User(username="admin", display_name="系统管理员", password_hash="x", is_admin=True)
    db.add_all([requester, admin])
    db.flush()
    proj = Project(name="DMS", code="dms", description="")
    db.add(proj)
    db.flush()
    prod = Group(project_id=proj.id, name="生产", type="prod", approval_required=True)
    db.add(prod)
    db.flush()
    pipe = Pipeline(project_id=proj.id, group_id=prod.id, name="prod-api", yaml="")
    db.add(pipe)
    db.flush()
    rel = Release(
        pipeline_id=pipe.id,
        group_id=prod.id,
        version="v1",
        status="pending",
        operator_id=requester.id,
        trigger_by="manual",
    )
    db.add(rel)
    db.flush()
    row = Approval(release_id=rel.id, approver_id=admin.id, status="pending", comment="")
    db.add(row)
    db.commit()
    return {"requester": requester, "admin": admin, "rel": rel, "row": row, "prod": prod}


def test_initiator_cannot_decide_when_approver_is_someone_else():
    db = _db()
    ids = _seed(db)
    me = CurrentUser(id=ids["requester"].id, username="alice", is_admin=False)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is False


def test_assigned_approver_with_permission_can_decide():
    db = _db()
    ids = _seed(db)
    ops = User(username="ops", display_name="运维", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    ids["row"].approver_id = ops.id
    db.add(
        Permission(
            user_id=ops.id,
            resource_type="group",
            resource_id=ids["prod"].id,
            action="approve",
        )
    )
    db.commit()
    me = CurrentUser(id=ops.id, username="ops", is_admin=False)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is True


def test_assigned_approver_without_permission_cannot_decide():
    db = _db()
    ids = _seed(db)
    ops = User(username="ops", display_name="运维", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    ids["row"].approver_id = ops.id
    db.commit()
    me = CurrentUser(id=ops.id, username="ops", is_admin=False)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is False


def test_admin_can_decide_even_if_not_assignee():
    db = _db()
    ids = _seed(db)
    me = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is True


def test_finished_release_cannot_decide():
    db = _db()
    ids = _seed(db)
    ids["rel"].status = "cancelled"
    db.commit()
    me = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is False


def test_admin_pending_list_includes_ticket_assigned_to_someone_else():
    """管理员自己发起的生产发布被派给别人后，待我审批仍能看到。"""
    from app.modules.approval.service import list_decidable_pending, pending_approval_for_reviewer

    db = _db()
    ids = _seed(db)
    ops = User(username="ops", display_name="运维", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    ids["row"].approver_id = ops.id
    ids["rel"].operator_id = ids["admin"].id
    db.commit()

    admin_rows = list_decidable_pending(db, reviewer_id=ids["admin"].id, is_admin=True, limit=50)
    assert [r.id for r in admin_rows] == [ids["row"].id]

    ops_rows = list_decidable_pending(db, reviewer_id=ops.id, is_admin=False, limit=50)
    assert [r.id for r in ops_rows] == [ids["row"].id]

    initiator = list_decidable_pending(db, reviewer_id=ids["requester"].id, is_admin=False, limit=50)
    assert initiator == []

    found = pending_approval_for_reviewer(
        db, release_id=ids["rel"].id, reviewer_id=ids["admin"].id, is_admin=True
    )
    assert found is not None and found.id == ids["row"].id
    assert (
        pending_approval_for_reviewer(
            db, release_id=ids["rel"].id, reviewer_id=ids["requester"].id, is_admin=False
        )
        is None
    )


def test_admin_can_approve_release_assigned_to_someone_else():
    """管理员点通过时，不能因为待办不在自己名下被拦住。"""
    from app.core.response import BizException
    from app.modules.pipeline.service import RELEASE_QUEUED, approve_release

    db = _db()
    ids = _seed(db)
    ops = User(username="ops", display_name="运维", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    ids["row"].approver_id = ops.id
    ids["rel"].operator_id = ids["admin"].id
    db.commit()

    try:
        approve_release(db, ids["rel"].id, True, "代审", reviewer_id=ids["requester"].id)
        raise AssertionError("普通发起人不该批别人名下的待办")
    except BizException as exc:
        assert "不在待审批名单" in str(exc.message or exc)

    updated = approve_release(db, ids["rel"].id, True, "管理员代审", reviewer_id=ids["admin"].id)
    assert updated.status == RELEASE_QUEUED
    db.refresh(ids["row"])
    assert ids["row"].status == "approved"
    assert ids["row"].approver_id == ids["admin"].id


def test_pick_release_approvers_includes_initiator_when_self_approval_on():
    """开了自审：发起人也进或签池，不能因为已经有别人就批不了自己。"""
    from app.modules.approval.service import pick_release_approvers

    db = _db()
    ids = _seed(db)
    ops = User(username="ops", display_name="运维", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    db.add(
        Permission(
            user_id=ops.id,
            resource_type="group",
            resource_id=ids["prod"].id,
            action="approve",
        )
    )
    db.commit()
    picked = pick_release_approvers(
        db,
        group_id=ids["prod"].id,
        requester_id=ids["admin"].id,
        allow_self_approval=True,
    )
    assert [u.id for u in picked] == [ops.id, ids["admin"].id]

    picked_off = pick_release_approvers(
        db,
        group_id=ids["prod"].id,
        requester_id=ids["admin"].id,
        allow_self_approval=False,
    )
    assert [u.id for u in picked_off] == [ops.id]


def test_initiator_can_self_approve_when_switch_on():
    """分组开了自审、发起人有审批权：待办即使派给别人，自己也能批。"""
    from app.modules.approval.router import pending_approvals
    from app.modules.approval.service import pending_approval_for_reviewer
    from app.modules.pipeline.service import RELEASE_QUEUED, approve_release

    db = _db()
    ids = _seed(db)
    ids["prod"].allow_self_approval = True
    db.add(
        Permission(
            user_id=ids["requester"].id,
            resource_type="group",
            resource_id=ids["prod"].id,
            action="approve",
        )
    )
    db.commit()
    me = CurrentUser(id=ids["requester"].id, username="alice", is_admin=False)
    assert _can_decide(db, me, ids["row"], ids["rel"]) is True
    assert pending_approval_for_reviewer(
        db, release_id=ids["rel"].id, reviewer_id=ids["requester"].id, is_admin=False
    ) is not None
    listed = pending_approvals(scope="pending", status="", limit=200, db=db, current=me).data
    assert len(listed) == 1
    assert listed[0]["can_decide"] is True
    updated = approve_release(db, ids["rel"].id, True, "自审通过", reviewer_id=ids["requester"].id)
    assert updated.status == RELEASE_QUEUED


def test_or_sign_pending_list_is_one_row_with_merged_approvers():
    """或签：同一发布派给 P 和管理员时，待我审批只出现一张单，审批人合并显示。"""
    from app.modules.approval.router import pending_approvals
    from app.modules.approval.service import list_decidable_pending

    db = _db()
    ids = _seed(db)
    ops = User(username="p", display_name="P", password_hash="x", is_admin=False)
    db.add(ops)
    db.flush()
    db.add(Approval(release_id=ids["rel"].id, approver_id=ops.id, status="pending", comment=""))
    db.add(
        Permission(
            user_id=ops.id,
            resource_type="group",
            resource_id=ids["prod"].id,
            action="approve",
        )
    )
    db.commit()

    admin_rows = list_decidable_pending(db, reviewer_id=ids["admin"].id, is_admin=True, limit=50)
    assert len({row.release_id for row in admin_rows}) == 1
    assert len(admin_rows) == 1

    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    listed = pending_approvals(scope="pending", status="", limit=200, db=db, current=admin).data
    assert len(listed) == 1
    assert listed[0]["release_id"] == ids["rel"].id
    assert "P" in listed[0]["approver"]
    assert "系统管理员" in listed[0]["approver"]
    assert listed[0]["can_decide"] is True
    assert listed[0]["action"] == "release"

    p_user = CurrentUser(id=ops.id, username="p", is_admin=False)
    p_listed = pending_approvals(scope="pending", status="", limit=200, db=db, current=p_user).data
    assert len(p_listed) == 1
    assert "P" in p_listed[0]["approver"]
    assert "系统管理员" in p_listed[0]["approver"]
    assert p_listed[0]["can_decide"] is True


def test_approval_list_shows_rollback_and_commit_title() -> None:
    """回滚单要标成回滚，变更列用被撤销那次的 commit 说明，而不是内部发布主键。"""
    from app.modules.approval.router import pending_approvals

    db = _db()
    ids = _seed(db)
    orig = Release(
        pipeline_id=ids["rel"].pipeline_id,
        group_id=ids["rel"].group_id,
        build_number=8,
        version="v0",
        status="success",
        trigger_by="manual",
        source_ref="39a59fe3b4c000000000",
        snapshot=json.dumps(
            {"commit_title": "修复登录超时", "commit_short": "39a59fe3"},
            ensure_ascii=False,
        ),
    )
    db.add(orig)
    db.flush()
    rb = ids["rel"]
    rb.trigger_by = "rollback"
    rb.rollback_of_release_id = orig.id
    rb.build_number = 9
    rb.source_ref = orig.source_ref
    rb.snapshot = "{}"
    rb.plan_json = json.dumps(
        {"summary": "增量文件发布 /data/nginx/html/aicoach-web/"},
        ensure_ascii=False,
    )
    db.commit()

    admin = CurrentUser(id=ids["admin"].id, username="admin", is_admin=True)
    listed = pending_approvals(scope="pending", status="", limit=200, db=db, current=admin).data
    assert listed[0]["action"] == "rollback"
    assert listed[0]["action_label"] == "回滚"
    assert listed[0]["build_number"] == 9
    assert listed[0]["rollback_of_build_number"] == 8
    assert listed[0]["commit_title"] == "修复登录超时"
    assert listed[0]["commit_short"] == "39a59fe3"
    assert "aicoach-web" in listed[0]["undo_summary"]
