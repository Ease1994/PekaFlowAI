"""当前用户的只读收件箱：站内通知。

这些 HTTP 接口本来就有（铃铛在用），只是没进助手 Tool 目录，
模型无法调用，技能包也教不了。这里按「当前登录用户、只读自己的」挂进去，
不标已读、不投递、不改别人的数据。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.ai.registry import Skill, register
from app.modules.notify import service as inbox

# 列表里不再带正文。完整篇幅用 get_notification，避免模型把收件箱当空、也避免轨迹被裁成字符串。
_MAX_LIST = 30
_FALSE_FLAGS = {"false", "0", "no", "off", "none", "null"}
_TRUE_FLAGS = {"true", "1", "yes", "on"}


def coerce_flag(value: object, *, default: bool) -> bool:
    """工具参数里的开关。模型常把 false 写成字符串，bool("false") 在 Python 里是 True。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _FALSE_FLAGS:
        return False
    if text in _TRUE_FLAGS:
        return True
    return default


def format_list_reply(result: dict) -> str:
    """用户看见的通知列表只来自这次查询。

    未读查询：unread_count 为 0 或列表里没有未读，固定回复没有未读，禁止把已读标题列出来。
    全部通知：可以列出已读，标题写成「站内通知」，不要写成未读。
    """
    raw = result.get("notifications") if isinstance(result, dict) else None
    items = [item for item in (raw or []) if isinstance(item, dict)]
    unread_only = coerce_flag(result.get("unread_only"), default=True)
    try:
        unread_n = int(result.get("unread_count"))
    except (TypeError, ValueError):
        unread_n = len([item for item in items if not item.get("is_read")])
    if unread_only:
        # 再滤一遍 is_read，防止工具参数被写成 false 后，调用方仍按未读来展示。
        items = [item for item in items if not item.get("is_read")]
        if unread_n <= 0 or not items:
            return "当前没有未读消息。"
        lines = [f"未读通知共 {len(items)} 条："]
    else:
        if not items:
            return "当前没有站内通知。"
        lines = [f"站内通知共 {len(items)} 条："]
    for item in items:
        nid = item.get("id") or item.get("notice_id") or ""
        title = str(item.get("title") or "").strip() or "（无标题）"
        url = str(item.get("detail_url") or "").strip()
        line = f"- #{nid} {title}".strip()
        if url:
            line = f"{line}  {url}"
        lines.append(line)
    return "\n".join(lines)


def _row_public(row: dict, *, full: bool) -> dict:
    """整理成给模型看的字段。只含收件人自己的那条。

    list 只给标题和 detail_url；正文留给 get_notification。
    list 用 id；get_notification 参数叫 notice_id。两边都给，模型抄哪边都能对上。
    """
    nid = row.get("id")
    out = {
        "id": nid,
        "notice_id": nid,
        "title": row.get("title") or "",
        "kind": row.get("kind") or "",
        "link": row.get("link") or "",
        "detail_url": f"/notifications?id={nid}" if nid else "/notifications",
        "is_read": bool(row.get("is_read")),
        "created_at": row.get("created_at") or "",
    }
    if full:
        out["content"] = str(row.get("content") or "")
    return out


def _list_notifications(db: Session, current, params: dict) -> dict:
    """列出当前用户的站内通知，默认未读。"""
    unread_only = coerce_flag(params.get("unread_only"), default=True)
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _MAX_LIST))
    rows = inbox.list_mine(
        db,
        current.id,
        unread_only=unread_only,
        read_only=False,
        limit=limit,
        offset=0,
    )
    unread = inbox.unread_count(db, current.id)
    notices = [_row_public(r, full=False) for r in rows]
    if unread_only:
        notices = [item for item in notices if not item.get("is_read")]
    return {
        "notifications": notices,
        "unread_count": unread,
        "unread_only": unread_only,
        "hint": "" if notices else ("没有未读通知" if unread_only else "没有站内通知"),
    }


def _get_notification(db: Session, current, params: dict) -> dict:
    """读自己的一条通知全文。"""
    try:
        notice_id = int(params.get("notice_id") or params.get("id") or 0)
    except (TypeError, ValueError):
        notice_id = 0
    if not notice_id:
        return {"error": "缺少 notice_id，可先 list_notifications"}
    row = inbox.get_one(db, current.id, notice_id)
    if row is None:
        return {"error": f"通知 #{notice_id} 不存在，或不是你的收件箱"}
    return {"notification": _row_public(row, full=True)}


def load() -> None:
    register(
        Skill(
            name="list_notifications",
            description=(
                "列出当前用户站内通知的标题和详情链接。默认只返回未读。"
                "在问未读、铃铛、通知中心、有没有消息时调用。"
                "不是装/卸通知技能。完整正文用 get_notification。"
            ),
            category="observe",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "unread_only": {
                        "type": "boolean",
                        "description": "默认 true。查未读时不要传 false，否则会把已读也列成未读。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20，最大 30",
                    },
                },
            },
            handler=_list_notifications,
            examples=["有哪些未读通知", "通知中心里有什么", "铃铛里有消息吗", "查一下未读"],
        )
    )
    register(
        Skill(
            name="get_notification",
            description="读取当前用户某一条站内通知的完整正文。在要点开某条、展开全文时调用。不是列未读标题，改用 list_notifications。",
            category="observe",
            risk="read",
            parameters={
                "type": "object",
                "properties": {
                    "notice_id": {"type": "integer", "description": "通知 id"},
                },
                "required": ["notice_id"],
            },
            handler=_get_notification,
            examples=["把第 12 条通知完整内容发我", "展开这条通知"],
        )
    )
