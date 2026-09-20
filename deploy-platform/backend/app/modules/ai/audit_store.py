"""AI 助手审计日志：只进 Elasticsearch，禁止写业务库，禁止攒进程内存。

对话正文、工具续聊、会话生命周期是数据，走 MySQL。
步骤/请求上下文/工具流水是日志：ES 挂了丢这一条，聊天照常跑。
索引前缀固定 rp-assist-logs，不和构建日志混在一起。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.modules.agent.log_store import LogStore, NullLogStore, _build_es_store
from app.modules.settings.defaults import DEFAULT_AI_LOG_INDEX_PREFIX

logger = logging.getLogger(__name__)

AI_LOG_INDEX = DEFAULT_AI_LOG_INDEX_PREFIX

_store_lock = threading.Lock()
_cached_store: LogStore | None = None
_cached_sig = ""
_override: LogStore | None = None


def set_ai_audit_store_for_tests(store: LogStore | None) -> None:
    """单测注入。生产路径禁止调用。"""
    global _override
    _override = store


def reset_ai_audit_store() -> None:
    global _cached_store, _cached_sig
    with _store_lock:
        _cached_store = None
        _cached_sig = ""


def _load_cfg(db: Any) -> dict:
    try:
        from app.modules.settings.service import get_all_settings

        if db is not None:
            return get_all_settings(db)
    except Exception:  # noqa: BLE001
        pass
    from app.modules.settings.defaults import DEFAULT_SETTINGS

    return dict(DEFAULT_SETTINGS)


def create_ai_audit_store(settings_provider=None) -> LogStore:
    """永远不写 MySQL，也永远不把 AI 日志攒在进程内存。"""
    cfg = settings_provider() if settings_provider else {}
    return _build_es_store(cfg, index=AI_LOG_INDEX)


def get_ai_audit_store(db: Any = None) -> LogStore:
    if _override is not None:
        return _override
    global _cached_store, _cached_sig
    with _store_lock:
        if _cached_store is not None:
            return _cached_store
        cfg = _load_cfg(db)
        sig = "|".join(str(cfg.get(k, "")) for k in ("es_hosts", "es_username", "es_password"))
        store = create_ai_audit_store(lambda: cfg)
        name = store.name().lower()
        if "memory" in name or "mysql" in name:
            logger.warning("AI 审计日志拒绝 %s，改为丢弃", store.name())
            store = NullLogStore()
        _cached_store = store
        _cached_sig = sig
        return store


_MAX_PAYLOAD_CHARS = 8000
_LIST_LIMIT = 200
_SEARCH_SCAN = 400


def _clip_payload(payload: dict | None) -> dict:
    """整包 messages 不能原样进 ES：一次对话就能到几百 KB，检查页再拉回来会卡浏览器。"""
    data = payload or {}
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"_truncated": True, "error": "payload 无法序列化"}
    if len(raw) <= _MAX_PAYLOAD_CHARS:
        return data
    return {"_truncated": True, "chars": len(raw), "preview": raw[:_MAX_PAYLOAD_CHARS]}


def append_ai_log(
    db: Any,
    session_id: int,
    event_type: str,
    payload: dict | None = None,
    *,
    turn: int | None = None,
    step: int | None = None,
    call: str = "",
    surface: str = "audit",
) -> None:
    """写一条审计日志。失败只 warning，绝不抛给聊天。"""
    record = {
        "session_id": session_id,
        "type": event_type,
        "surface": surface,
        "turn": turn,
        "step": step,
        "call": call or "",
        "payload": _clip_payload(payload),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        get_ai_audit_store(db).append_batch(session_id, [line])
    except Exception as e:  # noqa: BLE001
        logger.warning("AI 审计日志写入失败，丢弃本条：%s", e)


def _parse_line(line: str) -> dict | None:
    try:
        value = json.loads(line or "{}")
    except json.JSONDecodeError:
        return {"type": "log", "payload": {"raw": line}}
    return value if isinstance(value, dict) else {"type": "log", "payload": {"value": value}}


def list_ai_logs(db: Any, session_id: int, *, limit: int = _LIST_LIMIT) -> list[dict]:
    cap = min(max(limit, 1), _SEARCH_SCAN)
    try:
        lines = get_ai_audit_store(db).get(session_id, start=0, limit=cap) or []
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 AI 审计日志失败：%s", e)
        return []
    out: list[dict] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is not None:
            out.append(parsed)
    return out


def search_ai_logs(db: Any, session_id: int, query: str, *, limit: int = 100) -> list[dict]:
    needle = (query or "").casefold()
    rows = list_ai_logs(db, session_id, limit=_SEARCH_SCAN)
    if needle:
        rows = [
            row
            for row in rows
            if needle in str(row.get("type") or "").casefold()
            or needle in str(row.get("payload") or "").casefold()
        ]
    return rows[: min(max(limit, 1), 200)]
