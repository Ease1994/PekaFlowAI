---
name: rp-observe
display_name: 查询发布状态
description: >-
  查询发布跑得怎样：最近一次、正在发布、成功失败、执行日志。
  在问状态、发完了没、正在发布的有哪些、看日志时使用。
  只读，不是发起发布，也不是列流水线目录。
---

# 查询发布状态

点名流水线时只查**最近一次执行**。没说要历史、最近几次，不要把执行记录全拉出来，也不要先 `list_pipelines`。

## Workflow

1. 调用一次 `get_release_status`。点名流水线用 `keyword`，不要先列目录、不要对每条再 `get_pipeline`。
2. 按当句传 `status`，没说筛哪种就不要传：
   - 正在发布 / 在跑 → `running`
   - 失败 → `failed`
   - 成功 → `success`
   - 取消 → `cancelled`
3. 只要失败清单 → `list_failed_releases`。
4. 只要现场日志原文 → `get_release_logs`。分析原因改加载 `rp-diagnose`。
5. `status=all` 不是把历史全拉出来。

## Do not use

- 要去发布 / 部署 / 上线 → `rp-release`
- 为什么失败、帮我诊断 → `rp-diagnose`
- 本周计划、卡在谁、同步给业务 → `rp-pm`
- 给助手加一个「查状态」技能 → `rp-agent-skill`（当前这一句如果只是在问状态，直接查，不要起草）

## Examples

**查一下 test-C 的状态 / 发完了没** → `get_release_status`（keyword=test-C），不传 status

**现在正在发布的有哪些** → `get_release_status`（status=running）

**把这次失败的日志发我** → `get_release_logs`
