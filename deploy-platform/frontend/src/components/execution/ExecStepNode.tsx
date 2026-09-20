import { memo } from 'react'
import { Handle, Position } from 'reactflow'
import { CheckCircleFilled, CloseCircleFilled, LoadingOutlined, PauseCircleFilled, StopOutlined } from '@ant-design/icons'
import { useT } from '@/i18n'

/** 步骤节点上的选中回调参数，与执行页 StepPick 对齐。 */
export type CanvasStepPick = {
  taskId: number | null
  stepOrder: number
  logIndex: number
}

/** 画布步骤节点的数据。 */
export type ExecStepData = {
  order: number
  title: string
  plugin: string
  status: string
  duration: string | null
  selected: boolean
  pick: CanvasStepPick
  onSelect: (pick: CanvasStepPick) => void
}

const STATUS_BAR: Record<string, string> = {
  success: '#10b981',
  failed: '#ef4444',
  timeout: '#ef4444',
  running: '#3b82f6',
  cancelled: '#94a3b8',
  pending: '#cbd5e1',
  queued: '#cbd5e1',
}

const PLUGIN_TONE: Record<string, string> = {
  git: '#0ea5e9',
  msbuild: '#8b5cf6',
  dotnet: '#8b5cf6',
  pack: '#f59e0b',
  file: '#10b981',
  transfer: '#10b981',
  iis: '#6366f1',
  pool: '#6366f1',
}

/**
 * 按插件名取节点主色，让 git / 编译 / 发包在画布上能一眼分开。
 */
function pluginTone(plugin: string): string {
  const key = (plugin || '').toLowerCase()
  for (const [prefix, color] of Object.entries(PLUGIN_TONE)) {
    if (key.includes(prefix)) return color
  }
  return '#64748b'
}

/**
 * 执行画布上的单个步骤，样式对齐 Dify 节点：色条、标题、状态角标。
 */
function ExecStepNode({ data }: { data: ExecStepData }) {
  const t = useT()
  const bar = STATUS_BAR[data.status] || '#cbd5e1'
  const tone = pluginTone(data.plugin)
  return (
    <div
      style={{
        width: 220,
        background: '#fff',
        borderRadius: 12,
        border: data.selected ? '2px solid #1677ff' : '1px solid #e5e7eb',
        boxShadow: data.selected
          ? '0 0 0 3px rgba(22,119,255,0.18)'
          : '0 8px 24px rgba(15,23,42,0.06)',
        overflow: 'hidden',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: bar, width: 8, height: 8 }} />
      <Handle type="source" position={Position.Right} style={{ background: bar, width: 8, height: 8 }} />
      <div style={{ height: 4, background: bar }} />
      <div style={{ padding: '10px 12px 12px' }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}>
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 8,
              background: `${tone}18`,
              color: tone,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 11,
              fontWeight: 700,
              flexShrink: 0,
            }}
          >
            {data.order + 1}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: '#111827',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {data.title}
            </div>
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>{data.plugin}</div>
          </div>
          <StatusMark status={data.status} />
        </div>
        {data.duration ? (
          <div style={{ marginTop: 8, fontSize: 11, color: '#6b7280' }}>{t('common.elapsed', { d: data.duration })}</div>
        ) : data.status === 'running' ? (
          <div style={{ marginTop: 8, fontSize: 11, color: '#3b82f6' }}>{t('deploy.inProgress')}</div>
        ) : null}
      </div>
    </div>
  )
}

/** 节点右上角的成功 / 失败 / 进行中标记。 */
function StatusMark({ status }: { status: string }) {
  if (status === 'success') return <CheckCircleFilled style={{ color: '#10b981', fontSize: 16 }} />
  if (status === 'failed' || status === 'timeout') {
    return <CloseCircleFilled style={{ color: '#ef4444', fontSize: 16 }} />
  }
  if (status === 'running') return <LoadingOutlined style={{ color: '#3b82f6', fontSize: 16 }} />
  if (status === 'cancelled') return <StopOutlined style={{ color: '#94a3b8', fontSize: 16 }} />
  return <PauseCircleFilled style={{ color: '#cbd5e1', fontSize: 16 }} />
}

export default memo(ExecStepNode)
