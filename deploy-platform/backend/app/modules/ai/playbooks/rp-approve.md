---
name: rp-approve
display_name: 审批生产发布
description: >-
  列出并处理当前用户能拍板、仍停在 pending 的生产发布。
  普通人看派给自己的；管理员看全部待办。
  在有待我点的发布、通过或驳回这次上线时使用。
  不是审批执行权申请，也不是项目经理只读工作台。
---

# 审批生产发布

只处理**生产发布**审批。驳回必须带原因。

## Workflow

1. 查看待审：`list_pending_approvals`（只读）。
2. 通过或驳回：`propose_approve`。`approved=false` 时 `comment` 必填。
3. 确认卡点过后才生效。不要口头说已经通过。

## Do not use

- 待我批的执行权申请 → `rp-access` 的 `list_pending_access_applications` / `propose_review_access`
- 本周计划、卡在谁、同步给业务（不点通过）→ `rp-pm`
- 发起发布 → `rp-release`

## Examples

**有哪些待我审批的发布 / 有没有要我点的生产发布** → `list_pending_approvals`

**同意这次生产发布 / 驳回这次上线** → `propose_approve`
