# -*- coding: utf-8 -*-
"""技能库上传是全员共享：别人能用，但不能删。"""
from __future__ import annotations

import io
import zipfile

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser
from app.core.response import BizException
from app.modules.audit.models import AuditLog
from app.modules.harness import skills
from app.modules.harness.models import (
    HarnessComponent,
    HarnessDependency,
    HarnessLifecycleAudit,
    HarnessRuntime,
    HarnessToolInvocation,
    HarnessVersion,
)
from app.modules.harness.router import _require_component_manage
from app.modules.settings.models import PlatformSetting

STAFF = CurrentUser(id=7, username="dev", is_admin=False)
OTHER = CurrentUser(id=8, username="other", is_admin=False)
ADMIN = CurrentUser(id=1, username="admin", is_admin=True)


def _skill_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "manifest.yaml",
            yaml.safe_dump(
                {
                    "kind": "agent-skill",
                    "name": "release-runbook",
                    "version": "1.0.0",
                    "display_name": "发布手册",
                    "description": "发布流程说明",
                    "metadata": {"invocation_policy": "model"},
                },
                allow_unicode=True,
            ),
        )
        archive.writestr("SKILL.md", "# 发布手册\n先看流水线状态。")
    return buffer.getvalue()


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    for table in (
        HarnessComponent.__table__,
        HarnessVersion.__table__,
        HarnessDependency.__table__,
        HarnessRuntime.__table__,
        HarnessToolInvocation.__table__,
        HarnessLifecycleAudit.__table__,
        PlatformSetting.__table__,
        AuditLog.__table__,
    ):
        table.create(engine)
    return Session(engine)


def test_store_skill_upload_is_shared() -> None:
    """技能库上传进全员目录，不改成 u{id}- 前缀。别人的助手也能加载。"""
    db = _db()
    row = skills.install_package(db, _skill_zip(), actor_id=7, actor_name="dev")
    assert row.name == "release-runbook"
    version = db.get(HarnessVersion, row.current_version_id)
    assert version.created_by == 7
    assert skills.personal_owner_id(row, {}) is None
    names = [item["name"] for item in skills.catalog(db, viewer_id=8)]
    assert "release-runbook" in names
    assert all(item.get("publisher_id") == 7 for item in skills.catalog(db) if item["name"] == "release-runbook")


def test_shared_skill_only_publisher_or_admin_can_delete() -> None:
    """发布者可以下架自己共享的技能；其他人 403。"""
    db = _db()
    row = skills.install_package(db, _skill_zip(), actor_id=7, actor_name="dev")
    _require_component_manage(db, row, STAFF)
    _require_component_manage(db, row, ADMIN)
    try:
        _require_component_manage(db, row, OTHER)
        raise AssertionError("别人不该能删除共享技能")
    except BizException as exc:
        assert exc.code == 403
