"""AI 会话事件日志的回归：追加只增不改、崩溃可恢复、分支和重放不污染父会话。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.agent.log_store import MemoryLogStore, NullLogStore
from app.modules.ai.audit_store import reset_ai_audit_store, set_ai_audit_store_for_tests
from app.modules.ai.history import backfill_legacy, list_session_messages
from app.modules.ai.models import AiConversation, AiMessage, AiSessionEvent
from app.modules.ai.sessions import (
    append_event,
    create_session,
    fork_session,
    inspect_session,
    project_events,
    read_events,
    replay_session,
    resume_session,
    verify_chain,
)


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            AiConversation.__table__,
            AiMessage.__table__,
            *[
                table
                for name, table in Base.metadata.tables.items()
                if name in {"ai_session", "ai_session_event"}
            ],
        ],
    )
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _ai_logs_null() -> None:
    set_ai_audit_store_for_tests(NullLogStore())
    yield
    set_ai_audit_store_for_tests(None)
    reset_ai_audit_store()


def test_append_only_chain_is_continuous_and_verifiable(db: Session) -> None:
    session = create_session(db, 7, title="验证")
    for index in range(12):
        append_event(
            db,
            session.id,
            "user/message",
            {"content": str(index)},
            turn=1,
            model_visible=True,
            surface="transcript",
        )
    events = read_events(db, session.id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert verify_chain(db, session.id)["ok"]


def test_chain_still_verifies_after_mysql_datetime_truncation(db: Session) -> None:
    """MySQL DATETIME 没有微秒。哈希如果带时间，读出来就会全站 inspect 失败。"""
    from datetime import datetime, timezone

    session = create_session(db, 7, title="截断")
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "x"},
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    event = read_events(db, session.id)[-1]
    event.occurred_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    db.commit()
    assert verify_chain(db, session.id)["ok"]


def test_append_rejects_unknown_type_and_illegal_visibility(db: Session) -> None:
    session = create_session(db, 7)
    with pytest.raises(ValueError):
        append_event(db, session.id, "tool/whatever", {})
    with pytest.raises(ValueError):
        # 只进审计的事件不能标成模型可见，否则提示词里会混进不该给模型看的内容
        append_event(db, session.id, "user/message", {"content": "x"}, model_visible=True, surface="audit")


def test_legacy_backfill_is_idempotent(db: Session) -> None:
    legacy = AiConversation(user_id=7, title="旧会话", working_json="{}")
    db.add(legacy)
    db.commit()
    db.refresh(legacy)
    db.add_all(
        [
            AiMessage(conversation_id=legacy.id, role="user", content="旧问题"),
            AiMessage(conversation_id=legacy.id, role="assistant", content="旧回答"),
        ]
    )
    db.commit()

    session = create_session(db, 7, legacy_conversation_id=legacy.id)
    assert backfill_legacy(db, legacy.id, session.id) == 2
    count = len(read_events(db, session.id))

    # 重启后再跑一次不能把旧消息又灌一遍
    assert backfill_legacy(db, legacy.id, session.id) == 0
    assert len(read_events(db, session.id)) == count
    contents = [m["content"] for m in project_events(read_events(db, session.id))["messages"]]
    assert contents == ["旧问题", "旧回答"]


def test_cold_recovery_closes_unresolved_tool_calls(db: Session) -> None:
    session = create_session(db, 7, title="中断")
    append_event(db, session.id, "turn/start", {}, turn=1, surface="audit")
    append_event(
        db,
        session.id,
        "tool/call",
        {"name": "probe", "arguments": {}},
        turn=1,
        step=1,
        call="call-1",
        model_visible=True,
        surface="model_context",
    )

    assert resume_session(db, session.id)["recovered"]
    events = read_events(db, session.id)
    assert events[-1].type == "turn/end"
    assert any(event.type == "tool/result" for event in events)
    assert verify_chain(db, session.id)["ok"]

    # 没有未收尾的轮次时不该再追加任何事件
    count = len(events)
    assert resume_session(db, session.id)["recovered"] is False
    assert len(read_events(db, session.id)) == count


def test_live_turn_survives_reload_until_turn_ends(db: Session) -> None:
    """刷新后应能从会话事件重挂思考态，而不是 Redis 连接标记。"""
    from app.modules.ai.sessions import live_turn_of

    session = create_session(db, 7, title="重连")
    assert live_turn_of(db, session.id) is None
    append_event(db, session.id, "turn/start", {"source": "chat"}, turn=2, surface="audit")
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "查一下我的未读消息"},
        turn=2,
        model_visible=True,
        surface="transcript",
    )
    live = live_turn_of(db, session.id)
    assert live is not None
    assert live["running"] is True
    assert live["turn"] == 2
    assert "思考" in live["status"]

    append_event(
        db,
        session.id,
        "turn/status",
        {"text": "正在调用 list_notifications…"},
        turn=2,
        surface="audit",
    )
    live = live_turn_of(db, session.id)
    assert live is not None
    assert live["status"] == "正在调用 list_notifications…"

    append_event(
        db,
        session.id,
        "tool/call",
        {"name": "list_notifications", "arguments": {}},
        turn=2,
        call="c1",
        model_visible=True,
        surface="model_context",
    )
    live = live_turn_of(db, session.id)
    assert live is not None
    assert "list_notifications" in live["status"]

    append_event(
        db,
        session.id,
        "assistant/message",
        {"content": "当前没有未读消息。"},
        turn=2,
        model_visible=True,
        surface="transcript",
    )
    append_event(db, session.id, "turn/end", {"status": "completed"}, turn=2, surface="audit")
    assert live_turn_of(db, session.id) is None


def test_fork_and_replay_never_touch_the_parent(db: Session) -> None:
    session = create_session(db, 7, title="验证")
    for index in range(12):
        append_event(
            db,
            session.id,
            "user/message",
            {"content": str(index)},
            turn=1,
            model_visible=True,
            surface="transcript",
        )
    parent_count = db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )

    fork = fork_session(db, session.id, 7, at_seq=5)
    before = db.scalar(select(func.count()).select_from(AiSessionEvent))
    replay = replay_session(db, fork.id)
    after = db.scalar(select(func.count()).select_from(AiSessionEvent))

    assert replay["read_only"] is True
    assert before == after
    assert parent_count == db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )
    assert fork.parent_session_id == session.id and fork.forked_from_seq == 5


def test_list_session_messages_ignores_huge_tool_payloads(db: Session) -> None:
    session = create_session(db, 7, title="列表")
    blob = "x" * 20000
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "查流水线"},
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    append_event(
        db,
        session.id,
        "tool/result",
        {"result": {"pipelines": [blob]}},
        turn=1,
        call="c1",
    )
    append_event(
        db,
        session.id,
        "assistant/message",
        {
            "content": "有这些",
            "traces": [{"name": "list_pipelines", "result": {"pipelines": [blob]}}],
        },
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    rows = list_session_messages(db, session.id)
    assert [item["content"] for item in rows] == ["查流水线", "有这些"]
    dumped = json.dumps(rows, ensure_ascii=False)
    assert blob not in dumped
    assert rows[-1]["traces"][0]["name"] == "list_pipelines"


def test_list_session_messages_starts_after_clear(db: Session) -> None:
    session = create_session(db, 7, title="清空后")
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "旧问题"},
        model_visible=True,
        surface="transcript",
    )
    append_event(db, session.id, "session/cleared", {"reason": "user"}, surface="audit")
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "新问题"},
        model_visible=True,
        surface="transcript",
    )
    rows = list_session_messages(db, session.id)
    assert [item["content"] for item in rows] == ["新问题"]


def test_failed_assistant_reply_survives_reload(db: Session) -> None:
    """技能起草失败必须能从会话投影再读出来，切页回来气泡才在。"""
    session = create_session(db, 7, title="失败气泡")
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "写下未读通知技能"},
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    append_event(
        db,
        session.id,
        "assistant/message",
        {
            "content": "技能包没有写合格",
            "kind": "chat",
            "traces": [
                {
                    "name": "propose_agent_skill",
                    "arguments": {"name": "unread-notice"},
                    "result": {
                        "ok": False,
                        "message": "不要发明工具名：notice_id",
                        "findings": [{"level": "error", "message": "不要发明工具名：notice_id"}],
                    },
                }
            ],
        },
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    rows = list_session_messages(db, session.id)
    assert [item["role"] for item in rows] == ["user", "assistant"]
    assert "没有写合格" in rows[-1]["content"]
    result = rows[-1]["traces"][0]["result"]
    assert result["ok"] is False
    assert "notice_id" in json.dumps(result, ensure_ascii=False)


def test_stream_error_is_visible_in_transcript(db: Session) -> None:
    """SSE 失败只写 AiMessage 时切页会丢气泡，必须进 assistant/message。"""
    from app.modules.ai.history import persist_assistant_error

    conv = AiConversation(user_id=7, title="流失败", working_json="{}")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    session = create_session(db, 7, title="流失败", legacy_conversation_id=conv.id)
    append_event(
        db,
        session.id,
        "user/message",
        {"content": "失败了，重新写下"},
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    persist_assistant_error(db, "助手处理失败", conversation_id=conv.id)
    rows = list_session_messages(db, session.id)
    assert [item["role"] for item in rows] == ["user", "assistant"]
    assert rows[-1]["kind"] == "error"
    assert rows[-1]["content"] == "助手处理失败"


def test_message_id_stays_event_seq_after_legacy_backfill(db: Session) -> None:
    """轮询用 after_id。id 若从 seq 变成 legacy 主键，同一句会被再拉进来变成两个气泡。"""
    session = create_session(db, 7, title="气泡")
    first = append_event(
        db,
        session.id,
        "user/message",
        {"content": "实现一个技能"},
        model_visible=True,
        surface="transcript",
    )
    before = list_session_messages(db, session.id)
    assert before[0]["id"] == first.seq
    append_event(
        db,
        session.id,
        "legacy/backfill",
        {"legacy_message_id": 9001, "source_event_seq": first.seq, "already_projected": True},
        surface="audit",
    )
    after = list_session_messages(db, session.id)
    assert after[0]["id"] == first.seq
    assert after[0]["content"] == "实现一个技能"
    assert list_session_messages(db, session.id, after_id=first.seq) == []


def test_audit_logs_never_enter_mysql(db: Session) -> None:
    captured = MemoryLogStore()
    set_ai_audit_store_for_tests(captured)
    session = create_session(db, 7, title="日志隔离")
    before = db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )
    append_event(db, session.id, "request/header", {"model": "x"}, turn=1, surface="audit")
    append_event(
        db,
        session.id,
        "request/context",
        {"messages": [{"role": "user", "content": "huge"}]},
        turn=1,
        surface="audit",
    )
    append_event(db, session.id, "step/start", {}, turn=1, step=1, surface="audit")
    append_event(db, session.id, "error", {"phase": "model"}, turn=1, surface="audit")
    after = db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )
    assert after == before
    assert verify_chain(db, session.id)["ok"]
    lines = captured.get(session.id)
    assert len(lines) == 4
    types = [json.loads(line)["type"] for line in lines]
    assert types == ["request/header", "request/context", "step/start", "error"]
    detail = inspect_session(db, session.id)
    assert [row["type"] for row in detail["logs"]] == types
    assert all(event["type"] != "request/header" for event in detail["events"])


def test_request_context_stays_out_of_mysql_even_if_marked_model_context(db: Session) -> None:
    """整包 messages 曾经误标成 model_context，按类型仍必须进 ES。"""
    captured = MemoryLogStore()
    set_ai_audit_store_for_tests(captured)
    session = create_session(db, 7, title="大包")
    before = db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )
    append_event(
        db,
        session.id,
        "request/context",
        {"messages": [{"role": "system", "content": "x" * 1000}]},
        model_visible=True,
        surface="model_context",
    )
    after = db.scalar(
        select(func.count()).select_from(AiSessionEvent).where(AiSessionEvent.session_id == session.id)
    )
    assert after == before
    assert json.loads(captured.get(session.id)[0])["type"] == "request/context"


def test_es_down_does_not_raise_or_write_mysql() -> None:
    from app.modules.ai.audit_store import create_ai_audit_store

    store = create_ai_audit_store(
        lambda: {"es_hosts": "http://127.0.0.1:1", "es_index": "rp-exec-logs"}
    )
    assert "memory" not in store.name().lower()
    assert "mysql" not in store.name().lower()
    store.append_batch(9, ["must-not-raise"])
    assert store.get(9) == [] or store.name() == "none"


def test_create_ai_audit_store_ignores_build_log_index() -> None:
    from app.modules.ai.audit_store import AI_LOG_INDEX, create_ai_audit_store

    store = create_ai_audit_store(
        lambda: {"es_hosts": "http://127.0.0.1:1", "es_index": "rp-exec-logs"}
    )
    prefix = getattr(store, "_prefix", "")
    if prefix:
        assert prefix == AI_LOG_INDEX
        assert "build" not in prefix


def test_huge_audit_payload_is_clipped(db: Session) -> None:
    captured = MemoryLogStore()
    set_ai_audit_store_for_tests(captured)
    session = create_session(db, 7, title="截断")
    append_event(
        db,
        session.id,
        "request/context",
        {"messages": [{"role": "system", "content": "x" * 20000}]},
        turn=1,
        surface="audit",
    )
    row = json.loads(captured.get(session.id)[0])
    assert row["payload"].get("_truncated") is True
    assert len(json.dumps(row, ensure_ascii=False)) < 20000


def test_fork_strips_parent_confirm_tokens(db: Session) -> None:
    """分支里看到的父确认卡不能带着原 token，点下去会绑错 conversation。"""
    session = create_session(db, 7, title="父会话")
    append_event(
        db,
        session.id,
        "assistant/message",
        {
            "content": "请确认发布",
            "actions": [
                {
                    "type": "confirm_release",
                    "label": "确认发布",
                    "payload": {"pipeline_id": 15},
                    "token": "v1.should-not-inherit",
                }
            ],
        },
        turn=1,
        model_visible=True,
        surface="transcript",
    )
    db.refresh(session)
    fork = fork_session(db, session.id, 7)
    parent_rows = list_session_messages(db, session.id)
    fork_rows = list_session_messages(db, fork.id)
    assert parent_rows[-1]["actions"][0]["token"] == "v1.should-not-inherit"
    assert fork_rows[-1]["actions"][0]["token"] == ""
    assert fork_rows[-1]["actions"][0]["payload"]["pipeline_id"] == 15

