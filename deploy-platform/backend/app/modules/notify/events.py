"""通知事件目录：业务只报事件名，渠道与关联类型由这里统一分配。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventSpec:
    """一条事件的投递约定。"""

    event: str
    channels: tuple[str, ...] = ("in_app",)
    related_type: str = ""
    description: str = ""


CATALOG: dict[str, EventSpec] = {
    "access.apply": EventSpec(
        "access.apply",
        channels=("in_app",),
        related_type="permission_application",
        description="流水线执行权待审核",
    ),
    "access.review": EventSpec(
        "access.review",
        channels=("in_app",),
        related_type="permission_application",
        description="流水线执行权审批结果",
    ),
    "release.success": EventSpec(
        "release.success",
        channels=("in_app", "email"),
        related_type="release",
        description="发布成功",
    ),
    "release.approval.pending": EventSpec(
        "release.approval.pending",
        channels=("in_app", "wecom"),
        related_type="release",
        description="生产发布待审批",
    ),
    "release.approval.reviewed": EventSpec(
        "release.approval.reviewed",
        channels=("in_app",),
        related_type="release",
        description="生产发布审批结果",
    ),
    "release.approval.bypassed": EventSpec(
        "release.approval.bypassed",
        channels=("in_app", "email"),
        related_type="release",
        description="生产发布应急跳审",
    ),
    "release.failed": EventSpec(
        "release.failed",
        channels=("in_app", "email"),
        related_type="release",
        description="发布失败（含错误摘要与建议）",
    ),
    "release.cancelled": EventSpec(
        "release.cancelled",
        channels=("in_app", "email"),
        related_type="release",
        description="发布已取消",
    ),
    "release.rolled_back": EventSpec(
        "release.rolled_back",
        channels=("in_app", "email"),
        related_type="release",
        description="发布已回滚",
    ),
    "pm.confirm.pending": EventSpec(
        "pm.confirm.pending",
        channels=("in_app", "wecom"),
        related_type="release",
        description="待项目经理确认上线",
    ),
    "pm.confirm.reviewed": EventSpec(
        "pm.confirm.reviewed",
        channels=("in_app", "wecom"),
        related_type="release",
        description="项目经理确认结果",
    ),
    "pm.confirm.bypassed": EventSpec(
        "pm.confirm.bypassed",
        channels=("in_app", "wecom", "email"),
        related_type="release",
        description="应急跳审未走业务确认",
    ),
    "release.announced": EventSpec(
        "release.announced",
        channels=("in_app", "wecom", "email"),
        related_type="release",
        description="上线通报",
    ),
    "system.custom": EventSpec(
        "system.custom",
        channels=("in_app",),
        related_type="",
        description="自定义系统通知",
    ),
}


def spec_of(event: str) -> EventSpec:
    known = CATALOG.get(event)
    if known is not None:
        return known
    return EventSpec(event=event, channels=("in_app",), description="未登记事件，仅站内信")


def action_path(event: str, *, related_id: int | None = None) -> str:
    """这条通知点进去该去哪：待办事去办事页，结果去执行详情或助手。"""
    if event == "release.approval.pending":
        return f"/approvals?release_id={related_id}" if related_id else "/approvals"
    if event == "pm.confirm.pending":
        return "/approvals?tab=pm"
    if event == "access.apply":
        return f"/permissions?tab=pending&id={related_id}" if related_id else "/permissions?tab=pending"
    if event == "access.review":
        return "/permissions?tab=history"
    return ""
