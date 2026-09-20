---
name: rp-plugin-draft
display_name: 写插件草稿
description: >-
  起草流水线步骤插件并存为待审草稿，源码会在构建机执行。
  在要给编排器加一个步骤、写插件代码、企业微信/对象存储这类插件时使用。
  不是 AI Agent 技能包，不会安装，流水线里还选不到。
---

# 写流水线插件草稿

草稿属于起草人。试跑需要该流水线的执行权。发布到全局仓库并安装进编排器仍要管理员。

## Workflow

1. 立刻调用 `propose_plugin_draft`，源码放进 `files`。不要口头说「已保存」却不调用。
2. Python 用 `import release_atom_sdk as sdk`：`sdk.get_input()`、`sdk.log.info()`、`sdk.set_output()`。回调平台用 `sdk.get_task_token()`，不要读 `RELEASE_AGENT_TOKEN`。
3. 按能力命名。查询类插件必须有 `status` 下拉：`all` / `running` / `success` / `failed` / `cancelled` / `rejected`，代码按参数过滤，禁止写死一种状态，禁止叫 `query-running-pipelines`。
4. 不要编造接口。任务凭证只能打 `/api/v1/plugin-api/*`。
5. 只体检不落库用 `lint_plugin_source`；看自己的草稿用 `list_plugin_drafts`。

## Do not use

- 给助手写 SKILL.md、实现 agent 技能 → `rp-agent-skill`
- 当前就要发布或查状态 → `rp-release` / `rp-observe`

## Constraints

- 不要说「已上线」「可以直接用了」。
- 不要把插件做成只能查 running。
