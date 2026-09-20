/** 发布自身状态。明细顶栏必须认这个，不能从步骤树猜。 */
import { t } from '@/i18n'

/** 状态码 → Tag 颜色。句子不写死在这里，切语言时走 t()。 */
const RELEASE_STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  queued: 'cyan',
  assigned: 'processing',
  running: 'processing',
  success: 'success',
  failed: 'error',
  rejected: 'error',
  rolling_back: 'warning',
  rolled_back: 'default',
  cancelled: 'default',
}

/** 状态码 → i18n 键。rolling_back 这类下划线码在 Messages 里用驼峰。 */
const RELEASE_STATUS_I18N: Record<string, string> = {
  pending: 'status.pending',
  queued: 'status.queued',
  assigned: 'status.assigned',
  running: 'status.running',
  success: 'status.success',
  failed: 'status.failed',
  rejected: 'status.rejected',
  rolling_back: 'status.rollingBack',
  rolled_back: 'status.rolledBack',
  cancelled: 'status.cancelled',
}

/**
 * 颜色固定、句子按当前界面语言现取。
 * 组件若把结果 memo 住，deps 里要带 t 或 locale，否则切语言不会刷新。
 */
export function releaseStatusMeta(status?: string | null): { color: string; text: string } {
  if (status && RELEASE_STATUS_COLOR[status]) {
    return { color: RELEASE_STATUS_COLOR[status], text: t(RELEASE_STATUS_I18N[status]) }
  }
  return { color: 'default', text: status || t('status.unknown') }
}

/**
 * 旧调用点用 RELEASE_STATUS_META[code]。
 * 每次读取都现译，避免模块加载时把中文冻进对象。
 */
export const RELEASE_STATUS_META: Record<string, { color: string; text: string }> = new Proxy(
  {},
  {
    get(_target, prop: string) {
      if (prop in RELEASE_STATUS_COLOR) return releaseStatusMeta(prop)
      return undefined
    },
  },
)

/** 明细/抽屉顶栏：优先用 sequence.status。启动失败时步骤全是 pending，从树猜会变成「已完成」。 */
export function sequenceHeaderMeta(seq: {
  status?: string
  is_running?: boolean
  stages?: { status: string }[]
}): { color: string; text: string } {
  if (seq.status) return releaseStatusMeta(seq.status)
  if (seq.is_running) return releaseStatusMeta('running')
  const stages = seq.stages || []
  if (stages.length && stages.every((s) => s.status === 'success')) return releaseStatusMeta('success')
  if (stages.some((s) => s.status === 'failed')) return releaseStatusMeta('failed')
  return { color: 'default', text: t('status.done') }
}
