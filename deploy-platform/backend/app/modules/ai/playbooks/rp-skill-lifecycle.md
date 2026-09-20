---
name: rp-skill-lifecycle
display_name: 安装或卸载技能
description: >-
  安装、卸载、停用或启用已经存在的 Agent 技能包，出确认卡。
  在要装上、卸掉、关掉、打开某份技能时使用。
  不是去使用该技能的能力（例如查未读），也不是起草一份新的。
---

# 安装或卸载技能

改的是技能包的安装状态，不是去执行那份说明书里的任务。

## Workflow

1. 不确定库里有没有时，可以先 `list_agent_skills`。
2. 调用 `propose_agent_skill_lifecycle`：
   - 卸掉 → `action=uninstall`
   - 停用但仍保留 → `action=disable`
   - 启用，或把已卸载的再装上 → `action=enable`
3. `name` 填技能标识或中文名。对不上时把原话放进 `intent`，例如「消息通知查看」应对上「读取未读通知」。
4. 确认卡点一次即完成。

## Do not use

- 查未读、看铃铛 → `rp-inbox`
- 起草一份还不在库里的技能 → `rp-agent-skill`
- 写流水线插件 → `rp-plugin-draft`

## Examples

**安装一下读取未读通知** → `propose_agent_skill_lifecycle`（action=enable）

**卸载消息通知查看这个技能 / 把未读通知技能卸了** → `propose_agent_skill_lifecycle`（action=uninstall）

**停用查询流水线状态** → `propose_agent_skill_lifecycle`（action=disable）
