---
name: rp-agent-skill
display_name: 写 Agent 技能
description: >-
  起草一份还不在库里的 AI Agent 技能包：只有 SKILL.md，没有可执行代码。
  在要给助手加技能、写 SKILL.md、实现 agent 技能时使用。
  不是流水线步骤插件，也不要先去查发布状态。
---

# 写 AI Agent 技能包

技能包教模型在什么任务上调用**已经存在的内置工具**。没有代码，不能改平台数据。点一次黄色确认按钮即安装。

## Workflow

1. 立刻调用 `propose_agent_skill`（`name` / `display_name` / `description` / `skill_md`）。不必先口头复述方案再等点头。
2. `description` 写给目录看：做什么 + 何时用 + 不是什么，第三人称，带检索关键词。不要写成「我可以帮你…」。
3. `skill_md` 按技能约定结构写：
   - YAML frontmatter：`name`、`description`（做什么 + 何时用 + 不是什么）
   - `# 标题` + `## Workflow` 逐步调用哪些已有工具
   - `## Do not use` 和易混技能/工具划界
   - `## Examples` 具体输入 → 调哪个工具，不是口吻白名单
4. 按**能力**命名，不要按例句收窄。查询发布状态要能查成功 / 失败 / 取消 / 驳回 / 进行中，禁止做成只能查 running，禁止叫 `query-running-releases`。
5. 查状态类技能：调用一次 `get_release_status`。按当句传 `status`；没说筛哪种、也没说要历史时不传 status，只查最近一次。`status=all` 不是全量历史。不要 `list_pipelines`。
6. 工具返回 error / 没有确认卡时，把失败原因原样告诉用户。禁止写「请点击黄色确认按钮」，也不要口头说已经保存。
7. 管理员安装全员可见；普通用户只装到自己的助手。

## Do not use

- 流水线里多一个步骤、写 `task.py` → `rp-plugin-draft`
- 当前就是在问发布状态，并没有要加技能 → `rp-observe`
- 库里已有的技能要安装 / 卸载 / 停用 / 启用 → `rp-skill-lifecycle`
- 助手要调一个平台还没有的 API → 第三方工具包，不是技能包

## Constraints

- 禁止 `release_atom_sdk`、`task.py`、`task.json`。
- 不要发明工具名。只能教模型调用已有工具。
- 不要占用 `rp-*` 内置名。
- 确认只靠页面黄色按钮，禁止写「回复确认即提交」。
