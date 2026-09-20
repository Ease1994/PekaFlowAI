"""只读收件箱 Tool：只看自己的通知，不标已读。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.ai.chat import format_tool_reply
from app.modules.ai.registry import get_skill
from app.modules.ai.skills import load_all
from app.modules.ai.skills.inbox import _get_notification, _list_notifications
from app.modules.ai.skills.platform import _list_platform_tools
from app.modules.notify.models import InAppNotice
from tests.test_harness_isolation import _memory_db

ME = SimpleNamespace(id=7, username="me", is_admin=False)
OTHER = SimpleNamespace(id=8, username="other", is_admin=False)


def _notice_db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[InAppNotice.__table__])
    return Session(engine)


def _add(db: Session, user_id: int, title: str, content: str, *, read: bool = False) -> InAppNotice:
    row = InAppNotice(
        user_id=user_id,
        title=title,
        content=content,
        kind="release.failed",
        is_read=read,
        created_at=datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_list_notifications_is_own_unread_and_does_not_mark_read() -> None:
    db = _notice_db()
    mine = _add(db, ME.id, "构建失败", "订单测试挂了，看第 3 步")
    _add(db, ME.id, "已读的", "旧消息", read=True)
    _add(db, OTHER.id, "别人的", "不该看见")

    out = _list_notifications(db, ME, {})
    titles = [n["title"] for n in out["notifications"]]
    assert titles == ["构建失败"]
    assert out["unread_count"] == 1
    assert out["notifications"][0]["notice_id"] == mine.id
    assert out["notifications"][0]["detail_url"] == f"/notifications?id={mine.id}"
    assert "content" not in out["notifications"][0]

    db.refresh(mine)
    assert mine.is_read is False

    other_box = _list_notifications(db, OTHER, {"unread_only": False})
    assert [n["title"] for n in other_box["notifications"]] == ["别人的"]


def test_get_notification_hides_other_users() -> None:
    db = _notice_db()
    mine = _add(db, ME.id, "构建失败", "全文")
    theirs = _add(db, OTHER.id, "别人的", "机密")

    ok = _get_notification(db, ME, {"notice_id": mine.id})
    assert ok["notification"]["content"] == "全文"
    assert ok["notification"]["notice_id"] == mine.id
    assert ok["notification"]["detail_url"] == f"/notifications?id={mine.id}"

    by_id = _get_notification(db, ME, {"id": mine.id})
    assert by_id["notification"]["content"] == "全文"

    hidden = _get_notification(db, ME, {"notice_id": theirs.id})
    assert "error" in hidden
    assert "notification" not in hidden


def test_notification_tool_reply_uses_title() -> None:
    traces = [
        {
            "name": "list_notifications",
            "result": {
                "notifications": [
                    {"id": 3, "title": "构建失败", "content": "订单测试挂了", "kind": "release.failed"}
                ],
                "unread_count": 1,
            },
        }
    ]
    text = format_tool_reply(traces)
    assert "构建失败" in text
    assert "订单测试挂了" not in text


def test_empty_inbox_hallucination_is_replaced_by_titles() -> None:
    from app.modules.ai.chat import finalize_assistant_reply

    traces = [
        {
            "name": "list_notifications",
            "result": {
                "notifications": [
                    {
                        "id": 271,
                        "title": "应急工单",
                        "detail_url": "/notifications?id=271",
                    }
                ],
                "unread_count": 1,
            },
        }
    ]
    text = finalize_assistant_reply("未读通知：当前没有未读消息，收件箱是空的。", traces)
    assert "应急工单" in text
    assert "没有未读" not in text
    assert "/notifications?id=271" in text


def test_unread_only_false_string_does_not_become_true() -> None:
    """模型常传 unread_only=\"false\"。bool(\"false\") 是 True，必须按未读过滤关掉。"""
    from app.modules.ai.skills.inbox import coerce_flag

    assert coerce_flag("false", default=True) is False
    assert coerce_flag(False, default=True) is False
    assert coerce_flag("true", default=False) is True
    assert coerce_flag(None, default=True) is True

    db = _notice_db()
    _add(db, ME.id, "已读的", "旧消息", read=True)
    _add(db, ME.id, "还没读", "新消息", read=False)
    out = _list_notifications(db, ME, {"unread_only": "false"})
    assert out["unread_only"] is False
    titles = [n["title"] for n in out["notifications"]]
    assert "已读的" in titles
    assert "还没读" in titles


def test_unread_query_does_not_list_read_notices() -> None:
    """收件箱里只有已读时，未读查询必须是空，不能把 20 条已读写成未读。"""
    from app.modules.ai.chat import finalize_assistant_reply, format_tool_reply, is_inbox_intent
    from app.modules.ai.skills.inbox import format_list_reply

    db = _notice_db()
    for i in range(20):
        _add(db, ME.id, f"已读 #{i}", "旧", read=True)
    out = _list_notifications(db, ME, {"unread_only": True})
    assert out["unread_count"] == 0
    assert out["notifications"] == []
    assert format_list_reply(out) == "当前没有未读消息。"
    assert "已读 #" not in format_tool_reply(
        [{"name": "list_notifications", "result": out}]
    )

    hallucinated = "未读通知 (共 20 条):\n" + "\n".join(f"{i}. 已读 #{i}" for i in range(20))
    text = finalize_assistant_reply(hallucinated, [{"name": "list_notifications", "result": out}])
    assert text == "当前没有未读消息。"
    assert "已读 #" not in text
    assert is_inbox_intent("查一下我的未读消息")
    assert not is_inbox_intent("写一个技能，读取未读消息")


def test_try_direct_inbox_unread_skips_read_notices(monkeypatch) -> None:
    """同一句未读问句不进模型，直接用当前未读计数。"""
    from app.modules.ai import chat as chat_mod
    from app.modules.ai.chat import try_direct_inbox

    empty = {
        "notifications": [],
        "unread_count": 0,
        "unread_only": True,
        "hint": "没有未读通知",
    }
    monkeypatch.setattr(chat_mod, "execute_tool", lambda *args, **kwargs: empty)
    out = try_direct_inbox(MagicMock(), ME, "查一下我的未读消息")
    assert out is not None
    assert out["reply"] == "当前没有未读消息。"
    assert out["traces"][0]["arguments"]["unread_only"] is True
    assert try_direct_inbox(MagicMock(), ME, "写一个技能，读取未读消息") is None
    assert try_direct_inbox(MagicMock(), ME, "安装一下读取未读通知") is None
    assert try_direct_inbox(MagicMock(), ME, "读取未读通知") is not None


def test_install_skill_does_not_direct_inbox(monkeypatch) -> None:
    """装/卸技能不走未读直达；查未读仍直达。不另开一轮分类模型。"""
    from app.modules.ai import chat as chat_mod
    from app.modules.ai.chat import is_inbox_intent, try_direct_inbox
    from app.modules.ai.intent import is_skill_lifecycle_utterance

    assert is_skill_lifecycle_utterance("安装一下读取未读通知")
    assert not is_inbox_intent("安装一下读取未读通知")
    assert is_inbox_intent("查一下我的未读消息")
    assert is_inbox_intent("读取未读通知")

    executed: list[str] = []
    monkeypatch.setattr(
        chat_mod,
        "execute_tool",
        lambda *args, **kwargs: executed.append(args[1] if len(args) > 1 else "") or {},
    )
    assert try_direct_inbox(MagicMock(), ME, "安装一下读取未读通知") is None
    assert executed == []

    empty = {
        "notifications": [],
        "unread_count": 0,
        "unread_only": True,
        "hint": "没有未读通知",
    }
    monkeypatch.setattr(chat_mod, "execute_tool", lambda *args, **kwargs: empty)
    out = try_direct_inbox(MagicMock(), ME, "查一下我的未读消息")
    assert out is not None


def test_readonly_tools_are_registered() -> None:
    load_all()
    for name in (
        "list_notifications",
        "get_notification",
        "list_platform_tools",
        "list_agent_skills",
        "list_pending_approvals",
        "get_release_logs",
    ):
        skill = get_skill(name)
        assert skill is not None, name
        assert skill.risk == "read"
        assert skill.confirm is False

    db = _memory_db()
    listed = _list_platform_tools(db, ME, {"keyword": "通知"})
    names = {item["name"] for item in listed["tools"]}
    assert "list_notifications" in names
    assert "get_notification" in names
