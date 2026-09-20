import { memo } from 'react'
import { Handle, Position } from 'reactflow'
import { JOB_W } from '@/utils/canvasLayout'

/** 执行画布上的 Job 标签，样式对齐编辑页深色 Job 节点，只读。 */
function ExecJobNode({
  data,
}: {
  data: { id: string; name: string; agent: string; status: string }
}) {
  const failed = data.status === 'failed' || data.status === 'timeout'
  return (
    <div
      style={{
        width: JOB_W,
        padding: '8px 10px',
        borderRadius: 10,
        background: failed ? '#7f1d1d' : '#1f2937',
        border: `1px solid ${failed ? '#fecaca' : '#111827'}`,
        color: '#fff',
      }}
    >
      <Handle type="target" position={Position.Left} style={{ background: '#60a5fa', width: 8, height: 8 }} />
      <Handle type="source" position={Position.Right} style={{ background: '#60a5fa', width: 8, height: 8 }} />
      <div style={{ fontSize: 11, color: '#93c5fd' }}>{data.id}</div>
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          marginTop: 2,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {data.name}
      </div>
    </div>
  )
}

export default memo(ExecJobNode)
