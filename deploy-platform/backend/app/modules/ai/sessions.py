"""Append-only AI session event store and deterministic projections."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.ai.models import AiSession, AiSessionEvent

EVENT_TYPES = frozenset(
    {
        "session/created",
        "session/forked",
        "session/cleared",
        "assistant/truncated",
        "assistant/reasoning",
        "legacy/backfill",
        "turn/start",
        "turn/end",
        "turn/status",
        "step/start",
        "step/end",
        "request/header",
        "request/context",
        "user/message",
        "assistant/chunk",
        "assistant/message",
        "tool/call",
        "tool/pre-execute",
        "tool/approval",
        "tool/result",
        "tool/post-execute",
        "error",
    }
)
SURFACES = frozenset({"internal", "transcript", "model_context", "audit", "stream"})
ZERO_DIGEST = "0" * 64
# 进数据库的才是会话数据。其它类型是日志，只写 ES，不占哈希链。
DURABLE_EVENT_TYPES = frozenset(
    {
        "session/created",
        "session/forked",
        "session/cleared",
        "legacy/backfill",
        "turn/start",
        "turn/end",
        "turn/status",
        "user/message",
        "assistant/message",
        "tool/call",
        "tool/result",
    }
)

# 刷新后重连只看这些类型。超过这个时长仍无 turn/end，当作进程已死，不再挂思考气泡。
_LIVE_EVENT_TYPES = frozenset({"turn/start", "turn/end", "turn/status", "tool/call", "tool/result"})
_LIVE_STALE_SECONDS = 30 * 60


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _payload(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _digest(
    session_id: int,
    seq: int,
    turn: int | None,
    step: int | None,
    call: str,
    event_type: str,
    payload_json: str,
    model_visible: bool,
    surface: str,
    occurred_at: datetime,
    prev_digest: str,
) -> str:
    """内容哈希。occurred_at 不算进去：MySQL DATETIME 会丢掉微秒，写进去再读出来对不上。"""
    record = {
        "session_id": session_id,
        "seq": seq,
        "turn": turn,
        "step": step,
        "call": call or "",
        "type": event_type,
        "payload": json.loads(payload_json),
        "model_visible": bool(model_visible),
        "surface": surface,
        "prev_digest": prev_digest,
    }
    return hashlib.sha256(_json(record).encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    user_id: int,
    *,
    title: str = "默认会话",
    legacy_conversation_id: int | None = None,
    parent_session_id: int | None = None,
    forked_from_seq: int | None = None,
    model_pk: int | None = None,
) -> AiSession:
    row = AiSession(
        user_id=user_id,
        title=(title or "默认会话")[:128],
        legacy_conversation_id=legacy_conversation_id,
        parent_session_id=parent_session_id,
        forked_from_seq=forked_from_seq,
        model_pk=model_pk,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    append_event(
        db,
        row.id,
        "session/forked" if parent_session_id else "session/created",
        {
            "parent_session_id": parent_session_id,
            "forked_from_seq": forked_from_seq,
            "title": row.title,
        },
        surface="audit",
    )
    return row


class LoggedEvent:
    """ES 里的审计日志：没有 seq，不进哈希链。"""

    def __init__(
        self,
        session_id: int,
        event_type: str,
        payload: dict | None = None,
        *,
        turn: int | None = None,
        step: int | None = None,
        call: str = "",
        surface: str = "audit",
    ) -> None:
        self.id = 0
        self.seq = 0
        self.session_id = session_id
        self.turn = turn
        self.step = step
        self.call = call or ""
        self.type = event_type
        self.payload_json = _json(payload or {})
        self.model_visible = False
        self.surface = surface
        self.occurred_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.prev_digest = ""
        self.digest = ""


def is_durable_event(event_type: str) -> bool:
    return event_type in DURABLE_EVENT_TYPES


def append_event(
    db: Session,
    session_id: int,
    event_type: str,
    payload: dict | None = None,
    *,
    turn: int | None = None,
    step: int | None = None,
    call: str = "",
    model_visible: bool = False,
    surface: str = "internal",
    retries: int = 8,
) -> AiSessionEvent | LoggedEvent:
    """数据进库并 CAS 更新 head；日志只写 ES，失败不挡会话。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知 session event type: {event_type}")
    if surface not in SURFACES:
        raise ValueError(f"未知 event surface: {surface}")
    if model_visible and surface not in {"model_context", "transcript"}:
        raise ValueError("model_visible 事件必须落在 model_context 或 transcript surface")
    if not is_durable_event(event_type):
        session = db.get(AiSession, session_id)
        if session is None:
            raise LookupError(f"AI session {session_id} 不存在")
        from app.modules.ai.audit_store import append_ai_log

        append_ai_log(
            db,
            session_id,
            event_type,
            payload,
            turn=turn,
            step=step,
            call=call,
            surface=surface,
        )
        return LoggedEvent(
            session_id,
            event_type,
            None,
            turn=turn,
            step=step,
            call=call,
            surface=surface,
        )
    payload_json = _json(payload or {})
    for _ in range(retries):
        session = db.get(AiSession, session_id)
        if session is None:
            raise LookupError(f"AI session {session_id} 不存在")
        expected = int(session.head_seq or 0)
        seq = expected + 1
        prev = session.head_digest or ZERO_DIGEST
        occurred_at = datetime.now(timezone.utc).replace(tzinfo=None)
        digest = _digest(
            session_id,
            seq,
            turn,
            step,
            call or "",
            event_type,
            payload_json,
            model_visible,
            surface,
            occurred_at,
            prev,
        )
        claimed = db.execute(
            update(AiSession)
            .where(AiSession.id == session_id, AiSession.head_seq == expected)
            .values(head_seq=seq, head_digest=digest, updated_at=occurred_at)
        )
        if claimed.rowcount != 1:
            db.rollback()
            db.expire_all()
            continue
        event = AiSessionEvent(
            session_id=session_id,
            seq=seq,
            turn=turn,
            step=step,
            call=call or "",
            type=event_type,
            payload_json=payload_json,
            model_visible=model_visible,
            surface=surface,
            occurred_at=occurred_at,
            prev_digest=prev,
            digest=digest,
        )
        db.add(event)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            db.expire_all()
            continue
        db.refresh(event)
        return event
    raise RuntimeError("session event 并发追加冲突，重试次数已耗尽")


def event_public(event: AiSessionEvent) -> dict:
    return {
        "id": event.id,
        "session_id": event.session_id,
        "seq": event.seq,
        "turn": event.turn,
        "step": event.step,
        "call": event.call,
        "type": event.type,
        "payload": _payload(event.payload_json),
        "model_visible": event.model_visible,
        "surface": event.surface,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else "",
        "prev_digest": event.prev_digest,
        "digest": event.digest,
    }


# 页面拉聊天记录只需要这些类型。tool/result 往往带着整表流水线 JSON，
# 进 GET /ai/sessions/{id} 会把空会话也拖成几秒到几十秒。
TRANSCRIPT_EVENT_TYPES = frozenset(
    {
        "user/message",
        "assistant/message",
        "session/cleared",
        "legacy/backfill",
    }
)


def read_events(
    db: Session,
    session_id: int,
    *,
    after_seq: int = 0,
    limit: int = 1000,
    types: frozenset[str] | set[str] | None = None,
) -> list[AiSessionEvent]:
    conditions = [
        AiSessionEvent.session_id == session_id,
        AiSessionEvent.seq > after_seq,
    ]
    if types:
        conditions.append(AiSessionEvent.type.in_(tuple(types)))
    return list(
        db.scalars(
            select(AiSessionEvent)
            .where(*conditions)
            .order_by(AiSessionEvent.seq)
            .limit(min(max(limit, 1), 5000))
        ).all()
    )


def transcript_events(db: Session, session_id: int) -> list[AiSessionEvent]:
    """分支可见的对话事件：父会话前缀 + 自己的，不含工具结果大字段。"""
    session = db.get(AiSession, session_id)
    if session is None:
        raise LookupError("会话不存在")
    own = read_events(db, session_id, limit=2000, types=TRANSCRIPT_EVENT_TYPES)
    if not session.parent_session_id:
        return own
    parent = transcript_events(db, session.parent_session_id)
    cutoff = session.forked_from_seq or 0
    return [event for event in parent if event.seq <= cutoff] + own


def effective_events(db: Session, session_id: int) -> list[AiSessionEvent]:
    """返回分支可见历史：父会话前缀 + 子会话自己的不可变事件。"""
    session = db.get(AiSession, session_id)
    if session is None:
        raise LookupError("会话不存在")
    own = read_events(db, session_id, limit=5000)
    if not session.parent_session_id:
        return own
    parent = effective_events(db, session.parent_session_id)
    cutoff = session.forked_from_seq or 0
    return [event for event in parent if event.seq <= cutoff] + own


def verify_chain(db: Session, session_id: int) -> dict:
    """校验 seq 连续、prev_digest 接龙、head 指针一致。

    内容哈希是附加检查：新写入不再把 occurred_at 算进去（MySQL DATETIME 会丢微秒）。
    已经落库的旧事件对不上内容哈希时，只要指针链完整仍视为可回放。
    """
    prev = ZERO_DIGEST
    expected = 1
    for event in read_events(db, session_id, limit=5000):
        if event.seq != expected or event.prev_digest != prev:
            return {"ok": False, "seq": event.seq, "expected_seq": expected}
        prev = event.digest
        expected += 1
    session = db.get(AiSession, session_id)
    ok = bool(session and session.head_seq == expected - 1 and (session.head_digest or "") == prev)
    return {"ok": ok, "count": expected - 1, "head_digest": prev}


def _actions_for_viewer(actions: object, event: AiSessionEvent, viewer_session_id: int | None) -> list:
    """分支会话继承父会话确认卡时，签名绑的是父 conversation，点下去只会报无效。

    投影里去掉 token，前端按钮禁用，而不是让人点了再失败。
    """
    if not isinstance(actions, list):
        return []
    if not viewer_session_id or event.session_id == viewer_session_id:
        return actions
    stripped = []
    for item in actions:
        if not isinstance(item, dict):
            stripped.append(item)
            continue
        copy = dict(item)
        copy["token"] = ""
        stripped.append(copy)
    return stripped


def project_events(events: list[AiSessionEvent], *, viewer_session_id: int | None = None) -> dict:
    from app.modules.ai.chat_images import public_urls as chat_image_urls

    messages: list[dict] = []
    turns: dict[int, dict] = {}
    tool_calls: dict[str, dict] = {}
    errors: list[dict] = []
    legacy_ids = {
        int(payload["source_event_seq"]): int(payload["legacy_message_id"])
        for event in events
        if event.type == "legacy/backfill"
        for payload in [_payload(event.payload_json)]
        if payload.get("source_event_seq") and payload.get("legacy_message_id")
    }
    for event in events:
        payload = _payload(event.payload_json)
        if event.type == "session/cleared":
            messages.clear()
            turns.clear()
            tool_calls.clear()
            errors.clear()
            continue
        if event.turn is not None:
            turn = turns.setdefault(event.turn, {"turn": event.turn, "status": "open", "steps": []})
            if event.type == "turn/end":
                turn["status"] = payload.get("status") or "completed"
            if event.type == "step/start" and event.step not in turn["steps"]:
                turn["steps"].append(event.step)
        if event.type in {"user/message", "assistant/message"}:
            messages.append(
                {
                    "event_seq": event.seq,
                    "legacy_message_id": payload.get("legacy_message_id") or legacy_ids.get(event.seq),
                    "role": "user" if event.type == "user/message" else "assistant",
                    "content": str(payload.get("content") or ""),
                    "images": chat_image_urls(event.session_id, payload.get("images")),
                    "actions": _actions_for_viewer(payload.get("actions") or [], event, viewer_session_id),
                    "traces": payload.get("traces") or [],
                    "kind": payload.get("kind") or "chat",
                    "created_at": event.occurred_at.isoformat() if event.occurred_at else "",
                }
            )
        elif event.type == "tool/call":
            tool_calls[event.call] = {"call": event.call, **payload, "status": "called"}
        elif event.type == "tool/result":
            tool_calls.setdefault(event.call, {"call": event.call}).update(
                {"result": payload.get("result"), "status": payload.get("status") or "completed"}
            )
        elif event.type == "error":
            errors.append({"seq": event.seq, **payload})
    return {
        "messages": messages,
        "turns": list(turns.values()),
        "tool_calls": list(tool_calls.values()),
        "errors": errors,
        "last_seq": events[-1].seq if events else 0,
    }


def inspect_session(db: Session, session_id: int, *, after_seq: int = 0) -> dict:
    session = db.get(AiSession, session_id)
    if session is None:
        raise LookupError("会话不存在")
    events = read_events(db, session_id, after_seq=after_seq)
    from app.modules.ai.audit_store import list_ai_logs

    return {
        "session": session_public(session),
        "events": [event_public(e) for e in events],
        "logs": list_ai_logs(db, session_id),
        "projection": project_events(effective_events(db, session_id), viewer_session_id=session_id),
        "chain": verify_chain(db, session_id),
    }


def session_public(row: AiSession) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "title": row.title,
        "status": row.status,
        "parent_session_id": row.parent_session_id,
        "forked_from_seq": row.forked_from_seq,
        "head_seq": row.head_seq,
        "head_digest": row.head_digest,
        "model_id": row.model_pk,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def list_sessions(db: Session, user_id: int, *, limit: int = 100) -> list[dict]:
    rows = db.scalars(
        select(AiSession)
        .where(AiSession.user_id == user_id, AiSession.status != "deleted")
        .order_by(AiSession.updated_at.desc(), AiSession.id.desc())
        .limit(min(max(limit, 1), 500))
    ).all()
    return [session_public(row) for row in rows]


def delete_session(db: Session, session_id: int) -> None:
    """删会话时连聊天记录一起去掉。

    只把 status 标成 deleted 的话，侧栏没了，GET /ai/conversation 和事件投影
    还在，重新打开就像记录没删，还会把别的会话的话掺进来。
    """
    from app.modules.ai.models import AiConversation, AiMessage, AiWatch

    session = db.get(AiSession, session_id)
    if session is None:
        raise LookupError("会话不存在")
    db.execute(
        update(AiSession)
        .where(AiSession.parent_session_id == session_id)
        .values(parent_session_id=None, forked_from_seq=None)
    )
    db.execute(delete(AiSessionEvent).where(AiSessionEvent.session_id == session_id))
    conv_id = session.legacy_conversation_id
    if conv_id:
        db.query(AiWatch).filter(AiWatch.conversation_id == conv_id).delete(
            synchronize_session=False
        )
        db.query(AiMessage).filter(AiMessage.conversation_id == conv_id).delete(
            synchronize_session=False
        )
    # MySQL 外键：session.legacy_conversation_id → conversation.id。
    # SQLAlchemy 默认 flush 会先删 conversation，于是 1451。先摘外键、删 session，再删 conversation。
    session.legacy_conversation_id = None
    db.flush()
    db.delete(session)
    db.flush()
    if conv_id:
        conv = db.get(AiConversation, conv_id)
        if conv is not None:
            db.delete(conv)
    db.commit()
    from app.modules.ai.chat_images import remove_session

    remove_session(session_id)


def clear_session(db: Session, session_id: int) -> dict:
    """清空对话投影，会话本身留在侧栏。"""
    from app.modules.ai.models import AiConversation, AiWatch

    append_event(
        db,
        session_id,
        "session/cleared",
        {"reason": "user"},
        surface="audit",
    )
    session = db.get(AiSession, session_id)
    stopped = 0
    if session is not None and session.legacy_conversation_id:
        conv = db.get(AiConversation, session.legacy_conversation_id)
        if conv is not None:
            from app.modules.ai.models import AiMessage

            conv.working_json = "{}"
            stopped = (
                db.query(AiWatch)
                .filter(AiWatch.conversation_id == conv.id, AiWatch.status == "watching")
                .update({AiWatch.status: "cancelled"}, synchronize_session=False)
                or 0
            )
            db.query(AiMessage).filter(AiMessage.conversation_id == conv.id).delete(
                synchronize_session=False
            )
        db.commit()
    return {"stopped_watches": int(stopped)}


def search_events(db: Session, session_id: int, query: str, *, limit: int = 100) -> list[dict]:
    needle = (query or "").casefold()
    rows = read_events(db, session_id, limit=5000)
    if needle:
        rows = [
            event
            for event in rows
            if needle in event.type.casefold() or needle in event.payload_json.casefold()
        ]
    found = [event_public(event) for event in rows]
    from app.modules.ai.audit_store import search_ai_logs

    found.extend(search_ai_logs(db, session_id, query, limit=limit))
    return found[: min(max(limit, 1), 500)]


def next_turn(db: Session, session_id: int) -> int:
    turns = [event.turn or 0 for event in effective_events(db, session_id)]
    return max(turns, default=0) + 1


def live_turn_of(db: Session, session_id: int) -> dict | None:
    """这一轮还在跑时返回进度，供刷新后重挂思考气泡。

    生成跟浏览器连接无关：turn/start 已落库，工具进度也写在同一条事件链上。
    前端丢掉 SSE 之后按会话来读，不需要 Redis 里的连接标记。
    进程已经死掉的开口轮次超过 _LIVE_STALE_SECONDS 就不再当作进行中。
    """
    start = db.scalars(
        select(AiSessionEvent)
        .where(
            AiSessionEvent.session_id == session_id,
            AiSessionEvent.type == "turn/start",
        )
        .order_by(AiSessionEvent.seq.desc())
        .limit(1)
    ).first()
    if start is None or start.turn is None:
        return None
    ended = db.scalar(
        select(AiSessionEvent.id).where(
            AiSessionEvent.session_id == session_id,
            AiSessionEvent.type == "turn/end",
            AiSessionEvent.turn == start.turn,
        )
    )
    if ended:
        return None
    started_at = start.occurred_at
    if started_at is not None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        age = (now - started_at).total_seconds()
        if age > _LIVE_STALE_SECONDS:
            return None
    rows = list(
        db.scalars(
            select(AiSessionEvent)
            .where(
                AiSessionEvent.session_id == session_id,
                AiSessionEvent.turn == start.turn,
                AiSessionEvent.type.in_(tuple(_LIVE_EVENT_TYPES)),
            )
            .order_by(AiSessionEvent.seq)
        ).all()
    )
    status = "正在思考…"
    for event in rows:
        payload = _payload(event.payload_json)
        if event.type == "turn/status":
            text = str(payload.get("text") or "").strip()
            if text:
                status = text
        elif event.type == "tool/call":
            name = str(payload.get("name") or event.call or "").strip()
            if name:
                status = f"正在调用 {name}…"
        elif event.type == "tool/result":
            status = "正在整理结果…"
    return {
        "running": True,
        "turn": start.turn,
        "status": status,
        "seq": start.seq,
    }


def resume_session(db: Session, session_id: int) -> dict:
    """冷恢复时仅追加中断收尾，永不修改已有事件。"""
    events = read_events(db, session_id, limit=5000)
    starts = [e for e in events if e.type == "turn/start" and e.turn is not None]
    ended = {e.turn for e in events if e.type == "turn/end"}
    open_turns = [e.turn for e in starts if e.turn not in ended]
    if not open_turns:
        return {"recovered": False, "projection": project_events(events)}
    turn = max(open_turns)
    unresolved: dict[str, AiSessionEvent] = {}
    for event in events:
        if event.turn != turn or not event.call:
            continue
        if event.type == "tool/call":
            unresolved[event.call] = event
        elif event.type == "tool/result":
            unresolved.pop(event.call, None)
    for call, source in unresolved.items():
        append_event(
            db,
            session_id,
            "tool/result",
            {"status": "interrupted", "result": {"error": "进程中断，工具结果未知"}},
            turn=turn,
            step=source.step,
            call=call,
            model_visible=True,
            surface="model_context",
        )
    append_event(
        db,
        session_id,
        "turn/end",
        {"status": "interrupted", "reason": "cold-recovery"},
        turn=turn,
        surface="audit",
    )
    return {"recovered": True, "turn": turn, "projection": project_events(read_events(db, session_id))}


def fork_session(
    db: Session, session_id: int, user_id: int, *, at_seq: int | None = None, title: str = ""
) -> AiSession:
    parent = db.get(AiSession, session_id)
    if parent is None or parent.user_id != user_id:
        raise LookupError("会话不存在")
    seq = parent.head_seq if at_seq is None else min(max(at_seq, 0), parent.head_seq)
    return create_session(
        db,
        user_id,
        title=title or f"{parent.title}（分支）",
        parent_session_id=parent.id,
        forked_from_seq=seq,
        model_pk=parent.model_pk,
    )


def replay_session(db: Session, session_id: int) -> dict:
    """纯读重放；调用方若要重新执行必须先显式 fork。"""
    events = effective_events(db, session_id)
    return {"read_only": True, "projection": project_events(events, viewer_session_id=session_id), "chain": verify_chain(db, session_id)}
