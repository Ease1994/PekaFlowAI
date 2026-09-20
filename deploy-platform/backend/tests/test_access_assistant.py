"""权限申请范围识别、工作记忆续接：整项目 / 环境 / 单条，不列流水线清单。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.modules.ai.continuation import _continue_access_scope
from app.modules.ai.intent import (
    is_group_wide_access,
    is_project_wide_access,
    is_role_access_intent,
    parse_env_code,
)
from app.modules.ai.memory import absorb_tool
from app.modules.ai.tool_select import names_for_turn


def test_parse_env_code_ignores_pipeline_name() -> None:
    assert parse_env_code("申请 test-C 执行权限") == ""
    assert parse_env_code("我要1项目测试环境所有流水线执行权限") == "test"
    assert parse_env_code("只要测试") == "test"
    assert parse_env_code("测试") == "test"


def test_group_wide_is_not_project_wide() -> None:
    text = "我要1项目测试环境所有流水线执行权限"
    assert is_group_wide_access(text)
    assert not is_project_wide_access(text)


def test_role_apply_intent() -> None:
    assert is_role_access_intent("申请订单项目的开发者角色")
    assert not is_role_access_intent("申请订单测试流水线的执行权限")


def test_names_for_turn_lookup_and_review() -> None:
    cats = frozenset({"access"})

    def visible(message: str, name: str) -> bool:
        picked = names_for_turn(message, categories=cats)
        return picked is None or name in picked

    assert visible("我的申请怎么样了", "list_my_access_applications")
    assert visible("有哪些待我审批", "list_pending_access_applications")
    assert visible("通过 #12", "propose_review_access")
    assert visible("撤销申请 12", "cancel_access_application")
    assert visible("取消这次权限申请", "cancel_access_application")
    assert visible("撤回执行权申请", "cancel_access_application")
    assert visible("那张申请不要了", "cancel_access_application")
    assert visible("作废申请 7", "cancel_access_application")


def test_absorb_catalog_waits_for_scope_not_pipeline() -> None:
    working = absorb_tool(
        {},
        "list_access_catalog",
        {},
        {
            "projects": [
                {
                    "id": 1,
                    "name": "DMS",
                    "code": "dms",
                    "groups": [{"id": 2, "name": "测试", "env": "test"}],
                }
            ],
            "awaiting": "pick_scope",
            "access_kind": "scope",
        },
    )
    assert working["awaiting"] == "pick_scope"
    assert working["selected_project_id"] == 1
    assert working.get("catalog") == []


def test_absorb_catalog_keeps_requested_actions() -> None:
    working = absorb_tool(
        {},
        "list_access_catalog",
        {},
        {
            "projects": [{"id": 1, "name": "DMS", "code": "dms", "groups": []}],
            "awaiting": "pick_scope",
            "requested_actions": ["read", "update"],
        },
    )
    assert working["requested_actions"] == ["read", "update"]


def test_continue_pick_scope_applies_group(monkeypatch) -> None:
    calls = []

    def fake_dispatch(db, name, params, current):
        calls.append((name, params))
        return {"reply": "已提交环境申请", "application_id": 9}

    monkeypatch.setattr("app.modules.ai.registry.dispatch", fake_dispatch)
    working = {
        "awaiting": "pick_scope",
        "selected_project_id": 1,
        "catalog_projects": [{"id": 1, "name": "DMS", "code": "dms"}],
    }
    out = _continue_access_scope(MagicMock(), "只要测试", MagicMock(), working)
    assert out is not None
    assert calls == [
        (
            "apply_group_execute",
            {
                "project_id": 1,
                "env": "test",
                "reason": "助手代为申请该环境全部分组流水线权限",
            },
        )
    ]
    assert "环境申请" in out["reply"]


def test_continue_pick_scope_forwards_requested_actions(monkeypatch) -> None:
    calls = []

    def fake_dispatch(db, name, params, current):
        calls.append((name, params))
        return {"reply": "已提交", "application_id": 4}

    monkeypatch.setattr("app.modules.ai.registry.dispatch", fake_dispatch)
    working = {
        "awaiting": "pick_scope",
        "selected_project_id": 1,
        "catalog_projects": [{"id": 1, "name": "DMS", "code": "dms"}],
        "requested_actions": ["read", "update", "delete"],
    }
    out = _continue_access_scope(MagicMock(), "整个项目", MagicMock(), working)
    assert out is not None
    assert calls[0][1]["actions"] == ["read", "update", "delete"]


def test_continue_pick_scope_applies_project(monkeypatch) -> None:
    calls = []

    def fake_dispatch(db, name, params, current):
        calls.append((name, params))
        return {"reply": "已提交项目申请", "application_id": 8}

    monkeypatch.setattr("app.modules.ai.registry.dispatch", fake_dispatch)
    working = {
        "awaiting": "pick_scope",
        "selected_project_id": 1,
        "catalog_projects": [{"id": 1, "name": "DMS", "code": "dms"}],
    }
    out = _continue_access_scope(MagicMock(), "整个项目", MagicMock(), working)
    assert out is not None
    assert calls[0][0] == "apply_project_execute"
    assert "项目申请" in out["reply"]


def test_project_name_matches_space_or_hyphen() -> None:
    """用户说「DMS 经销商」也能对上库里的「DMS-经销商」。"""
    from app.modules.access.service import _project_matches_keyword

    name = "DMS-经销商协同运营平台"
    assert _project_matches_keyword("DMS 经销商协同运营平台", name, "dms")
    assert _project_matches_keyword("申请一下 DMS 经销商协同运营平台 的执行发布权限", name, "dms")
    assert _project_matches_keyword("DMS", name, "dms")
    assert not _project_matches_keyword("订单中心", name, "dms")


def test_list_access_catalog_asks_scope_instead_of_looping() -> None:
    """命中唯一项目时直接把范围问出来，本轮有 reply 就会停，不再换关键字搜。"""
    from app.modules.ai.chat import _repeat_list_tool_skip, _skill_replies
    from app.modules.ai.skills.access import _list_access_catalog

    current = MagicMock(id=1, username="u1", is_admin=False)
    data = {
        "projects": [
            {
                "id": 1,
                "name": "DMS-经销商协同运营平台",
                "code": "dms",
                "groups": [{"id": 2, "name": "测试", "env": "test", "already_execute": False}],
            }
        ]
    }
    with patch("app.modules.access.service.catalog_for_apply", return_value=data):
        out = _list_access_catalog(MagicMock(), current, {"keyword": "DMS"})
    assert out.get("_ask")
    assert "整个项目" in out["reply"]
    traces = [{"name": "list_access_catalog", "arguments": {"keyword": "DMS"}, "result": out}]
    assert _skill_replies(traces) == [out["reply"]]
    assert _repeat_list_tool_skip("list_access_catalog", traces)


def test_ask_user_formats_options() -> None:
    from app.modules.ai.skills.platform import _ask_user

    out = _ask_user(MagicMock(), MagicMock(), {"question": "按哪一层申请？", "options": ["整个项目", "只要测试"]})
    assert out["_ask"]
    assert "整个项目" in out["reply"]
    assert "只要测试" in out["reply"]
