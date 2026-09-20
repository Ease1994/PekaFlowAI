---
name: rp-release
display_name: 发布流水线
description: >-
  发起一次流水线发布，生成确认卡，点确认后才执行。
  在要发布、部署、上线、发到某环境、跑流水线时使用。
  不是申请执行权，也不是把文件拷到节点。
---

# 发布流水线

一次发布 = 一条流水线 + 一个代码版本。会执行这条流水线编排好的全部步骤，不能临时指定「只发某一个文件」。没有滚动 / 蓝绿 / 灰度可选，不要编造这些字段。

## Workflow

1. 从原话抽出 `project` / `env` / `pipeline`。抽不准就把整句放进 `query`。
2. 立刻调用 `propose_release`。禁止编造 `pipeline_id`。不要先 `list_projects` / `list_pipelines` / `skill` 来凑 id。
3. 工具只认可见流水线：唯一则出确认卡，多条则列出真实 id 让人再选，找不到会说明原因。
4. 没有执行权限时把错误原文转达，再加载 `rp-access`（`apply_pipeline_execute`）。不要一上来把发布理解成申请权限。
5. 确认卡就是确认。回「确认 / 好的 / 是 / 提交」时完成未完成的卡片。不要说「已经发布成功」。
6. 回复只写真正传给工具的参数。

## Do not use

- 申请权限、没有权限、要执行权 → `rp-access`
- 把上传的文件拷到某台机器 → `rp-node-push`
- 只要流水线目录 → 一次 `list_pipelines`
- 停掉正在跑的、回滚已成功的、按原 commit 重跑 → `rp-release-control`
- 问发得怎样了、失败原因、日志 → `rp-observe` / `rp-diagnose`

## Examples

**把订单服务发到测试** → `propose_release`（project / env=test / pipeline，或 `query`=原话）

**帮我跑一下陪练的生产** → `propose_release`（`query`=原话）

**发布到生产 v1.2.3** → `propose_release`（env=prod, version=v1.2.3）
