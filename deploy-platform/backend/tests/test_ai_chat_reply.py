"""模型把「正在查询」当答案时，必须改用技能结果。

上一轮只测了 finalize 字符串函数，没有把「content + tool_calls → 执行技能 → 第二轮仍在口头禅」
这条真实循环跑起来，所以线上通义照样把口头禅当回复。这里把循环也卡住。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.modules.ai.chat import (
    _audit_context,
    _chat_with_llm,
    finalize_assistant_reply,
    format_tool_reply,
    is_release_intent,
    is_status_intent,
    looks_like_text_confirm_prompt,
    looks_like_wait_reply,
    try_direct_release,
    try_direct_status,
)
from app.modules.ai.sessions import create_session, delete_session, list_sessions
from app.modules.harness.tools import ToolSpec
from app.modules.llm.client import LlmTurn

WAIT_NODES = "正在查询全部部署节点……稍等，我马上为您列出。"
TWO_NODES = {
    "nodes": [
        {
            "id": 1,
            "name": "web-1",
            "host": "10.0.0.1",
            "env": "prod",
            "status": "online",
            "groups": ["prod"],
            "allow_paths": ["D:\\wwwroot"],
        },
        {
            "id": 2,
            "name": "web-2",
            "host": "10.0.0.2",
            "env": "prod",
            "status": "online",
            "groups": ["prod"],
            "allow_paths": ["D:\\wwwroot"],
        },
    ],
    "total": 2,
    "hint": "目标目录必须落在节点的 allow_paths 之内。",
}


def test_wait_text_is_not_a_final_answer() -> None:
    assert looks_like_wait_reply(WAIT_NODES)
    assert not looks_like_wait_reply("共 2 个部署节点：\n- #1 web-1（10.0.0.1）")


def test_audit_context_does_not_keep_message_bodies() -> None:
    blob = "secret-system-prompt" * 200
    summary = _audit_context([{"role": "system", "content": blob}, {"role": "user", "content": "hi"}])
    assert blob not in str(summary)
    assert summary["message_count"] == 2
    assert summary["roles"] == ["system", "user"]


def test_format_nodes_even_when_there_are_only_two() -> None:
    traces = [{"name": "list_push_nodes", "result": TWO_NODES}]
    reply = finalize_assistant_reply(WAIT_NODES, traces)
    assert "#1 web-1" in reply
    assert "#2 web-2" in reply
    assert "正在查询" not in reply
    assert "10.0.0.1" in format_tool_reply(traces)

    vague = finalize_assistant_reply("已经查过了，请看技能调用。", traces)
    assert "#1 web-1" in vague
    assert "已经查过了" not in vague


def test_format_tool_reply_does_not_repeat_the_same_catalog() -> None:
    traces = [
        {"name": "list_pipelines", "result": {"pipelines": [{"id": 1, "name": "a"}], "total": 1}},
        {"name": "list_pipelines", "result": {"pipelines": [{"id": 1, "name": "a"}], "total": 1}},
        {"name": "skill", "result": {"skill_content": "<skill_content name='x'>secret</skill_content>"}},
    ]
    text = format_tool_reply(traces)
    assert text.count("#1 a") == 1
    assert "secret" not in text
    assert "<skill_content" not in text


def test_similar_pipeline_names_are_not_treated_as_listed() -> None:
    """模型编 order-service-tests 不能算已经列出了 order-service-test。"""
    traces = [
        {
            "name": "list_pipelines",
            "result": {
                "pipelines": [
                    {"id": 1, "name": "new-from-test", "project": "demo", "group": "测试", "env": "test"},
                    {"id": 2, "name": "order-service-test", "project": "demo", "group": "测试", "env": "test"},
                    {"id": 3, "name": "pay-service-ci", "project": "demo", "group": "测试", "env": "test"},
                    {"id": 4, "name": "b2c-front-deploy", "project": "demo", "group": "生产", "env": "prod"},
                ],
                "total": 4,
            },
        }
    ]
    fake = (
        "共 9 条流水线：\n"
        "- order-service-tests\n"
        "- order-service-prod\n"
        "- pay-service-tests\n"
        "- b2c-front-tests\n"
    )
    reply = finalize_assistant_reply(fake, traces)
    assert "new-from-test" in reply
    assert "order-service-test" in reply
    assert "pay-service-ci" in reply
    assert "order-service-tests" not in reply


def test_node_keyword_matches_numeric_id() -> None:
    from app.modules.ai.skills.delivery import _node_matches

    node = SimpleNamespace(id=11, name="test-1", host="192.0.2.10")
    assert _node_matches("11", node, [])
    assert _node_matches("192.0.2.10", node, [])
    assert _node_matches("test-1", node, [])
    assert not _node_matches("17", node, [])
    assert not _node_matches("99", node, [])


def test_same_bug_on_pipelines_and_repositories() -> None:
    pipe_traces = [
        {
            "name": "list_pipelines",
            "result": {
                "pipelines": [
                    {"id": 8, "name": "test-C", "project": "demo", "group": "测试", "env": "test"}
                ],
                "total": 1,
            },
        }
    ]
    reply = finalize_assistant_reply("正在查询全部流水线，请稍等。", pipe_traces)
    assert "test-C" in reply
    assert "正在查询" not in reply

    repo_traces = [
        {
            "name": "list_repositories",
            "result": {
                "repositories": [
                    {"id": 3, "name": "order-service", "alias": "order", "url": "git://x"}
                ]
            },
        }
    ]
    reply = finalize_assistant_reply("好的，我来帮你查一下代码库。", repo_traces)
    assert "order-service" in reply
    assert "帮你查" not in reply


def test_empty_nodes_keeps_permission_hint() -> None:
    traces = [
        {
            "name": "list_push_nodes",
            "result": {"nodes": [], "hint": "你还没有任何节点的下发权限。"},
        }
    ]
    reply = finalize_assistant_reply(WAIT_NODES, traces)
    assert "下发权限" in reply
    assert "正在查询" not in reply


def test_deleted_session_removes_chat_history() -> None:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.modules.ai.history import append_message, list_session_messages
    from app.modules.ai.models import AiConversation, AiMessage, AiSession, AiWatch
    from app.modules.ai.sessions import create_session, delete_session, list_sessions

    from sqlalchemy import event

    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):  # noqa: ARG001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=[
            AiConversation.__table__,
            AiMessage.__table__,
            AiWatch.__table__,
            *[
                table
                for name, table in Base.metadata.tables.items()
                if name in {"ai_session", "ai_session_event"}
            ],
        ],
    )
    with Session(engine) as db:
        conv = AiConversation(user_id=7, title="要删的", working_json="{}")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        gone = create_session(db, 7, title="要删的", legacy_conversation_id=conv.id)
        keep = create_session(db, 7, title="留下")
        append_message(db, conv.id, "user", "旧消息不要再出现")
        append_message(db, conv.id, "assistant", "旧回复也不要出现")
        assert list_session_messages(db, gone.id)
        delete_session(db, gone.id)
        titles = [row["title"] for row in list_sessions(db, 7)]
        assert "留下" in titles
        assert "要删的" not in titles
        assert keep.id in {row["id"] for row in list_sessions(db, 7)}
        assert db.get(AiSession, gone.id) is None
        assert db.get(AiConversation, conv.id) is None
        leftover = db.scalars(select(AiMessage).where(AiMessage.conversation_id == conv.id)).all()
        assert leftover == []


def test_cancel_reply_does_not_ask_model_to_rephrase(monkeypatch) -> None:
    """作废申请一旦有回执，就不要再开一轮推理，否则用户会停在「正在推理」。"""
    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn("", "cancel_access_application")],
        {
            "cancel_access_application": {
                "application_id": 7,
                "status": "cancelled",
                "reply": "已取消申请 #7（DMS 整项目）。",
            }
        },
    )
    out = _chat_with_llm(
        MagicMock(),
        "那张申请不要了",
        SimpleNamespace(id=1, username="dev", is_admin=False),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert "已取消申请 #7" in out["reply"]


def _scripted_turn(content: str, tool_name: str | None = None) -> LlmTurn:
    calls = []
    if tool_name:
        calls.append(
            {
                "id": "call-1",
                "name": tool_name,
                "arguments": {},
                "raw": {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                },
            }
        )
    return LlmTurn(content=content, tool_calls=calls, raw_assistant={}, finish_reason="stop")


class _ScriptedClient:
    def __init__(self, turns: list[LlmTurn]):
        self._turns = list(turns)
        self.cfg = SimpleNamespace(context_window=32000, max_tokens=2048, timeout_sec=30)

    def complete(self, messages, tools=None, timeout_sec=None) -> LlmTurn:
        assert self._turns, "模型被多调了一次"
        return self._turns.pop(0)


def _patch_chat_loop(
    monkeypatch,
    turns: list[LlmTurn],
    tool_payloads: dict[str, dict],
    *,
    source: str = "package",
    confirm: bool = False,
) -> None:
    client = _ScriptedClient(turns)
    meta = {
        "id": 1,
        "name": "fake",
        "model_id": "fake",
        "provider_name": "test",
        "supports_vision": False,
    }
    monkeypatch.setattr(
        "app.modules.llm.service.build_client", lambda db, pk=None: (client, meta)
    )
    monkeypatch.setattr("app.modules.ai.prompt.assemble_system", lambda *a, **k: "sys")
    monkeypatch.setattr(
        "app.modules.harness.tools.openai_tools",
        lambda db, **_kw: [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in tool_payloads
        ],
    )

    def fake_resolve(db, name):
        from app.modules.ai.chat import canonicalize_tool_name

        return ToolSpec(
            name=canonicalize_tool_name(name),
            description="",
            parameters={"type": "object", "properties": {}},
            source=source,
            isolation="in-process",
            confirm=confirm,
        )

    monkeypatch.setattr("app.modules.harness.tools.resolve", fake_resolve)
    from app.modules.ai.chat import canonicalize_tool_name

    monkeypatch.setattr(
        "app.modules.ai.chat.execute_tool",
        lambda db, tool, params, current, **kw: tool_payloads[canonicalize_tool_name(tool)],
    )


def test_chat_loop_replaces_wait_text_after_list_nodes(monkeypatch) -> None:
    """通义第一拍口头禅 + 调工具，第二拍还是口头禅：用户必须看到节点列表。"""
    _patch_chat_loop(
        monkeypatch,
        [
            _scripted_turn(WAIT_NODES, "list_push_nodes"),
            _scripted_turn(WAIT_NODES),
        ],
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
    assert "web-2" in out["reply"]
    assert "正在查询" not in out["reply"]
    assert out["traces"][0]["name"] == "list_push_nodes"


def test_chat_loop_keeps_nodes_when_second_round_crashes(monkeypatch) -> None:
    class BoomClient(_ScriptedClient):
        def complete(self, messages, tools=None, timeout_sec=None) -> LlmTurn:
            if len(self._turns) == 1:
                self._turns.pop(0)
                raise RuntimeError("model timeout")
            return super().complete(messages, tools=tools, timeout_sec=timeout_sec)

    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn(WAIT_NODES, "list_push_nodes"), _scripted_turn("should-not-run")],
        {"list_push_nodes": TWO_NODES},
    )
    boom = BoomClient([_scripted_turn(WAIT_NODES, "list_push_nodes"), _scripted_turn("x")])
    meta = {
        "id": 1,
        "name": "fake",
        "model_id": "fake",
        "provider_name": "test",
        "supports_vision": False,
    }
    monkeypatch.setattr(
        "app.modules.llm.service.build_client", lambda db, pk=None: (boom, meta)
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


def test_chat_loop_fills_catalog_when_model_skips_tools(monkeypatch) -> None:
    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn("当前没有可见的项目，因此也无法列出代码库。")],
        {
            "list_repositories": {
                "repositories": [
                    {"id": 3, "name": "order-service", "alias": "order", "url": "git://x"}
                ]
            }
        },
    )
    out = _chat_with_llm(
        MagicMock(),
        "有哪些代码库",
        SimpleNamespace(id=1, username="admin", is_admin=True),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert "order-service" in out["reply"]
    assert "没有可见的项目" not in out["reply"]


def test_catalog_intent_does_not_hijack_file_push() -> None:
    from app.modules.ai.chat import match_catalog_tool

    assert match_catalog_tool("有哪些代码库") == "list_repositories"
    assert match_catalog_tool("查询全部节点") == "list_push_nodes"
    assert match_catalog_tool("把文件传到节点 D:\\wwwroot") is None
    assert match_catalog_tool("发到 192.0.2.6 的 D:\\wwwroot\\testagent") is None


def test_catalog_intent_does_not_hijack_skill_authoring() -> None:
    from app.modules.ai.chat import match_catalog_tool

    assert match_catalog_tool("帮我实现一个agent技能，查询流水线状态") is None
    assert match_catalog_tool("做成一个插件，当我问正在发布的有哪些") is None
    assert match_catalog_tool("查询流水线状态，正在发布的有哪些") == "list_pipelines"


def test_confirm_builtin_skill_returns_action_card(monkeypatch) -> None:
    card = {
        "_action": "confirm_node_push",
        "label": "确认提交下发申请",
        "payload": {"node_ids": [17], "target_dir": "D:\\wwwroot\\testagent", "attachment_ids": [5]},
        "reply": "这是生产节点，确认后先进审批，通过了才真正下发。",
    }
    _patch_chat_loop(
        monkeypatch,
        [
            _scripted_turn("正在准备下发。", "prepare_node_push"),
            _scripted_turn("请点确认。"),
        ],
        {"propose_node_push": card},
        source="builtin",
        confirm=True,
    )
    out = _chat_with_llm(
        MagicMock(),
        "发到 192.0.2.6 的 D:\\wwwroot\\testagent",
        SimpleNamespace(id=1, username="admin", is_admin=True),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert out["actions"]
    assert out["actions"][0]["type"] == "confirm_node_push"
    assert out["actions"][0]["payload"]["node_ids"] == [17]
    assert "审批" in (out.get("reply") or "")
    assert not (out["traces"][0].get("result") or {}).get("error")


def test_release_intent_is_not_catalog_or_node_push() -> None:
    assert is_release_intent("发布 test-C 流水线")
    assert is_release_intent("发布test-C流水线")
    assert is_release_intent("把 test-C 发一下")
    assert is_release_intent("执行AI陪练项目生产环境")
    assert not is_release_intent("有哪些流水线")
    assert not is_release_intent("查询发布状态")
    assert not is_release_intent("现在正在发布的流水线有哪些")
    assert not is_release_intent("查一下test-C的状态")
    assert not is_release_intent("申请 test-C 的执行权限")
    assert not is_release_intent("执行权限")
    assert not is_release_intent("把这个文件发到 192.0.2.6")
    assert not is_release_intent("test-C 发布得怎样了")
    assert not is_release_intent("发布怎么样了")
    assert not is_release_intent("取消这次发布")
    assert not is_release_intent("回滚上一笔")
    assert not is_release_intent("申请一下1项目的执行发布权限")
    assert not is_release_intent("申请一下1项目整个项目的执行发布权限")


def test_execute_publish_permission_phrasing_is_access_apply() -> None:
    """「申请…执行发布权限」是要权，项目叫什么不参与判定。"""
    from app.modules.ai.intent import is_access_apply_intent, is_project_wide_access

    one = "申请一下1项目的执行发布权限"
    whole = "申请一下1项目整个项目的执行发布权限"
    assert is_access_apply_intent(one)
    assert is_access_apply_intent(whole)
    assert not is_project_wide_access(one)
    assert is_project_wide_access(whole)


def test_status_intent_is_not_authoring_or_release() -> None:
    assert is_status_intent("查一下test-C的状态")
    assert is_status_intent("现在正在发布的流水线有哪些")
    assert is_status_intent("test-C 发布得怎样了")
    assert not is_status_intent("实现一个查询流水线状态的agent技能")
    assert not is_status_intent("发布 test-C 流水线")
    assert not is_status_intent("申请 test-C 的执行权限")


def test_named_pipeline_status_skips_skill_loop(monkeypatch) -> None:
    pipeline = SimpleNamespace(id=15, name="test-C", yaml="")
    group = SimpleNamespace(id=1, name="测试", type="test")
    monkeypatch.setattr(
        "app.modules.ai.chat.visible_pipelines",
        lambda db, current: [(pipeline, None, group)],
    )
    calls = []

    def fake_execute(db, name, params, current, **kwargs):
        calls.append((name, params))
        return {
            "releases": [
                {"id": 117, "pipeline": "test-C", "pipeline_id": 15, "status": "success", "version": "1"}
            ]
        }

    monkeypatch.setattr("app.modules.ai.chat.execute_tool", fake_execute)
    out = try_direct_status(MagicMock(), MagicMock(), "查一下test-C的状态")
    assert out is not None
    assert calls == [("get_release_status", {"pipeline_id": 15})]
    assert len(out["traces"]) == 1
    assert out["traces"][0]["name"] == "get_release_status"
    assert "#117" in out["reply"]
    assert "success" in out["reply"]
    assert try_direct_status(MagicMock(), MagicMock(), "实现一个查询流水线状态的技能") is None
    assert try_direct_status(MagicMock(), MagicMock(), "安装一下查询流水线状态") is None
    from app.modules.ai.intent import is_release_intent

    assert not is_release_intent("安装一下发布技能")


def test_finalize_keeps_release_card_when_lists_also_ran() -> None:
    traces = [
        {
            "name": "list_pipelines",
            "result": {
                "pipelines": [
                    {"id": 1, "name": "order-service-test", "project": "a", "group": "测试", "env": "test"},
                    {"id": 15, "name": "test-C", "project": "dms", "group": "测试", "env": "test"},
                ],
                "total": 2,
            },
        }
    ]
    reply = finalize_assistant_reply(
        "已识别 流水线=#15 test-C · 分组=测试。",
        traces,
        actions=[{"type": "confirm_release", "label": "确认发布", "payload": {"pipeline_id": 15}}],
    )
    assert "test-C" in reply
    assert "order-service-test" not in reply
    assert "共 2" not in reply


def test_text_confirm_without_button_is_not_nagged() -> None:
    assert looks_like_text_confirm_prompt("你回复『确认』即提交该技能。")
    text = finalize_assistant_reply(
        "技能卡片已生成，等待你的确认。你回复『确认』即提交该技能。",
        [],
        actions=[],
    )
    assert "请再说一次" not in text
    assert "必须出现黄色" not in text
    assert "没有生成确认按钮" in text


def test_failed_propose_replaces_fake_yellow_button() -> None:
    """起草失败时正文必须是失败原因，不能还让人去点不存在的黄按钮。"""
    traces = [
        {"name": "skill", "result": {"name": "rp-agent-skill", "content": "# 写技能"}},
        {"name": "list_platform_tools", "result": {"tools": []}},
        {
            "name": "propose_agent_skill",
            "result": {"error": "只有管理员可以起草 Agent 技能", "code": "TOOL_EXCEPTION"},
        },
    ]
    fake = (
        "技能草稿已生成，确认卡片如下：\n"
        "请点击上方黄色确认按钮完成安装。"
    )
    text = finalize_assistant_reply(fake, traces, actions=[])
    assert "黄色确认" not in text
    assert "只有管理员可以起草 Agent 技能" in text
    assert "确认按钮" in text


def test_query_failure_does_not_mention_confirm_button() -> None:
    traces = [{"name": "list_notifications", "result": {"error": "收件箱不可用"}}]
    text = finalize_assistant_reply("正在查询未读消息。", traces, actions=[])
    assert "收件箱不可用" in text
    assert "确认按钮" not in text


def test_plugin_draft_findings_use_message_not_dict() -> None:
    from app.modules.ai.chat import format_tool_failures, tool_failures

    traces = [
        {
            "name": "propose_plugin_draft",
            "result": {
                "ok": False,
                "message": "草稿没通过体检",
                "findings": [{"level": "error", "message": "缺少 task.py"}],
            },
        }
    ]
    text = format_tool_failures(tool_failures(traces))
    assert "缺少 task.py" in text
    assert "{'level'" not in text
    assert "确认按钮" in text


def test_failed_propose_stops_without_second_model_round(monkeypatch) -> None:
    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn("正在起草。", "propose_agent_skill")],
        {"propose_agent_skill": {"error": "只有管理员可以起草 Agent 技能"}},
        source="builtin",
        confirm=True,
    )
    out = _chat_with_llm(
        MagicMock(),
        "写一个技能，读取未读消息",
        SimpleNamespace(id=7, username="dev", is_admin=False),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert not out.get("actions")
    assert "只有管理员可以起草 Agent 技能" in (out.get("reply") or "")
    assert "黄色" not in (out.get("reply") or "")


def test_confirm_card_stops_without_second_model_round(monkeypatch) -> None:
    """出确认卡后不要再让模型写「回复确认」，轨迹也必须留下。"""
    card = {
        "_action": "confirm_agent_skill",
        "label": "确认安装该 Agent 技能",
        "payload": {"name": "query-pipeline-status", "skill_md": "# 何时使用\n" + "x" * 8000},
        "reply": "准备把查询流水线状态做成 Agent 技能包。",
    }
    _patch_chat_loop(
        monkeypatch,
        [_scripted_turn("正在起草。", "propose_agent_skill")],
        {"propose_agent_skill": card},
        source="builtin",
        confirm=True,
    )
    out = _chat_with_llm(
        MagicMock(),
        "帮我实现一个 agent 技能，查询流水线状态",
        SimpleNamespace(id=1, username="admin", is_admin=True),
        conversation_id=None,
        session_id=None,
        turn_no=None,
    )
    assert out is not None
    assert out["actions"]
    assert out["actions"][0]["type"] == "confirm_agent_skill"
    assert out["traces"]
    assert out["traces"][0]["name"] == "propose_agent_skill"
    assert "请再说一次" not in (out.get("reply") or "")
    args = out["traces"][0].get("arguments") or {}
    result = out["traces"][0].get("result") or {}
    assert len(json.dumps(args, ensure_ascii=False) + json.dumps(result, ensure_ascii=False)) < 4000
    assert result.get("_action") == "confirm_agent_skill"


def test_slim_traces_drops_skill_md_body() -> None:
    from app.modules.ai.history import slim_traces

    body = "# 何时使用\n" + "正文" * 4000
    slim = slim_traces(
        [
            {
                "id": "c1",
                "name": "propose_agent_skill",
                "arguments": {"name": "query-pipeline-status", "skill_md": body},
                "result": {
                    "_action": "confirm_agent_skill",
                    "label": "确认安装该 Agent 技能",
                    "payload": {"skill_md": body},
                    "reply": "ok",
                },
            }
        ]
    )
    assert slim[0]["name"] == "propose_agent_skill"
    assert slim[0]["result"]["_action"] == "confirm_agent_skill"
    assert body not in json.dumps(slim, ensure_ascii=False)
    assert "skill_md" in (slim[0].get("arguments") or {})


def test_slim_traces_keeps_propose_failure_reason() -> None:
    from app.modules.ai.history import slim_traces

    body = "# 何时使用\n" + "正文" * 400
    slim = slim_traces(
        [
            {
                "id": "c1",
                "name": "propose_agent_skill",
                "arguments": {"name": "unread-notice", "skill_md": body},
                "result": {
                    "ok": False,
                    "message": "技能包没有写合格",
                    "findings": [
                        {"level": "error", "code": "invented_tool", "message": "不要发明工具名：notice_id"}
                    ],
                },
            }
        ]
    )
    result = slim[0]["result"]
    assert result["ok"] is False
    assert result["message"] == "技能包没有写合格"
    assert result["findings"][0]["message"] == "不要发明工具名：notice_id"
    dumped = json.dumps(slim, ensure_ascii=False)
    assert body not in dumped


def test_slim_traces_keeps_notification_titles() -> None:
    from app.modules.ai.history import slim_traces

    slim = slim_traces(
        [
            {
                "name": "list_notifications",
                "result": {
                    "notifications": [
                        {
                            "id": 271,
                            "notice_id": 271,
                            "title": "应急工单",
                            "content": "正文" * 2000,
                            "detail_url": "/notifications?id=271",
                        }
                    ],
                    "unread_count": 1,
                },
            }
        ]
    )
    result = slim[0]["result"]
    assert isinstance(result, dict)
    assert result["notifications"][0]["title"] == "应急工单"
    assert "content" not in result["notifications"][0]
    assert "正文" * 20 not in json.dumps(slim, ensure_ascii=False)


def test_slim_traces_keeps_access_application_scope() -> None:
    """完整轨迹要看得出是整项目申请，不能只剩 pipeline 占位文案。"""
    from app.modules.ai.history import slim_traces

    slim = slim_traces(
        [
            {
                "name": "list_my_access_applications",
                "result": {
                    "applications": [
                        {
                            "id": 12,
                            "scope": "DMS 经销商协同运营平台 整项目",
                            "pipeline": "（项目下全部流水线）",
                            "apply_type": "project_execute",
                            "status": "rejected",
                            "reason": "助手代为申请该项目全部流水线权限",
                        }
                    ],
                    "reply": "你的权限申请：\n- #12 DMS 整项目  rejected",
                },
            }
        ]
    )
    row = slim[0]["result"]["applications"][0]
    assert row["id"] == 12
    assert row["status"] == "rejected"
    assert row["scope"] == "DMS 经销商协同运营平台 整项目"
    assert row["apply_type"] == "project_execute"
    assert "reason" not in row


def test_history_tells_model_to_use_button_not_typed_confirm() -> None:
    from app.modules.ai.history import _llm_history_from_rows

    out = _llm_history_from_rows(
        [
            {
                "role": "assistant",
                "content": "准备安装技能",
                "actions": [{"type": "confirm_agent_skill", "label": "确认安装该 Agent 技能"}],
                "traces": [],
            }
        ],
        drop_trailing_user=False,
    )
    text = out[-1]["content"]
    assert "按钮" in text
    assert "再说确认即执行" not in text


def _patch_release_catalog(monkeypatch, rows, projects=None) -> None:
    """解析器读的是 context.visible_pipelines，测试要补在源头。"""
    monkeypatch.setattr("app.modules.ai.context.visible_pipelines", lambda db, current: rows)
    if projects is not None:
        monkeypatch.setattr("app.modules.ai.context.listed_business_projects", lambda db: projects)


def test_prepare_release_arguments_moves_utterance_out_of_id() -> None:
    from app.modules.ai.chat import _prepare_release_arguments

    out = _prepare_release_arguments(
        "执行AI陪练项目生产环境",
        {"pipeline_id": "AI陪练项目生产环境"},
    )
    assert "pipeline_id" not in out
    assert out["pipeline"] == "AI陪练项目生产环境"
    assert out["query"] == "执行AI陪练项目生产环境"


def test_parse_inline_action_uses_user_query_not_prose_id(monkeypatch) -> None:
    """模型在正文里写 JSON 假 id 时，必须带上用户原话交给解析器。"""
    from app.modules.ai.chat import _parse_inline_action

    calls = []

    def fake_execute(db, name, params, current, **kwargs):
        calls.append((name, params))
        return {
            "_action": "confirm_release",
            "label": "确认发布",
            "payload": {"pipeline_id": 21},
            "reply": "已识别 流水线=#21",
        }

    monkeypatch.setattr("app.modules.ai.chat.execute_tool", fake_execute)
    out = _parse_inline_action(
        '请确认 {"action":"propose_release","pipeline_id":12}',
        MagicMock(),
        MagicMock(),
        user_message="执行AI陪练项目生产环境",
    )
    assert out is not None
    assert calls[0][0] == "propose_release"
    assert calls[0][1]["pipeline_id"] == 12
    assert calls[0][1]["query"] == "执行AI陪练项目生产环境"
    assert out["actions"][0]["payload"]["pipeline_id"] == 21


def test_named_pipeline_release_skips_listing(monkeypatch) -> None:
    pipeline = SimpleNamespace(id=15, name="test-C", yaml="")
    group = SimpleNamespace(id=1, name="测试", type="test")
    _patch_release_catalog(monkeypatch, [(pipeline, None, group)])
    monkeypatch.setattr("app.modules.ai.chat.execute_tool", _fake_propose)
    out = try_direct_release(MagicMock(), MagicMock(), "发布 test-C 流水线")
    assert out is not None
    assert out["actions"][0]["type"] == "confirm_release"
    assert out["actions"][0]["payload"]["pipeline_id"] == 15


def _fake_propose(db, name, params, current, **kwargs):
    """测试里把 propose_release 收成确认卡，不碰真实权限。"""
    return {
        "_action": "confirm_release",
        "label": "确认发布",
        "payload": {"pipeline_id": params["pipeline_id"]},
        "reply": f"已识别 流水线=#{params['pipeline_id']}",
    }


def test_project_env_release_skips_listing(monkeypatch) -> None:
    """能唯一落到项目+环境时仍走直达，省一次模型往返。"""
    from app.modules.ai.context import project_keyword

    assert project_keyword("执行AI陪练项目生产环境") == "AI陪练"
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    pipeline = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9)
    group = SimpleNamespace(id=3, name="生产", type="prod")
    _patch_release_catalog(monkeypatch, [(pipeline, proj, group)], [proj])
    monkeypatch.setattr("app.modules.ai.chat.execute_tool", _fake_propose)
    out = try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境")
    assert out is not None
    assert out["actions"][0]["payload"]["pipeline_id"] == 21
    assert len(out["traces"]) == 1
    assert out["traces"][0]["name"] == "propose_release"


def test_project_env_release_ignores_unrelated_visible_pipelines(monkeypatch) -> None:
    """可见目录里的无关项目不能靠弱词抢走，也不该出现在回复里。"""
    dms = SimpleNamespace(id=1, name="DMS 经销商协同运营平台", code="COP")
    coach = SimpleNamespace(id=9, name="AI陪练", code="coach")
    dms_a = SimpleNamespace(id=108, name="ai-cop-web", yaml="", project_id=1)
    dms_b = SimpleNamespace(id=109, name="ai-cop-api", yaml="", project_id=1)
    coach_prod = SimpleNamespace(id=21, name="coach-prod", yaml="", project_id=9)
    prod = SimpleNamespace(id=3, name="生产", type="prod")
    _patch_release_catalog(
        monkeypatch,
        [(dms_a, dms, prod), (dms_b, dms, prod), (coach_prod, coach, prod)],
        [dms, coach],
    )
    monkeypatch.setattr("app.modules.ai.chat.execute_tool", _fake_propose)
    out = try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境")
    assert out["actions"][0]["payload"]["pipeline_id"] == 21
    assert "108" not in out["reply"]
    assert "DMS" not in out["reply"]


def test_project_env_release_lists_when_multiple(monkeypatch) -> None:
    proj = SimpleNamespace(id=9, name="AI陪练", code="coach")
    a = SimpleNamespace(id=21, name="coach-web", yaml="", project_id=9)
    b = SimpleNamespace(id=22, name="coach-api", yaml="", project_id=9)
    prod = SimpleNamespace(id=3, name="生产", type="prod")
    _patch_release_catalog(monkeypatch, [(a, proj, prod), (b, proj, prod)], [proj])
    out = try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境")
    assert out["actions"] == []
    assert out["traces"] == []
    assert "#21" in out["reply"]
    assert "#22" in out["reply"]


def test_project_env_release_does_not_fall_through_to_model(monkeypatch) -> None:
    """项目已经钉死但看不见线：这是事实，不必问模型。"""
    coach = SimpleNamespace(id=9, name="AI陪练", code="coach")
    _patch_release_catalog(monkeypatch, [], [coach])
    out = try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境")
    assert out is not None
    assert out["actions"] == []
    assert "看不到" in out["reply"]
    assert "108" not in out["reply"]


def test_unresolved_release_falls_through_to_model(monkeypatch) -> None:
    """对不上任何项目时不要写死「找不到」，把槽位交给模型抽。"""
    _patch_release_catalog(monkeypatch, [], [])
    assert try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境") is None
    assert try_direct_release(MagicMock(), MagicMock(), "帮我把陪练的生产跑起来") is None


def test_project_env_release_hidden_pipelines_are_not_other_projects(monkeypatch) -> None:
    dms = SimpleNamespace(id=1, name="DMS 经销商协同运营平台", code="COP")
    coach = SimpleNamespace(id=9, name="AI陪练", code="coach")
    dms_pipe = SimpleNamespace(id=1, name="cop-prod", yaml="", project_id=1)
    prod = SimpleNamespace(id=3, name="生产", type="prod")
    _patch_release_catalog(monkeypatch, [(dms_pipe, dms, prod)], [dms, coach])
    out = try_direct_release(MagicMock(), MagicMock(), "执行AI陪练项目生产环境")
    assert out["actions"] == []
    assert "看不到" in out["reply"]
    assert "AI陪练" in out["reply"]
    assert "DMS" not in out["reply"]


def test_project_wide_access_is_not_a_catalog_list() -> None:
    from app.modules.ai.chat import match_catalog_tool
    from app.modules.ai.intent import is_project_wide_access, parse_project_id
    from app.modules.ai.tool_select import names_for_turn

    text = "我要1项目下的所有流水线执行权限"
    assert parse_project_id(text) == 1
    assert parse_project_id("申请项目#3全部流水线执行权") == 3
    assert is_project_wide_access(text)
    assert match_catalog_tool(text) is None
    picked = names_for_turn(text, categories=frozenset({"access", "catalog"}))
    assert picked is None or "apply_project_execute" in picked


def test_skip_tools_does_not_lock_route_by_utterance() -> None:
    """有模型时不按话术拦工具：没跑过申请就不能因为句子像发布而禁止 apply_*。"""
    from app.modules.ai.chat import _skip_access_extra_tool, _skip_release_extra_tool

    ask = "申请一下1项目的执行发布权限"
    assert _skip_access_extra_tool(ask, "apply_project_execute", []) == ""
    assert _skip_release_extra_tool(ask, "apply_project_execute", []) == ""
    traces = [{"name": "apply_project_execute", "result": {"application_id": 1}}]
    assert _skip_access_extra_tool(ask, "apply_project_execute", traces)
    assert _skip_access_extra_tool(ask, "list_pipelines", traces)


def test_format_tool_reply_keeps_apply_result_not_pipeline_dump() -> None:
    traces = [
        {
            "name": "list_pipelines",
            "result": {
                "pipelines": [
                    {"id": 1, "name": "a", "project": "dms", "group": "生产", "env": "prod"},
                    {"id": 2, "name": "b", "project": "dms", "group": "测试", "env": "test"},
                ],
                "total": 2,
            },
        },
        {
            "name": "apply_pipeline_execute",
            "result": {"error": "你已有该流水线执行权限，无需再申请"},
        },
        {"name": "apply_pipeline_execute", "result": {"error": "流水线 不存在"}},
    ]
    reply = finalize_assistant_reply(
        "共 12 个流水线：\n- #1 a\n- #2 b\n你已有该流水线执行权限，无需再申请\n流水线 不存在",
        traces,
    )
    assert "无需再申请" in reply
    assert "流水线 不存在" not in reply
    assert "共 2 个流水线" not in reply
    assert reply == "你已有该流水线执行权限，无需再申请"


def test_try_direct_access_submits_one_project_application(monkeypatch) -> None:
    from app.modules.ai.chat import try_direct_access

    monkeypatch.setattr(
        "app.modules.access.service.catalog_for_apply",
        lambda db, current, keyword="": {
            "projects": [
                {
                    "id": 1,
                    "name": "DMS 经销商协同运营平台",
                    "code": "dms",
                    "pipeline_count": 12,
                    "already_execute": False,
                    "groups": [],
                }
            ],
        },
    )
    calls = []

    def fake_execute(db, name, params, current, **kwargs):
        calls.append((name, params))
        return {
            "application_id": 88,
            "reply": "已提交一张项目级执行权申请 #88：DMS 经销商协同运营平台 下全部流水线。",
        }

    monkeypatch.setattr("app.modules.ai.chat.execute_tool", fake_execute)
    current = MagicMock()
    current.is_admin = False
    out = try_direct_access(MagicMock(), current, "我要1项目下的所有流水线执行权限")
    assert out is not None
    assert calls == [
        ("apply_project_execute", {"project_id": 1, "reason": "助手代为申请该项目全部流水线权限"})
    ]
    assert len(out["traces"]) == 1
    assert out["traces"][0]["name"] == "apply_project_execute"
    assert "一张项目级" in out["reply"]
    assert "list_pipelines" not in out["reply"]
    assert "apply_group_execute" not in out["reply"]


def test_try_direct_access_accepts_execute_publish_permission_wording(monkeypatch) -> None:
    """句式是「执行发布权限」时走申请直达，不进发布解析器。项目用编号，不绑业务名。"""
    from app.modules.ai.chat import try_direct_access, try_direct_release

    monkeypatch.setattr(
        "app.modules.access.service.catalog_for_apply",
        lambda db, current, keyword="": {
            "projects": [
                {
                    "id": 1,
                    "name": "示例项目",
                    "code": "demo",
                    "pipeline_count": 8,
                    "already_execute": False,
                    "groups": [{"id": 3, "name": "测试", "env": "test", "already_execute": False}],
                }
            ],
        },
    )
    monkeypatch.setattr("app.modules.access.service.catalog", lambda db: {"pipelines": []})
    current = MagicMock()
    current.is_admin = False
    ask_scope = "申请一下1项目的执行发布权限"
    assert try_direct_release(MagicMock(), current, ask_scope) is None
    scoped = try_direct_access(MagicMock(), current, ask_scope)
    assert scoped is not None
    assert "看不到" not in scoped["reply"]
    assert scoped["traces"][0]["name"] == "list_access_catalog"

    calls = []

    def fake_execute(db, name, params, current, **kwargs):
        calls.append((name, params))
        return {
            "application_id": 90,
            "reply": "已提交一张项目级执行权申请 #90：示例项目 下全部流水线（含生产）。",
        }

    monkeypatch.setattr("app.modules.ai.chat.execute_tool", fake_execute)
    whole = "申请一下1项目整个项目的执行发布权限"
    assert try_direct_release(MagicMock(), current, whole) is None
    out = try_direct_access(MagicMock(), current, whole)
    assert out is not None
    assert calls == [
        ("apply_project_execute", {"project_id": 1, "reason": "助手代为申请该项目全部流水线权限"})
    ]
    assert "看不到" not in out["reply"]
    assert "apply_group_execute" not in out["reply"]


def test_try_direct_access_submits_one_group_application(monkeypatch) -> None:
    from app.modules.ai.chat import try_direct_access

    monkeypatch.setattr(
        "app.modules.access.service.catalog_for_apply",
        lambda db, current, keyword="": {
            "projects": [
                {
                    "id": 1,
                    "name": "DMS 经销商协同运营平台",
                    "code": "dms",
                    "pipeline_count": 12,
                    "already_execute": False,
                    "groups": [{"id": 2, "name": "测试", "env": "test", "already_execute": False}],
                }
            ],
        },
    )
    calls = []

    def fake_execute(db, name, params, current, **kwargs):
        calls.append((name, params))
        return {
            "application_id": 89,
            "reply": "已提交一张环境执行权申请 #89：DMS 经销商协同运营平台 / 测试。",
        }

    monkeypatch.setattr("app.modules.ai.chat.execute_tool", fake_execute)
    current = MagicMock()
    current.is_admin = False
    out = try_direct_access(MagicMock(), current, "我要1项目测试环境所有流水线执行权限")
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
    assert out["traces"][0]["name"] == "apply_group_execute"
    assert "环境执行权" in out["reply"]


def test_admin_does_not_submit_access_application(monkeypatch) -> None:
    from app.modules.ai.chat import try_direct_access

    monkeypatch.setattr(
        "app.modules.access.service.catalog_for_apply",
        lambda db, current, keyword="": {"projects": [{"id": 1, "name": "DMS", "code": "dms", "groups": []}]},
    )
    monkeypatch.setattr(
        "app.modules.ai.chat.execute_tool",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("admin must not apply")),
    )
    current = MagicMock()
    current.is_admin = True
    out = try_direct_access(MagicMock(), current, "我要1项目下的所有流水线执行权限")
    assert out is not None
    assert "不用申请" in out["reply"]
    assert not out["traces"]
