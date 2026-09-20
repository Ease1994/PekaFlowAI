"""通知中心 API：收件箱 + 统一入站事件。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user
from app.core.response import BizException, R
from app.db.session import get_db
from app.modules.notify import emit
from app.modules.notify.events import CATALOG
from app.modules.notify import service as inbox

router = APIRouter(tags=["通知中心"])


@router.get("/notifications", summary="我的站内通知")
def list_notifications(
    unread_only: bool = Query(default=False),
    read_only: bool = Query(default=False),
    kind: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok(
        inbox.list_mine(
            db,
            current.id,
            unread_only=unread_only,
            read_only=read_only,
            kind=kind,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/notifications/unread-count", summary="未读通知数")
def unread_count(db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    return R.ok({"count": inbox.unread_count(db, current.id)})


@router.get("/notifications/events", summary="已登记的通知事件")
def list_events(_: CurrentUser = Depends(get_current_user)):
    return R.ok(
        [
            {
                "event": s.event,
                "channels": list(s.channels),
                "related_type": s.related_type,
                "description": s.description,
            }
            for s in CATALOG.values()
        ]
    )


@router.post("/notifications/events", summary="向通知中心投递事件")
def post_event(
    body: dict,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    """统一入站。非管理员只能发给自己。"""
    event = str(body.get("event") or "").strip()
    title = str(body.get("title") or "").strip()
    content = str(body.get("content") or "")
    if not event or not title:
        raise BizException.bad_request("event、title 必填")
    raw_ids = body.get("user_ids") or []
    if not isinstance(raw_ids, list):
        raise BizException.bad_request("user_ids 须为数组")
    user_ids = [int(x) for x in raw_ids if x]
    if not current.is_admin:
        user_ids = [current.id]
    elif not user_ids:
        user_ids = [current.id]
    rid = body.get("related_id")
    if rid is not None and rid != "":
        try:
            rid = int(rid)
        except (TypeError, ValueError) as e:
            raise BizException.bad_request("related_id 须为整数") from e
    else:
        rid = None
    ch = body.get("channels")
    if ch is not None and not isinstance(ch, list):
        raise BizException.bad_request("channels 须为数组")
    n = emit(
        db,
        event,
        user_ids=user_ids,
        title=title,
        content=content,
        link=str(body.get("link") or ""),
        related_id=rid,
        related_type=body.get("related_type"),
        channels=ch,
        dedupe_key=str(body.get("dedupe_key") or ""),
        commit=True,
    )
    return R.ok({"written": n, "event": event})


@router.post("/notifications/read-all", summary="全部标为已读")
def read_all(db: Session = Depends(get_db), current: CurrentUser = Depends(get_current_user)):
    return R.ok({"updated": inbox.mark_read(db, current.id)})


@router.get("/notifications/{notice_id}", summary="通知详情")
def get_notification(
    notice_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    row = inbox.get_one(db, current.id, notice_id)
    if row is None:
        raise BizException.not_found("通知")
    return R.ok(row)


@router.post("/notifications/{notice_id}/read", summary="单条标为已读")
def read_one(
    notice_id: int,
    db: Session = Depends(get_db),
    current: CurrentUser = Depends(get_current_user),
):
    return R.ok({"updated": inbox.mark_read(db, current.id, notice_id)})
