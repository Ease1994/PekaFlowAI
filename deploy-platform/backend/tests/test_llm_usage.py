"""按意图少发工具、按用户记 Token。"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.modules.ai.chat import _catalog_answer_ready, _chat_with_llm
from app.modules.ai.tool_select import CORE_CATEGORIES, categories_for_turn, names_for_turn
from app.modules.llm.models import LlmObservation
from app.modules.llm.service import record_invocation, usage_by_user
from tests.test_ai_chat_reply import TWO_NODES, WAIT_NODES, _patch_chat_loop, _scripted_turn


def test_authoring_tools_only_when_asked() -> None:
    assert "authoring" not in categories_for_turn("查询全部节点")
    assert "access" in categories_for_turn("查询全部节点")
    assert CORE_CATEGORIES <= categories_for_turn("查询全部节点")
    assert "authoring" in categories_for_turn("帮我实现一个 agent 技能，查询流水线状态")
    assert "authoring" in categories_for_turn("安装一下读取未读通知")
    assert "authoring" not in categories_for_turn("查一下我的未读消息")
    assert "access" in categories_for_turn("申请 test-C 的执行权限")
    assert "authoring" in categories_for_turn("继续", selected_skills=["rp-plugin-draft"])
    assert "authoring" in categories_for_turn(
        "改一下名字",
        history=[{"role": "user", "content": "写一个查询发布状态的技能"}],
    )


def _tool_visible(message: str, name: str) -> bool:
    """检索命中该工具，或分太低发全量（None），模型都能看到它。"""
    picked = names_for_turn(message, categories=categories_for_turn(message))
    return picked is None or name in picked


def test_skill_descriptions_are_semantic_not_utterance_whitelist() -> None:
    """发给模型的说明写能力，不写「用户说 XXX 时用」。"""
    from app.modules.ai.playbooks import all_playbooks
    from app.modules.ai.registry import all_skills
    from app.modules.ai.skills import load_all

    load_all()
    for skill in all_skills():
        assert "用户说" not in (skill.description or ""), skill.name
    for item in all_playbooks():
        assert "用户说" not in item.description, item.name
        assert "时加载" not in item.description, item.name


def test_confusable_tools_stay_visible_by_semantics() -> None:
    """易混工具靠说明/例句区分，检索只保证该看到的还在候选里。"""
    assert _tool_visible("取消这次发布", "propose_cancel")
    assert _tool_visible("停掉正在跑的流水线", "propose_cancel")
    assert _tool_visible("取消这次权限申请", "cancel_access_application")
    assert _tool_visible("那张申请不要了", "cancel_access_application")
    assert _tool_visible("查询全部节点", "list_push_nodes")
    assert _tool_visible("构建机在线吗", "list_agents")
    assert _tool_visible("有哪些待我审批的发布", "list_pending_approvals")
    assert _tool_visible("有哪些权限申请待审批", "list_pending_access_applications")
    assert _tool_visible("回滚上一笔发布", "propose_rollback")
    assert _tool_visible("把失败的那次 Rebuild", "propose_rebuild")
    cancel_release = names_for_turn("取消这次发布", categories=categories_for_turn("取消这次发布"))
    assert cancel_release is None or "cancel_access_application" not in cancel_release
    cancel_app = names_for_turn("取消这次权限申请", categories=categories_for_turn("取消这次权限申请"))
    assert cancel_app is None or "propose_cancel" not in cancel_app
    nodes = names_for_turn("查询全部节点", categories=categories_for_turn("查询全部节点"))
    assert nodes is None or "list_agents" not in nodes


def test_status_question_only_exposes_get_release_status() -> None:
    """检索只收窄候选，不把整轮锁死成一个工具。"""
    cats = categories_for_turn("查一下test-C的状态")
    status_names = names_for_turn("查一下test-C的状态", categories=cats)
    assert status_names is None or "get_release_status" in status_names
    drafted = names_for_turn(
        "帮我实现一个 agent 技能，查询流水线状态",
        categories=categories_for_turn("帮我实现一个 agent 技能，查询流水线状态"),
    )
    assert drafted is not None
    assert "propose_agent_skill" in drafted
    uninstall = names_for_turn(
        "卸载消息通知查看这个技能",
        categories=categories_for_turn("卸载消息通知查看这个技能"),
    )
    assert uninstall is not None
    assert "propose_agent_skill_lifecycle" in uninstall
    assert "list_notifications" not in uninstall
    install = names_for_turn(
        "安装一下读取未读通知",
        categories=categories_for_turn("安装一下读取未读通知"),
    )
    assert install is not None
    assert "propose_agent_skill_lifecycle" in install
    assert "list_notifications" not in install
    assert _tool_visible("我要1项目下的所有流水线执行权限", "apply_project_execute")
    assert _tool_visible("申请 test-C 的执行权限", "apply_pipeline_execute")
    assert _tool_visible("申请1项目测试环境执行权限", "apply_group_execute")
    assert _tool_visible("执行AI陪练项目生产环境", "propose_release")
    mixed = "申请一下1项目的执行发布权限"
    mixed_names = names_for_turn(mixed, categories=categories_for_turn(mixed))
    assert mixed_names != frozenset({"propose_release"})
    assert _tool_visible(mixed, "apply_project_execute") or _tool_visible(mixed, "apply_pipeline_execute")


def test_openai_tools_drops_authoring_on_catalog_questions() -> None:
    from app.modules.ai.skills import load_all
    from app.modules.harness import tools
    from tests.test_harness_isolation import _memory_db

    load_all()
    with _memory_db() as db:
        all_names = {item["function"]["name"] for item in tools.openai_tools(db)}
        slim = tools.openai_tools(db, categories=categories_for_turn("查询全部节点"))
        names = {item["function"]["name"] for item in slim}
        status_tools = tools.openai_tools(
            db,
            categories=categories_for_turn("查一下test-C的状态"),
            names=names_for_turn("查一下test-C的状态", categories=categories_for_turn("查一下test-C的状态")),
        )
    assert "propose_agent_skill" in all_names
    assert "propose_agent_skill_lifecycle" in all_names
    assert "propose_agent_skill" not in names
    assert "list_push_nodes" in names
    assert "skill" in names
    status_names = {item["function"]["name"] for item in status_tools}
    assert "get_release_status" in status_names
    assert "propose_agent_skill" not in status_names
    desc = next(item["function"]["description"] for item in slim if item["function"]["name"] == "list_push_nodes")
    assert len(desc) <= 140


def test_catalog_list_skips_second_model_round(monkeypatch) -> None:
    """「有哪些节点」查出列表后不要再调一轮模型复述。"""
    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn(WAIT_NODES, "list_push_nodes")],
        {"list_push_nodes": TWO_NODES},
    )
    out = _chat_with_llm(
        MagicMock(),
        "查询全部节点",
        SimpleNamespace(id=1, username="admin", is_admin=True),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert "web-1" in out["reply"]
    assert "正在查询" not in out["reply"]


def test_catalog_answer_ready_ignores_status_questions() -> None:
    traces = [{"name": "list_pipelines", "result": {"pipelines": [{"id": 1, "name": "a"}]}}]
    assert _catalog_answer_ready("查询全部流水线", traces)
    assert not _catalog_answer_ready("查询流水线状态，正在发布的有哪些", traces)


def test_usage_by_user_rolls_up_tokens(tmp_path) -> None:
    from app.db.base import Base
    from app.modules.auth.models import User

    engine = create_engine(f"sqlite:///{tmp_path / 'usage.db'}")
    Base.metadata.create_all(engine, tables=[User.__table__, LlmObservation.__table__])
    with Session(engine) as db:
        user = User(username="alice", display_name="爱丽丝", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        record_invocation(
            db,
            task="assistant",
            meta={"id": 1, "model_id": "qwen", "provider_code": "dashscope"},
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            user_id=user.id,
            username="alice",
        )
        record_invocation(
            db,
            task="assistant",
            meta={"id": 1, "model_id": "qwen", "provider_code": "dashscope"},
            usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            user_id=user.id,
            username="alice",
        )
        old = LlmObservation(
            task="assistant",
            username="alice",
            user_id=user.id,
            input_tokens=999,
            output_tokens=1,
            total_tokens=1000,
            finished_at=datetime.now() - timedelta(days=40),
        )
        db.add(old)
        db.commit()
        summary = usage_by_user(db, days=7)
    assert summary["totals"]["requests"] == 2
    assert summary["totals"]["total_tokens"] == 180
    assert summary["users"][0]["username"] == "alice"
    assert summary["users"][0]["display_name"] == "爱丽丝"
    assert summary["users"][0]["total_tokens"] == 180
