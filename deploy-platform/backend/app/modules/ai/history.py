"""旧消息 API 的事件投影兼容层，并负责 legacy 惰性回填。"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.ai.budget import (
    HISTORY_TEXT_MAX_TOKENS,
    HISTORY_TOOL_RESULT_MAX_TOKENS,
    clip_to_tokens,
    tool_result_for_model,
)
from app.modules.ai.chat import looks_like_wait_reply
from app.modules.ai.models import AiConversation, AiMessage, AiSession
from app.modules.ai.sessions import (
    append_event,
    create_session,
    effective_events,
    project_events,
    read_events,
    transcript_events,
)


def get_or_create_conversation(db: Session, user_id: int) -> AiConversation:
    """取这个人当前还在用的会话，不要永远回到最早那一条。

    以前按 conversation.id 升序，删了侧栏会话再进页面，仍会把最老那份
    聊天记录（含已删会话）整表拖回来，看起来像记录删不掉、还会串到新会话。
    """
    open_session = db.scalar(
        select(AiSession)
        .where(AiSession.user_id == user_id, AiSession.status != "deleted")
        .order_by(AiSession.updated_at.desc(), AiSession.id.desc())
    )
    if open_session and open_session.legacy_conversation_id:
        row = db.get(AiConversation, open_session.legacy_conversation_id)
        if row is not None:
            return row
    row = db.scalar(
        select(AiConversation).where(AiConversation.user_id == user_id).order_by(AiConversation.id.desc())
    )
    if row is None:
        row = AiConversation(user_id=user_id, title="默认会话", working_json="{}")
        db.add(row)
        db.commit()
        db.refresh(row)
    ensure_session(db, row)
    return row


def ensure_session(db: Session, conversation: AiConversation) -> AiSession:
    row = db.scalar(
        select(AiSession).where(
            AiSession.legacy_conversation_id == conversation.id,
            AiSession.status != "deleted",
        )
    )
    if row is None:
        try:
            row = create_session(
                db,
                conversation.user_id,
                title=conversation.title,
                legacy_conversation_id=conversation.id,
            )
        except IntegrityError:
            db.rollback()
            occupied = db.scalar(
                select(AiSession).where(AiSession.legacy_conversation_id == conversation.id)
            )
            if occupied is not None and occupied.status == "deleted":
                from app.modules.ai.models import AiMessage, AiSessionEvent

                db.query(AiSessionEvent).filter(AiSessionEvent.session_id == occupied.id).delete(
                    synchronize_session=False
                )
                db.query(AiMessage).filter(AiMessage.conversation_id == conversation.id).delete(
                    synchronize_session=False
                )
                db.delete(occupied)
                db.commit()
                return ensure_session(db, conversation)
            row = occupied
            if row is None:
                raise
    backfill_legacy(db, conversation.id, row.id)
    return row


def session_for_conversation(db: Session, conversation_id: int) -> AiSession:
    conversation = db.get(AiConversation, conversation_id)
    if conversation is None:
        raise LookupError("旧会话不存在")
    return ensure_session(db, conversation)


def backfill_legacy(db: Session, conversation_id: int, session_id: int) -> int:
    """按旧消息 ID 幂等回填，不删改旧表。

    只在这个会话还没有任何对话事件时跑。事件超过 5000 条之后如果每次 GET
    都再扫一遍再 CAS 写入，页面会卡几分钟，同一句话还会在投影里出现多次。
    """
    from app.modules.ai.models import AiSessionEvent

    already = db.scalar(
        select(AiSessionEvent.id).where(
            AiSessionEvent.session_id == session_id,
            AiSessionEvent.type.in_(("legacy/backfill", "user/message", "assistant/message")),
        ).limit(1)
    )
    if already:
        return 0
    existing = {
        int(e.get("legacy_message_id"))
        for event in read_events(db, session_id, limit=5000)
        if event.type in {"legacy/backfill", "user/message", "assistant/message"}
        for e in [json.loads(event.payload_json or "{}")]
        if e.get("legacy_message_id")
    }
    rows = db.scalars(
        select(AiMessage)
        .where(AiMessage.conversation_id == conversation_id)
        .order_by(AiMessage.id)
    ).all()
    count = 0
    for message in rows:
        if message.id in existing:
            continue
        data = message_public(message)
        append_event(
            db,
            session_id,
            "legacy/backfill",
            {"legacy_message_id": message.id},
            surface="audit",
        )
        append_event(
            db,
            session_id,
            "user/message" if message.role == "user" else "assistant/message",
            {**data, "legacy_message_id": message.id},
            model_visible=True,
            surface="transcript",
        )
        count += 1
    return count


def append_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
    *,
    actions: list | None = None,
    traces: list | None = None,
    kind: str = "chat",
    log_event: bool = True,
    turn: int | None = None,
    source_event_seq: int | None = None,
) -> AiMessage:
    session = session_for_conversation(db, conversation_id) if log_event else None
    msg = AiMessage(
        conversation_id=conversation_id,
        role=role,
        content=content or "",
        actions_json=json.dumps(actions or [], ensure_ascii=False),
        traces_json=json.dumps(traces or [], ensure_ascii=False),
        kind=kind,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    if session is not None:
        append_event(
            db,
            session.id,
            "user/message" if role == "user" else "assistant/message",
            {
                "content": content or "",
                "actions": actions or [],
                "traces": traces or [],
                "kind": kind,
                "legacy_message_id": msg.id,
            },
            turn=turn,
            model_visible=True,
            surface="transcript",
        )
    elif not log_event:
        projected_session = db.scalar(
            select(AiSession).where(AiSession.legacy_conversation_id == conversation_id)
        )
        if projected_session is not None:
            append_event(
                db,
                projected_session.id,
                "legacy/backfill",
                {
                    "legacy_message_id": msg.id,
                    "already_projected": True,
                    "source_event_seq": source_event_seq,
                },
                turn=turn,
                surface="audit",
            )
    return msg


def persist_assistant_error(
    db: Session,
    content: str,
    *,
    conversation_id: int | None = None,
    session_id: int | None = None,
) -> None:
    """把流失败写进会话投影，切页再拉还能看见助手气泡。

    实现：优先走对话表（会同时写 assistant/message）；没有对话时直接追加会话事件。
    只写 AiMessage、不写 assistant/message 时，列表接口读不到这句话。

    conversation_id: 旧对话主键，有则同时写消息表和会话事件。
    session_id: 没有对话时用来直接写 assistant/message。
    content: 失败原因，会作为助手气泡正文。
    """
    text = (content or "").strip() or "助手处理失败"
    if conversation_id:
        append_message(db, conversation_id, "assistant", text, kind="error", log_event=True)
        return
    if session_id:
        from app.modules.ai.sessions import append_event

        append_event(
            db,
            session_id,
            "assistant/message",
            {"content": text, "actions": [], "traces": [], "kind": "error"},
            model_visible=True,
            surface="transcript",
        )


def message_public(m: AiMessage) -> dict:
    try:
        actions = json.loads(m.actions_json or "[]")
    except json.JSONDecodeError:
        actions = []
    try:
        traces = json.loads(getattr(m, "traces_json", None) or "[]")
    except json.JSONDecodeError:
        traces = []
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "actions": actions if isinstance(actions, list) else [],
        "traces": traces if isinstance(traces, list) else [],
        "kind": m.kind,
        "created_at": m.created_at.isoformat() if m.created_at else "",
    }


_TRACE_RESULT_MAX = 1500
_TRACE_ARG_MAX = 240
_TRACE_BIG_KEYS = ("skill_md", "body", "content", "skill_content", "reply")
_TRACE_FINDING_MAX = 8
_TRACE_LIST_MAX = 30
_TRACE_LIST_ITEM_KEYS = (
    "id",
    "notice_id",
    "name",
    "title",
    "host",
    "alias",
    "display_name",
    "detail_url",
    "kind",
    "status",
    "env",
    "pipeline",
    "scope",
    "apply_type",
    "project",
    "group",
    "version",
    "code",
)


def _clip_mapping_lists(result: dict) -> dict | None:
    """目录类结果保住能辨认是哪一条的字段，丢掉正文。

    权限申请要留 scope / apply_type：整项目单的 pipeline 列只是占位文案，
    裁掉范围后轨迹里只剩「项目下全部流水线」，完整轨迹也看不出申请了什么。
    """
    list_keys = [key for key, value in result.items() if isinstance(value, list)]
    if not list_keys:
        return None
    keep: dict = {}
    for key, value in result.items():
        if isinstance(value, list):
            clipped = []
            for item in value[:_TRACE_LIST_MAX]:
                if isinstance(item, dict):
                    clipped.append({k: item[k] for k in _TRACE_LIST_ITEM_KEYS if k in item})
                else:
                    clipped.append(str(item)[:120])
            keep[key] = clipped
        elif key in {"hint", "unread_count", "total", "ok", "error", "code", "message"}:
            keep[key] = value
    return keep


def _clip_findings(findings: object) -> list:
    """失败体检只留能读的几条，避免整份诊断 JSON 撑爆会话事件。

    findings: propose_* 返回的 findings 列表，元素是 dict 或字符串。
    """
    if not isinstance(findings, list):
        return []
    clipped: list = []
    for item in findings[:_TRACE_FINDING_MAX]:
        if isinstance(item, dict):
            clipped.append(
                {
                    key: item.get(key)
                    for key in ("level", "code", "message", "path")
                    if item.get(key)
                }
            )
        else:
            clipped.append(str(item)[:240])
    return clipped


def _clip_trace_result(result: object) -> object:
    """页面轨迹只要失败原因或确认动作，不要把 SKILL.md 全文再写进会话。"""
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        keep: dict = {}
        if "ok" in result:
            keep["ok"] = result.get("ok")
        for key in ("error", "code", "message"):
            if result.get(key):
                keep[key] = result.get(key)
        findings = _clip_findings(result.get("findings"))
        if findings:
            keep["findings"] = findings
        return keep or {"ok": False, "message": "失败"}
    if isinstance(result, dict) and result.get("_action"):
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        keep = {
            "_action": result.get("_action"),
            "label": result.get("label"),
            "ok": True,
        }
        slim_payload = {
            key: payload[key]
            for key in ("pipeline_id", "release_id", "application_id", "node_ids")
            if key in payload
        }
        if slim_payload:
            keep["payload"] = slim_payload
        return keep
    if isinstance(result, dict):
        listed = _clip_mapping_lists(result)
        if listed is not None:
            return listed
    if isinstance(result, str) and len(result) <= _TRACE_RESULT_MAX:
        return result
    try:
        raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except TypeError:
        raw = str(result)
    if len(raw) <= _TRACE_RESULT_MAX:
        return result
    return raw[:_TRACE_RESULT_MAX] + "…"


def _clip_trace_args(args: dict) -> dict:
    clipped: dict = {}
    for key, value in args.items():
        if key in _TRACE_BIG_KEYS and isinstance(value, str) and len(value) > _TRACE_ARG_MAX:
            clipped[key] = value[:_TRACE_ARG_MAX] + "…"
        elif isinstance(value, str) and len(value) > _TRACE_RESULT_MAX:
            clipped[key] = value[:_TRACE_RESULT_MAX] + "…"
        else:
            clipped[key] = value
    return clipped


def slim_traces(traces: object) -> list:
    """页面轨迹只要「调了谁」；全文 SKILL.md 只留在确认卡 payload 里。

    整份正文再写进 assistant/message 会撑爆 MySQL TEXT，落库失败或截断后
    前端 merge 用空 traces 顶掉 SSE 那条，轨迹就消失了。
    """
    if not isinstance(traces, list):
        return []
    out = []
    for item in traces:
        if not isinstance(item, dict):
            continue
        slim = {"id": item.get("id"), "name": item.get("name")}
        args = item.get("arguments")
        if isinstance(args, dict):
            slim["arguments"] = _clip_trace_args(args)
        if "result" in item:
            slim["result"] = _clip_trace_result(item.get("result"))
        out.append(slim)
    return out


def _slim_traces(traces: object) -> list:
    return slim_traces(traces)


def _messages_from_events(
    events: list, *, after_id: int = 0, limit: int = 200, viewer_session_id: int | None = None
) -> list[dict]:
    out = []
    for message in project_events(events, viewer_session_id=viewer_session_id)["messages"]:
        # id 必须用 event_seq：落库后才补上的 legacy_message_id 会变大，
        # 轮询 after_id 会把同一句用户话再拉进来，页面上就出现两个一模一样的气泡。
        item = {
            **message,
            "id": int(message["event_seq"]),
            "traces": _slim_traces(message.get("traces")),
        }
        item.pop("event_seq", None)
        item.pop("legacy_message_id", None)
        out.append(item)
    if after_id:
        return [item for item in out if item["id"] > after_id][:limit]
    return out[-limit:]


def list_messages(db: Session, conversation_id: int, *, after_id: int = 0, limit: int = 200) -> list[dict]:
    session = session_for_conversation(db, conversation_id)
    return _messages_from_events(
        transcript_events(db, session.id),
        after_id=after_id,
        limit=limit,
        viewer_session_id=session.id,
    )


def list_session_messages(db: Session, session_id: int, *, after_id: int = 0, limit: int = 200) -> list[dict]:
    """按会话事件投影消息。id 始终用 event_seq，避免 backfill 后 id 跳变导致轮询重复。"""
    return _messages_from_events(
        transcript_events(db, session_id),
        after_id=after_id,
        limit=limit,
        viewer_session_id=session_id,
    )


def last_assistant_turn(db: Session, conversation_id: int) -> dict | None:
    """当前用户这句话之前，最近一条助手消息（含确认卡片）。"""
    rows = list_messages(db, conversation_id, limit=24)
    for m in reversed(rows):
        if m.get("role") == "assistant" and m.get("kind") != "followup":
            return m
    return None


def _truncate(text: str, limit: int = HISTORY_TEXT_MAX_TOKENS) -> str:
    return clip_to_tokens((text or "").strip(), limit, suffix="…")


def load_llm_history(
    db: Session,
    conversation_id: int,
    *,
    drop_trailing_user: bool = True,
    limit: int = 8,
) -> list[dict]:
    """从会话日志重建模型 messages（含 tool_calls / tool 结果）。"""
    rows = list_messages(db, conversation_id, limit=limit + 8)
    return _llm_history_from_rows(rows, drop_trailing_user=drop_trailing_user)


def load_session_llm_history(
    db: Session,
    session_id: int,
    *,
    drop_trailing_user: bool = True,
    limit: int = 8,
) -> list[dict]:
    rows = project_events(effective_events(db, session_id))["messages"][-(limit + 8):]
    return _llm_history_from_rows(rows, drop_trailing_user=drop_trailing_user)


def _llm_history_from_rows(rows: list[dict], *, drop_trailing_user: bool) -> list[dict]:
    if drop_trailing_user and rows and rows[-1].get("role") == "user":
        rows = rows[:-1]
    out: list[dict] = []
    for m in rows:
        role = m.get("role")
        if role == "user":
            text = _truncate(m.get("content") or "")
            if text:
                out.append({"role": "user", "content": text})
            continue
        if role != "assistant":
            continue
        traces = m.get("traces") or []
        if traces:
            tool_calls = []
            for i, t in enumerate(traces):
                tid = str(t.get("id") or f"hist-{m.get('id')}-{i}")
                tool_calls.append(
                    {
                        "id": tid,
                        "type": "function",
                        "function": {
                            "name": t.get("name") or "",
                            "arguments": json.dumps(t.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                )
            out.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for i, t in enumerate(traces):
                tid = str(t.get("id") or f"hist-{m.get('id')}-{i}")
                payload = tool_result_for_model(
                    t.get("name") or "", t.get("result"), HISTORY_TOOL_RESULT_MAX_TOKENS
                )
                out.append({"role": "tool", "tool_call_id": tid, "content": payload})
        text = _truncate(m.get("content") or "")
        actions = m.get("actions") or []
        if actions:
            names = "、".join(str(a.get("label") or a.get("type") or "") for a in actions[:3])
            extra = (
                f"\n[系统] 该轮已在页面上给出确认按钮：{names}。"
                "用户要点按钮才算确认。下一轮若用户改了需求，必须重新调用对应 propose_* 出新按钮；"
                "禁止改口成「请回复确认」。"
            )
            text = (text + extra) if text else extra.strip()
        if traces and looks_like_wait_reply(text):
            # 历史里那句「正在查询」会让下一轮模型以为已经回答过了
            continue
        if text:
            out.append({"role": "assistant", "content": text})
        elif m.get("kind") == "followup":
            continue
    return out[-24:]
