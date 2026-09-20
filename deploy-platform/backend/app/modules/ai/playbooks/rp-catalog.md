---
name: rp-catalog
display_name: 查看目录
description: >-
  列出可见的项目、环境分组、流水线、代码仓库和已安装的流水线步骤插件。
  在问有哪些项目、有哪些流水线、代码库、编排器能用哪些插件时使用。
  不是发起发布、申请权限或查发布状态。
---

# 查看目录

只读。列一次即可，不要对返回的每一条再 `get_pipeline`。

## Workflow

1. 项目清单 → `list_projects`
2. 某项目有哪些环境 → `list_groups`
3. 流水线清单 → 一次 `list_pipelines`（可用 `keyword` / `project` / `group_type` 收窄）
4. 代码仓库 → `list_repositories`
5. 编排器步骤插件 → `list_plugins`（不是助手技能包）
6. 只要核对单条流水线怎么配、要不要审批 → `get_pipeline`（必须已有 id）

## Do not use

- 要去发布 / 部署 / 上线 → `rp-release`
- 问发得怎样了 → `rp-observe`
- 申请执行权 → `rp-access`
- 助手装了哪些技能 → `list_agent_skills`

## Examples

**有哪些流水线 / 测试环境流水线** → `list_pipelines`

**有哪些项目 / 有哪些代码库** → `list_projects` / `list_repositories`
