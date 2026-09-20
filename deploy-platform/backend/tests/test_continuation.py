"""续接：口头确认必须核销签名；权限上下文不能把发布话术抢走。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.modules.ai.continuation import try_continue
from app.modules.ai.tokens import issue


def test_short_confirm_without_token_does_not_execute(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.modules.ai.history.last_assistant_turn",
        lambda db, cid: {
            "content": "请确认发布",
            "actions": [{"type": "confirm_release", "payload": {"pipeline_id": 15}, "token": ""}],
        },
    )
    monkeypatch.setattr("app.modules.ai.memory.get_working", lambda db, cid: {})
    monkeypatch.setattr("app.modules.ai.memory.set_working", lambda *a, **k: None)
    performed = []
    monkeypatch.setattr(
        "app.modules.ai.actions.perform_action",
        lambda *a, **k: performed.append(1) or {"reply": "不该执行"},
    )
    out = try_continue(MagicMock(), "确认", SimpleNamespace(id=1), 9)
    assert performed == []
    assert out is not None
    assert "无效" in (out.get("reply") or "")


def test_short_confirm_with_token_executes_once(monkeypatch) -> None:
    token = issue(1, 9, "confirm_release", {"pipeline_id": 15})
    monkeypatch.setattr(
        "app.modules.ai.history.last_assistant_turn",
        lambda db, cid: {
            "content": "请确认发布",
            "actions": [
                {"type": "confirm_release", "payload": {"pipeline_id": 15}, "token": token}
            ],
        },
    )
    monkeypatch.setattr("app.modules.ai.memory.get_working", lambda db, cid: {"awaiting": "confirm_card"})
    monkeypatch.setattr("app.modules.ai.memory.set_working", lambda *a, **k: None)
    performed = []

    def fake_perform(*args, **kwargs):
        performed.append(kwargs.get("persist"))
        return {"reply": "已发布", "watching": True}

    monkeypatch.setattr("app.modules.ai.actions.perform_action", fake_perform)
    out = try_continue(MagicMock(), "确认", SimpleNamespace(id=1), 9)
    assert performed == [False]
    assert out["watching"] is True
    # 同一 token 再确认应被核销
    again = try_continue(MagicMock(), "确认", SimpleNamespace(id=1), 9)
    assert "执行过" in (again.get("reply") or "") or "无效" in (again.get("reply") or "")


def test_permission_failure_context_does_not_apply_on_pipeline_name(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.modules.ai.history.last_assistant_turn",
        lambda db, cid: {
            "content": "你没有流水线「test-C」的执行权限，发不了。可以说「申请 test-C 的执行权限」。",
            "actions": [],
        },
    )
    monkeypatch.setattr("app.modules.ai.memory.get_working", lambda db, cid: {"awaiting": None})
    monkeypatch.setattr("app.modules.ai.memory.set_working", lambda *a, **k: None)
    applied = []
    monkeypatch.setattr(
        "app.modules.ai.registry.dispatch",
        lambda *a, **k: applied.append(a) or {"reply": "不该申请"},
    )
    assert try_continue(MagicMock(), "test-C", SimpleNamespace(id=1), 9) is None
    assert applied == []


def test_pick_project_does_not_apply_when_user_switches_to_release(monkeypatch) -> None:
    """问完「申请哪个项目」后改口发布，不能把项目名当申请范围交上去。"""
    monkeypatch.setattr(
        "app.modules.ai.history.last_assistant_turn",
        lambda db, cid: {
            "content": "整项目执行权含该项目所有环境。请指出项目编号或全称：",
            "actions": [],
        },
    )
    working = {
        "awaiting": "pick_project",
        "access_kind": "project",
        "catalog_projects": [
            {"id": 1, "name": "DMS 经销商协同运营平台", "code": "COP"},
            {"id": 9, "name": "AI陪练", "code": "coach"},
        ],
    }
    monkeypatch.setattr("app.modules.ai.memory.get_working", lambda db, cid: working)
    saved = []
    monkeypatch.setattr("app.modules.ai.memory.set_working", lambda db, cid, w: saved.append(w))
    applied = []
    monkeypatch.setattr(
        "app.modules.ai.registry.dispatch",
        lambda *a, **k: applied.append(a) or {"reply": "不该申请"},
    )
    out = try_continue(MagicMock(), "执行AI陪练项目生产环境", SimpleNamespace(id=1), 9)
    assert out is None
    assert applied == []
    assert saved and saved[-1].get("awaiting") is None


def test_abort_clears_awaiting_without_applying(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.modules.ai.history.last_assistant_turn",
        lambda db, cid: {"content": "请指出项目", "actions": []},
    )
    monkeypatch.setattr(
        "app.modules.ai.memory.get_working",
        lambda db, cid: {"awaiting": "pick_project", "access_kind": "project"},
    )
    saved = []
    monkeypatch.setattr("app.modules.ai.memory.set_working", lambda db, cid, w: saved.append(w))
    applied = []
    monkeypatch.setattr(
        "app.modules.ai.registry.dispatch",
        lambda *a, **k: applied.append(a) or {"reply": "不该申请"},
    )
    out = try_continue(MagicMock(), "算了", SimpleNamespace(id=1), 9)
    assert applied == []
    assert "取消" in (out.get("reply") or "")
    assert saved and saved[-1].get("awaiting") is None
