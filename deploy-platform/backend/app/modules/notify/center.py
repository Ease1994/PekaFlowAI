"""统一通知中心：业务只调用 emit / on_release_finished，不直接发渠道。"""
from __future__ import annotations

import contextlib
import logging
import threading

from sqlalchemy.orm import Session

from app.modules.notify.channels import deliver_email, deliver_in_app
from app.modules.pm.wecom import deliver_wecom
from app.modules.notify.events import spec_of
from app.modules.notify.service import reviewers_of_group

logger = logging.getLogger(__name__)

RELEASE_EVENTS = {
    "success": "release.success",
    "failed": "release.failed",
    "cancelled": "release.cancelled",
    "rolled_back": "release.rolled_back",
}
RELEASE_LABEL = {
    "success": "成功",
    "failed": "失败",
    "cancelled": "已取消",
    "rolled_back": "已回滚",
}

# 「查是否已发过」和「写入」之间隔着一次数据库往返，同一条发布可能被多处几乎同时
# 判定为结束（Agent 上报完成、任务状态同步各走各的），两个线程双双查不到再双双写入，
# 用户就收到两条一模一样的通知。把这一段串起来，去重查询才真的起作用。
_dedupe_lock = threading.Lock()


def emit(
    db: Session,
    event: str,
    *,
    user_ids: list[int] | None = None,
    group_reviewers: int | None = None,
    exclude_user_id: int | None = None,
    title: str,
    content: str,
    link: str = "",
    related_id: int | None = None,
    related_type: str | None = None,
    channels: list[str] | None = None,
    dedupe_key: str = "",
    commit: bool = False,
) -> int:
    """投递一条事件。返回实际写入站内信的人数。

    收件人：user_ids 和/或某分组审批人。渠道默认取事件目录。
    commit=False 时由调用方随业务事务提交；邮件在 commit 之后再发。
    """
    spec = spec_of(event)
    recipients = _recipients(db, user_ids, group_reviewers, exclude_user_id)
    if not recipients:
        return 0
    chans = tuple(channels) if channels else spec.channels
    rel_type = related_type if related_type is not None else spec.related_type
    pending_mail: list[tuple[str, str, str, str]] = []
    pending_wecom: list[tuple[int, str, str, str]] = []
    written = 0
    from app.modules.auth.models import User

    # 去重靠「先查再写」，而两个线程各拿各的 session，没提交前彼此看不见对方写的行。
    # 所以要把查、写、提交整段串起来才拦得住重复。拿不到 commit 权的调用方由它自己的
    # 事务边界负责，这里管不着，也就不必上锁
    guard = _dedupe_lock if (commit and dedupe_key) else contextlib.nullcontext()
    with guard:
        for uid in recipients:
            key = f"{dedupe_key}:{uid}" if dedupe_key else ""
            if "in_app" in chans:
                row = deliver_in_app(
                    db,
                    uid,
                    title=title,
                    content=content,
                    kind=event,
                    link=link,
                    related_type=rel_type,
                    related_id=related_id,
                    dedupe_key=key,
                )
                if row is not None:
                    written += 1
                elif key:
                    continue
            if "email" in chans:
                u = db.get(User, uid)
                if u is not None:
                    pending_mail.append((u.email or "", title, content, link))
            if "wecom" in chans:
                pending_wecom.append((uid, title, content, link))
        if commit:
            db.commit()
    # 邮件 / 企微走网络，慢，不该占着去重锁
    if commit:
        for addr, subj, body, href in pending_mail:
            deliver_email(addr, subj, body, href)
    # 企微不跟 commit 走：审批待办的 emit 都是嵌在业务事务里、由外层提交，
    # 等 commit=True 才推的话，手机永远收不到「待你确认」
    for uid, subj, body, href in pending_wecom:
        deliver_wecom(db, uid, subj, body, href)
    return written


def _recipients(
    db: Session,
    user_ids: list[int] | None,
    group_reviewers: int | None,
    exclude_user_id: int | None,
) -> list[int]:
    ids: set[int] = set()
    for uid in user_ids or []:
        if uid:
            ids.add(int(uid))
    if group_reviewers:
        ids.update(reviewers_of_group(db, int(group_reviewers), exclude_user_id=exclude_user_id))
    elif exclude_user_id:
        ids.discard(int(exclude_user_id))
    return sorted(ids)


def on_release_finished(release_id: int) -> None:
    """发布进入终态后由流水线/Agent 调用；后台组文案并 emit，不阻塞回调。"""
    threading.Thread(
        target=_emit_release,
        args=(int(release_id),),
        name=f"notify-release-{release_id}",
        daemon=True,
    ).start()


def _emit_release(release_id: int) -> None:
    from app.db.session import SessionLocal
    from app.modules.project.models import Group
    from app.modules.pipeline.models import Pipeline, Release

    try:
        with SessionLocal() as db:
            r = db.get(Release, release_id)
            if r is None or not r.operator_id:
                return
            event = RELEASE_EVENTS.get(r.status)
            if not event:
                return
            from app.modules.ai.followup import format_one

            pipe = db.get(Pipeline, r.pipeline_id)
            group = db.get(Group, r.group_id) if r.group_id else None
            group_type = (group.type or "").lower() if group else ""
            channels = _release_channels(group_type, r.status)
            if not channels:
                return
            name = pipe.name if pipe else f"流水线 #{r.pipeline_id}"
            title = (
                f"发布{RELEASE_LABEL.get(r.status, r.status)}："
                f"{name} #{r.build_number or r.id}"
            )
            emit(
                db,
                event,
                user_ids=[r.operator_id],
                title=title,
                content=format_one(db, r),
                link=f"/executions/{r.pipeline_id}/{r.id}",
                related_id=r.id,
                channels=channels,
                dedupe_key=f"{event}:{r.id}",
                commit=True,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("发布通知失败 #%s：%s", release_id, e)


def _release_channels(group_type: str, status: str) -> list[str]:
    """按环境控制发布终态通知。

    - 生产 / UAT / 预发 / 自定义环境：所有终态都发站内信 + 邮件
    - 测试 / 开发：仅 failed 发站内信，不发邮件

    只认 prod 的话，UAT 上线成功没人收到通知，而 UAT 恰恰是要通知业务方验收的那一套。
    """
    from app.core.env import is_disposable

    if not is_disposable(group_type):
        return ["in_app", "email"]
    if status == "failed":
        return ["in_app"]
    return []
