import { memo } from 'react'
import { Handle, Position } from 'reactflow'
import { useT } from '@/i18n'

/** 阶段标签节点：挂在该阶段第一步左侧，表示编排从哪一段开始。 */
function ExecStageNode({ data }: { data: { name: string; status: string } }) {
  const t = useT()
  const failed = data.status === 'failed'
  return (
    <div
      style={{
        width: 120,
        padding: '10px 12px',
        borderRadius: 10,
        background: failed ? '#fff1f0' : '#fff',
        border: `1px solid ${failed ? '#ffa39e' : '#e5e7eb'}`,
        boxShadow: '0 4px 12px rgba(15,23,42,0.04)',
      }}
    >
      <Handle type="source" position={Position.Right} style={{ background: '#94a3b8', width: 8, height: 8 }} />
      <div style={{ fontSize: 11, color: '#9ca3af', letterSpacing: 1 }}>{t('common.stage')}</div>
      <div style={{ fontSize: 13, fontWeight: 700, color: '#111827', marginTop: 2 }}>{data.name}</div>
    </div>
  )
}

export default memo(ExecStageNode)
