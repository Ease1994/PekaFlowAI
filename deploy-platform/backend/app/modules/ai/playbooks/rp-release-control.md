---
name: rp-release-control
display_name: 停发 / 回滚 / 重跑
description: >-
  控制已经存在的发布：停掉正在跑的、把已成功的退回上一版、按原 commit 再跑一次。
  在要取消这次发布、中止构建、回滚、Rebuild 时使用。
  不是作废权限申请，也不是新开一条不同版本的发布。
---

# 停发 / 回滚 / 重跑

这三件事都针对**已有的 release_id**，出确认卡后才执行。先弄清要改的是哪一次发布。

## Workflow

1. 没有 `release_id` 时，先 `get_release_status`（点名流水线用 `keyword`）拿到最近一次。
2. 按意图选工具，一次只做一件：
   - 正在跑、要停掉 → `propose_cancel`
   - 已经成功、要退回上一版 → `propose_rollback`
   - 失败了、按原 commit 再来一次 → `propose_rebuild`
3. 确认卡点过后才算执行。不要口头说已经停掉或已经回滚。

## Do not use

- 作废尚未通过的执行权申请（收回、撤回、撤销、取消申请）→ `rp-access` 的 `cancel_access_application`
- 新开一条发布、换版本再发 → `rp-release`
- 只要看状态或日志 → `rp-observe`

## Examples

**取消这次发布 / 停掉正在跑的流水线 / 发布不要跑了** → `propose_cancel`

**回滚上一笔 / 这次发错了撤回去** → `propose_rollback`

**把失败的那次 Rebuild / 按原提交重跑** → `propose_rebuild`
