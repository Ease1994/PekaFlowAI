---
name: rp-diagnose
display_name: 诊断失败发布
description: >-
  对失败发布做诊断，只分析失败步骤日志并给出原因。
  在问为什么挂了、失败原因、帮我看看这次发布时使用。
  只要日志原文用 rp-observe；不要据此直接发布或回滚。
---

# 诊断失败发布

结论来自失败步骤日志，不要猜步骤名，也不要编一套业务原因。

## Workflow

1. 没有 `release_id` 时先 `get_release_status` 或 `list_failed_releases` 定位最近失败。
2. 调用 `diagnose_release`。
3. 用户只要原文不要结论时，改 `get_release_logs`。
4. 诊断完如果对方要重跑或回滚，再加载 `rp-release-control`。未要求时不要主动发起。

## Do not use

- 只问发得怎样、正在发布有哪些 → `rp-observe`
- 直接 Rebuild / 回滚（还没问原因）→ 仍先诊断，除非对方已经明确要重跑或撤回去

## Examples

**帮我看看这次失败原因 / 为什么挂了** → `diagnose_release`（有 id 就带上，没有先查最近失败）
