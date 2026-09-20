---
name: rp-inbox
display_name: 站内通知
description: >-
  查看当前用户的站内通知：默认只列未读标题，点名某条再展开正文。
  在问未读、铃铛、通知中心、消息时使用。
  不是安装或卸载「读取未读通知」技能。
---

# 站内通知

只读自己的收件箱。不标已读、不投递、不改别人的数据。

## Workflow

1. 列标题：`list_notifications`。默认 `unread_only=true`。查未读时不要传 `false`。
2. 回复只列标题和详情链接，不要把正文整段贴出来。
3. 点名某一条再 `get_notification`（`notice_id` 来自列表的 id）。
4. 没有未读就说没有未读，不要把已读标题列成未读。

## Do not use

- 安装 / 卸载 / 停用「读取未读通知」这类技能 → `rp-skill-lifecycle`
- 待我审批的发布或权限单 → `rp-approve` / `rp-access`

## Examples

**有哪些未读通知 / 铃铛里有消息吗** → `list_notifications`

**把第 12 条通知完整内容发我** → `get_notification`（notice_id=12）
