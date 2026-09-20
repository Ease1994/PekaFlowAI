---
name: rp-access
display_name: 申请执行权
description: >-
  提交流水线权限或项目角色审核单，以及查询、作废、审批这些申请。
  默认申请查看+执行；也可以申请编辑、删除、审批、豁免或全部权限。
  用户点名「某某角色」时申请加入该角色。
  在要申请权限、没有权限、要执行权、申请角色、撤销尚未通过的申请、审批权限单时使用。
  申请只是送审，不是立刻授权，也不是去发布。
---

# 申请流水线执行权 / 项目角色

申请通过后默认有查看 + 执行。用户点名编辑、删除、审批、豁免或「全部权限」时，把对应 `actions` 带上。点名角色时用 `apply_project_role`，不要改成流水线动作。不要说已经有权限、已经能发。

## Workflow

1. 先定范围，再调对应工具，禁止对流水线循环 `apply_pipeline_execute`：
   - 整个项目（含生产、含以后新建的流水线）→ 一次 `apply_project_execute`
   - 某个环境分组（测试 / 生产等）→ 一次 `apply_group_execute`
   - 只要某一条流水线 → 立刻 `apply_pipeline_execute`
   - 点名项目角色（「申请某某角色」）→ 一次 `apply_project_role`
2. 范围不清楚时 `list_access_catalog` **只调一次**。工具会把「整项目 / 某个环境」问出来；禁止换关键字再搜。用户下一句选范围后再 `apply_*`。其它任务对象不清时用 `ask_user`，不要猜。
3. 查自己交过的单 → `list_my_access_applications`（只读，不会作废）。
4. 作废尚未通过的申请（收回、撤回、撤销、取消都是同一件事）→ `cancel_access_application`。无单号时处理待审，多张则列出。
5. 待我批的执行权 → `list_pending_access_applications`；通过或驳回 → `propose_review_access`。
6. 已经有更大范围权限、已是该角色成员、或已有待审单时，工具会返回错误。不要改去逐条重试。

## Do not use

- 发布 / 部署 / 上线 / 发一下 → `rp-release`。句子里带流水线名也不是申请权限。
- 往节点拷文件 → `rp-node-push`
- 停掉正在跑的发布 → `rp-release-control` 的 `propose_cancel`
- 待审的生产发布（不是权限单）→ `rp-approve`

## Examples

**我要整个项目的执行权** → `apply_project_execute`

**只要测试环境的执行权** → `apply_group_execute`

**申请订单测试流水线的执行权限** → `apply_pipeline_execute`

**申请订单项目的开发者角色** → `apply_project_role`

**取消这次权限申请 / 那张申请不要了** → `cancel_access_application`
