# -*- coding: utf-8 -*-
"""项目经理功能：默认不挡发布；打开闸门后才多一道确认。"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401
from app.db.base import Base
from app.modules.auth.models import User
from app.modules.pipeline.models import Pipeline, Release
from app.modules.pipeline.service import (
    RELEASE_PENDING,
    RELEASE_QUEUED,
    approve_release,
    create_release,
)
from app.modules.pm import service as pm
from app.modules.pm.models import PmDecision, ProjectMember
from app.modules.project.models import Group, Project
from app.modules.settings.models import PlatformSetting


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db: Session) -> dict:
    admin = User(username="admin", display_name="管理员", password_hash="x", is_admin=False)
    dev = User(username="dev", display_name="开发", password_hash="x")
    mgr = User(username="pm", display_name="项目经理", password_hash="x")
    db.add_all([admin, dev, mgr])
    db.flush()
    proj = Project(name="订单", code="order", description="")
    db.add(proj)
    db.flush()
    test = Group(project_id=proj.id, name="测试", type="test", approval_required=False)
    prod = Group(
        project_id=proj.id,
        name="生产",
        type="prod",
        approval_required=True,
        allow_self_approval=True,
        allow_emergency_bypass=True,
    )
    db.add_all([test, prod])
    db.flush()
    test_pipe = Pipeline(project_id=proj.id, group_id=test.id, name="order-test", yaml="")
    prod_pipe = Pipeline(project_id=proj.id, group_id=prod.id, name="order-prod", yaml="")
    db.add_all([test_pipe, prod_pipe])
    db.commit()
    return {
        "proj": proj,
        "test": test,
        "prod": prod,
        "test_pipe": test_pipe,
        "prod_pipe": prod_pipe,
        "dev": dev,
        "mgr": mgr,
        "admin": admin,
    }


def test_default_off_does_not_block_test_or_prod_gate_logic(db_ids=None):
    db = _db()
    ids = _seed(db)
    assert pm.confirmation_required(db, ids["proj"], ids["prod"]) is False
    r = create_release(
        db,
        pipeline_id=ids["test_pipe"].id,
        version="v1",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
    )
    assert r.status == RELEASE_QUEUED
    assert pm.pm_still_blocking(db, r.id) is False


def test_gate_on_without_pm_still_does_not_block():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["prod"].pm_approval_required = True
    db.commit()
    assert pm.confirmation_required(db, ids["proj"], ids["prod"]) is False
    r = create_release(
        db,
        pipeline_id=ids["test_pipe"].id,
        version="v1",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
    )
    assert r.status == RELEASE_QUEUED


def test_gate_on_with_pm_holds_even_on_test_group():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["test"].pm_approval_required = True
    db.add(ProjectMember(project_id=ids["proj"].id, user_id=ids["mgr"].id, role="pm"))
    db.commit()
    assert pm.confirmation_required(db, ids["proj"], ids["test"]) is True
    r = create_release(
        db,
        pipeline_id=ids["test_pipe"].id,
        version="v2",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
        brief={"business_summary": "订单分页修复"},
    )
    assert r.status == RELEASE_PENDING
    assert r.business_summary == "订单分页修复"
    assert pm.pm_still_blocking(db, r.id) is True
    rows = db.query(PmDecision).filter(PmDecision.release_id == r.id).all()
    assert len(rows) == 1
    assert rows[0].reviewer_id == ids["mgr"].id

    updated = pm.decide_pm(
        db,
        rows[0].id,
        reviewer_id=ids["mgr"].id,
        approved=True,
        comment="",
    )
    assert updated.status == RELEASE_QUEUED
    assert pm.pm_still_blocking(db, r.id) is False


def test_emergency_bypass_skips_pm_and_does_not_create_decision():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["prod"].pm_approval_required = True
    db.add(ProjectMember(project_id=ids["proj"].id, user_id=ids["mgr"].id, role="pm"))
    db.add(PlatformSetting(key="emergency_bypass_enabled", value="true"))
    db.commit()
    r = create_release(
        db,
        pipeline_id=ids["prod_pipe"].id,
        version="hotfix",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
        emergency_bypass=True,
        emergency_bypass_reason="线上支付挂了",
    )
    assert r.status == RELEASE_QUEUED
    assert db.query(PmDecision).filter(PmDecision.release_id == r.id).count() == 0


def test_platform_switch_off_blocks_emergency_bypass_even_if_group_allows():
    db = _db()
    ids = _seed(db)
    from app.core.response import BizException
    from app.modules.pipeline.service import can_emergency_bypass
    from app.modules.settings.models import PlatformSetting

    db.add(PlatformSetting(key="emergency_bypass_enabled", value="false"))
    db.commit()
    assert can_emergency_bypass(db, ids["prod"]) is False
    try:
        create_release(
            db,
            pipeline_id=ids["prod_pipe"].id,
            version="hotfix",
            strategy="rolling",
            trigger_by="manual",
            operator_id=ids["dev"].id,
            emergency_bypass=True,
            emergency_bypass_reason="线上支付挂了",
        )
        raise AssertionError("平台关了跳审仍放行了")
    except BizException as e:
        assert "平台已关闭应急跳审" in str(e.message or e)


def test_missing_platform_setting_blocks_emergency_bypass():
    """没写总闸配置时按关闭，不能靠缺省 true 跳审上生产。"""
    db = _db()
    ids = _seed(db)
    from app.core.response import BizException
    from app.modules.pipeline.service import can_emergency_bypass

    assert can_emergency_bypass(db, ids["prod"]) is False
    try:
        create_release(
            db,
            pipeline_id=ids["prod_pipe"].id,
            version="hotfix",
            strategy="rolling",
            trigger_by="manual",
            operator_id=ids["dev"].id,
            emergency_bypass=True,
            emergency_bypass_reason="线上支付挂了",
        )
        raise AssertionError("缺省总闸仍放行了跳审")
    except BizException as e:
        assert "平台已关闭应急跳审" in str(e.message or e)


def test_tech_approve_waits_for_pm():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["prod"].pm_approval_required = True
    ids["prod"].allow_self_approval = True
    db.add(ProjectMember(project_id=ids["proj"].id, user_id=ids["mgr"].id, role="pm"))
    db.commit()
    from app.modules.approval.models import Approval
    from app.modules.auth.models import Permission

    db.add(
        Permission(
            user_id=ids["dev"].id,
            resource_type="group",
            resource_id=ids["prod"].id,
            action="approve",
            effect="allow",
        )
    )
    db.commit()
    r2 = create_release(
        db,
        pipeline_id=ids["prod_pipe"].id,
        version="v4",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
    )
    tech = db.query(Approval).filter(Approval.release_id == r2.id, Approval.status == "pending").first()
    assert tech is not None
    approved = approve_release(db, r2.id, True, "技术没问题", reviewer_id=ids["dev"].id)
    assert approved.status == RELEASE_PENDING
    assert pm.pm_still_blocking(db, r2.id) is True
    decision = db.query(PmDecision).filter(PmDecision.release_id == r2.id, PmDecision.status == "pending").one()
    done = pm.decide_pm(db, decision.id, reviewer_id=ids["mgr"].id, approved=True, comment="今晚可以")
    assert done.status == RELEASE_QUEUED


def test_assign_pm_grants_project_read():
    db = _db()
    ids = _seed(db)
    pm.set_project_pms(db, ids["proj"].id, [ids["mgr"].id])
    from app.modules.auth.models import Permission

    row = (
        db.query(Permission)
        .filter(
            Permission.user_id == ids["mgr"].id,
            Permission.resource_type == "project",
            Permission.resource_id == ids["proj"].id,
            Permission.action == "read",
        )
        .first()
    )
    assert row is not None
    assert pm.is_project_pm(db, ids["mgr"].id, ids["proj"].id)


def test_draft_announcement_uses_written_summary():
    db = _db()
    ids = _seed(db)
    r = Release(
        pipeline_id=ids["test_pipe"].id,
        group_id=ids["test"].id,
        build_number=3,
        version="v9",
        status="success",
        business_summary="修复支付回调",
        impact_scope="全部用户",
        audience="C 端",
        need_user_notice=True,
    )
    db.add(r)
    db.commit()
    text = pm.draft_announcement(db, r)
    assert "修复支付回调" in text
    assert "请通知用户" in text
    assert "订单" in text


def test_rebuild_also_waits_for_pm():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["test"].pm_approval_required = True
    db.add(ProjectMember(project_id=ids["proj"].id, user_id=ids["mgr"].id, role="pm"))
    db.commit()
    src = create_release(
        db,
        pipeline_id=ids["test_pipe"].id,
        version="v1",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
    )
    # 上一单还在 pending 等项目经理，Rebuild 同一条线
    from app.modules.pipeline.service import rebuild_release

    rebuilt = rebuild_release(db, src.id, ids["dev"].id)
    assert rebuilt.status == RELEASE_PENDING
    assert pm.pm_still_blocking(db, rebuilt.id) is True


def test_cancel_clears_pending_pm_decisions():
    db = _db()
    ids = _seed(db)
    ids["proj"].pm_enabled = True
    ids["test"].pm_approval_required = True
    db.add(ProjectMember(project_id=ids["proj"].id, user_id=ids["mgr"].id, role="pm"))
    db.commit()
    r = create_release(
        db,
        pipeline_id=ids["test_pipe"].id,
        version="v1",
        strategy="rolling",
        trigger_by="manual",
        operator_id=ids["dev"].id,
    )
    assert pm.pm_still_blocking(db, r.id) is True
    from app.modules.pipeline.service import cancel_release

    cancel_release(db, r.id, ids["dev"].id)
    assert pm.pm_still_blocking(db, r.id) is False
    left = db.query(PmDecision).filter(PmDecision.release_id == r.id, PmDecision.status == "pending").count()
    assert left == 0


def test_brief_fields_are_clipped_to_column_limits():
    req = type("R", (), {})()
    pm.apply_brief(
        req,
        {
            "iteration_tag": "x" * 200,
            "planned_window": "y" * 200,
            "audience": "z" * 400,
        },
    )
    assert len(req.iteration_tag) == 64
    assert len(req.planned_window) == 128
    assert len(req.audience) == 256
