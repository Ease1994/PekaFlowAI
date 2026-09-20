# -*- coding: utf-8 -*-
"""待审通知点进去必须进办事页，不能进执行画布。"""
from types import SimpleNamespace

from app.modules.notify.events import action_path
from app.modules.notify.service import public


def test_pending_approval_opens_approvals_page() -> None:
    assert action_path("release.approval.pending", related_id=14) == "/approvals?release_id=14"
    assert action_path("pm.confirm.pending", related_id=9) == "/approvals?tab=pm"
    assert action_path("access.apply", related_id=3) == "/permissions?tab=pending&id=3"
    assert action_path("access.review", related_id=3) == "/permissions?tab=history"


def test_other_events_have_no_forced_path() -> None:
    assert action_path("release.success", related_id=14) == ""
    assert action_path("release.failed", related_id=14) == ""


def test_public_rewrites_old_pending_execution_link() -> None:
    row = SimpleNamespace(
        id=1,
        title="生产发布待审批",
        content="",
        kind="release.approval.pending",
        link="/executions/9/14",
        related_type="release",
        related_id=14,
        is_read=False,
        created_at=None,
    )
    assert public(row)["link"] == "/approvals?release_id=14"
