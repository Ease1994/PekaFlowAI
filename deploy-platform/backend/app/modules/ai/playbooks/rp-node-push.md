---
name: rp-node-push
display_name: 节点文件下发
description: >-
  把对话框里已上传的文件发到一台或多台部署节点的指定目录。
  在要传到某台机器、拷到节点、按 IP 下发文件时使用。
  这是绕开流水线直接写磁盘，不是发布，也不是申请执行权。
---

# 节点文件下发

没有附件不可调用。目标目录必须落在节点 `allow_paths` 之内。

## Workflow

1. `list_push_nodes` 拿到真实 `node_id` 和允许写入的目录，禁止编造。keyword 可匹配机器名、IP、分组。
2. 用 IP 指代机器时，把 IP 当 `keyword` 查一次。
3. 按批次指代（如「O2O 生产那几台」）时，用 keyword 查该分组，把查到的整组填进 `node_ids`。结果被截断就换更准的关键字再查，不要在没列全时替对方挑机器。
4. 要发多台就把 `node_ids` 一次填全，一张确认卡发完，不要一台调一次。
5. `target_dir` 原样照抄给出的绝对目录。严禁截短成 `allow_paths` 根目录，严禁因为换了机器就改目录。没说目录就问。
6. 目录超出允许范围时如实说明并停下来问，不许自己挑一个能过的目录。
7. `attachment_ids` 只用对话里已上传的附件 id。
8. 查不到时 hint 会写明是没登记、登记成了构建机、还是没授权。照实转达。

## Do not use

- 发布 / 部署 / 上线流水线 → `rp-release`
- 申请流水线执行权 → `rp-access`
- 只问构建机在不在线 → `list_agents`（那是编译机，不是部署节点）

## Examples

**把这些文件传到 srvt4 的 D:\wwwroot\site 下** → `list_push_nodes`（keyword=srvt4）再 `propose_node_push`

**传到 192.0.2.10** → `list_push_nodes`（keyword=192.0.2.10）再 `propose_node_push`
