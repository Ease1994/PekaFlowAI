import type { Pipeline, PipelineGraph } from '@/api/types'

/** 发布清单输入框的示例，和发布提交页同一套写法 */
export const MANIFEST_PLACEHOLDER = `bin/*.dll
Areas/
Views/
Content/o2o-theme.css
!bin/*.pdb`

const MANIFEST_VAR = /^\{\{\s*DEPLOY_MANIFEST\s*\}\}$|^\$\{\{\s*DEPLOY_MANIFEST\s*\}\}$/

/**
 * 流水线详情/列表要不要在执行弹窗里填发布清单。
 * 有「提取增量发布包」步骤才出框；没有该插件时忽略清单。
 */
export function pipelineNeedsManifest(p?: Pipeline | null): boolean {
  if (!p) return false
  if (p.uses_pack_incremental != null) return !!p.uses_pack_incremental
  return !!p.uses_deploy_manifest
}

/** 步骤清单是否没写死文件（空，或只引用 DEPLOY_MANIFEST）。 */
export function isManifestPlaceholder(text: string): boolean {
  const lines = (text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
  if (!lines.length) return true
  return lines.every((line) => MANIFEST_VAR.test(line))
}

/**
 * 弹窗里的清单是否还缺必填内容。
 * 有增量包、步骤未写死文件、输入又为空时必须挡住确认，不能空着发出去。
 */
export function manifestInputRequired(
  needManifest: boolean,
  stepDefault: string,
  userText: string,
): boolean {
  if (!needManifest) return false
  if ((userText || '').trim()) return false
  const preset = (stepDefault || '').trim()
  if (preset && !isManifestPlaceholder(preset)) return false
  return true
}

/**
 * 当前编排（含未保存改动）是否消费发布清单。
 * 保存并执行时要看画布上的步骤，不能只看上次保存的流水线标记。
 */
export function graphNeedsManifest(graph?: PipelineGraph | null): boolean {
  if (!graph) return false
  for (const stage of graph.stages) {
    for (const job of stage.jobs) {
      for (const step of job.steps) {
        if (step.plugin === 'pack-incremental') return true
        for (const value of Object.values(step.with || {})) {
          if (String(value).includes('DEPLOY_MANIFEST')) return true
        }
      }
    }
  }
  return false
}

/**
 * 编排里第一条增量包步骤写死的清单；仍是占位时返回空，弹窗不预填。
 */
export function graphManifestDefault(graph?: PipelineGraph | null): string {
  if (!graph) return ''
  for (const stage of graph.stages) {
    for (const job of stage.jobs) {
      for (const step of job.steps) {
        if (step.plugin !== 'pack-incremental') continue
        const text = String(step.with?.manifest ?? '')
        return isManifestPlaceholder(text) ? '' : text
      }
    }
  }
  return ''
}

/**
 * 保存并执行时，用基础设置里尚未保存的审批覆盖算出这次要不要审。
 */
export function editorApprovalRequired(
  pipeline: Pipeline | undefined,
  approvalMode: string,
): boolean {
  if (approvalMode === 'force') return true
  if (approvalMode === 'exempt') return false
  if (approvalMode === 'inherit') return !!pipeline?.group_approval_required
  return !!pipeline?.approval_required
}
