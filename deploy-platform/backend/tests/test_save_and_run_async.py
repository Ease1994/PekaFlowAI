"""保存并执行不能同步等 Git / 企微。"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.modules.notify import center
from app.modules.pipeline import source_ref_service


def test_fill_source_ref_later_returns_before_git_finishes(monkeypatch):
    """后台问 SHA 时，调用方必须立刻返回。"""

    def slow(_release_id: int) -> None:
        time.sleep(1.2)

    monkeypatch.setattr(source_ref_service, "_fill_source_ref", slow)
    started = time.monotonic()
    source_ref_service.fill_source_ref_later(1)
    assert time.monotonic() - started < 0.4


def test_emit_returns_before_wecom_finishes(monkeypatch):
    """审批通知写站内信后立刻返回，企微 gettoken 不能拖住创建发布。"""

    def slow_wecom(*_a, **_k) -> None:
        time.sleep(1.2)

    monkeypatch.setattr(center, "spec_of", lambda _event: SimpleNamespace(
        channels=("in_app", "wecom"),
        related_type="release",
    ))
    monkeypatch.setattr(center, "_recipients", lambda *_a, **_k: [9])
    monkeypatch.setattr(center, "deliver_in_app", lambda *_a, **_k: object())
    monkeypatch.setattr(center, "_deliver_wecom_later", slow_wecom)

    started = time.monotonic()
    written = center.emit(
        None,
        "release.approval.pending",
        user_ids=[9],
        title="待审批",
        content="详情",
        link="/approvals",
    )
    assert written == 1
    assert time.monotonic() - started < 0.4
