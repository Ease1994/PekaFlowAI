import { memo } from 'react'
import { Handle, Position } from 'reactflow'
import { Button, Dropdown, Space, Tag, Tooltip } from 'antd'
import type { MenuProps } from 'antd'
import { PlusOutlined, DeleteOutlined, HolderOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import { agentDisplay } from '@/utils/agentLabel'
import type { BuildAgent, GraphStage, GraphJob, GraphStep, Plugin } from '@/api/types'
import { useT } from '@/i18n'

interface StageNodeProps {
  data: {
    stage: GraphStage
    plugins: Plugin[]
    onAddJob: (stageId: string) => void
    onAddStep: (stageId: string, jobId: string) => void
    onDeleteStage: (stageId: string) => void
    onDeleteStep: (stageId: string, jobId: string, stepOrder: number) => void
    onRenameStage: (stageId: string, name: string) => void
  }
  selected: boolean
}

/**
 * 蓝鲸风格的 Stage 节点：阶段标题 + 内部 Job 卡片 + Job 内 Step 列表。
 */
function StageNode({ data, selected }: StageNodeProps) {
  const t = useT()
  const { stage, plugins } = data

  const stepMenu = (stageId: string, jobId: string): MenuProps => ({
    items: plugins.map((p) => ({
      key: p.name,
      icon: <span>{p.name === 'k8s-deploy' ? '☸️' : '🔧'}</span>,
      label: `${p.display_name}（${p.category}）`,
      onClick: () => data.onAddStep(stageId, jobId),
    })),
  })

  return (
    <div
      style={{
        width: 300,
        border: selected ? '2px solid #1677ff' : '1px solid #d9d9d9',
        borderRadius: 8,
        background: '#fff',
        boxShadow: '0 2px 8px rgba(0,0,0,0.08)',
        fontSize: 13,
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />

      {/* 阶段标题 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 12px',
          borderBottom: '1px solid #f0f0f0',
          background: '#fafafa',
          borderTopLeftRadius: 8,
          borderTopRightRadius: 8,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <HolderOutlined style={{ color: '#bbb', cursor: 'grab' }} />
          <Tooltip title={t("pipe.clickRename")}>
            <span
              style={{ fontWeight: 600, cursor: 'text' }}
              onClick={() => {
                const name = window.prompt(t('pipe.stageNamePrompt'), stage.name)
                if (name) data.onRenameStage(stage.id, name)
              }}
            >
              {stage.name}
            </span>
          </Tooltip>
          <Tag color="blue" style={{ marginLeft: 4 }}>
            {t('pipe.stageN', { n: stage.order })}
          </Tag>
        </div>
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            onClick={() => data.onAddJob(stage.id)}
            title={t("pipe.addJobShort")}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() => data.onDeleteStage(stage.id)}
            title={t("pipe.deleteStageShort")}
          />
        </Space>
      </div>

      {/* Job 卡片列表 */}
      <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {stage.jobs.length === 0 && (
          <div style={{ textAlign: 'center', color: '#bbb', padding: 16 }}>
            {t('pipe.noJobsHint')}
          </div>
        )}
        {stage.jobs.map((job: GraphJob) => (
          <JobCard
            key={job.id}
            stageId={stage.id}
            job={job}
            stepMenu={stepMenu(stage.id, job.id)}
            onDeleteStep={data.onDeleteStep}
          />
        ))}
      </div>
    </div>
  )
}

function JobCard({
  stageId,
  job,
  stepMenu,
  onDeleteStep,
}: {
  stageId: string
  job: GraphJob
  stepMenu: MenuProps
  onDeleteStep: (stageId: string, jobId: string, stepOrder: number) => void
}) {
  const t = useT()
  // 构建机列表已被编辑器里的其它组件拉取过，同 key 复用缓存，不会多发请求
  const { data: agents = [] } = useQuery({
    queryKey: ['agents', 'builder'],
    queryFn: () => get<BuildAgent[]>('/agents?role=builder'),
    staleTime: 30000,
  })
  const agentText = agentDisplay(job.agent, agents)

  return (
    <div
      style={{
        border: '1px solid #e8e8e8',
        borderRadius: 6,
        background: '#fff',
        overflow: 'hidden',
      }}
    >
      {/* Job 标题 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 10px',
          background: '#e6f4ff',
          fontSize: 12,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 14 }}>{agentText.icon}</span>
          <span style={{ fontWeight: 600 }}>{job.name}</span>
        </div>
        <Tag color="cyan" style={{ marginRight: 0, fontSize: 11 }}>
          {agentText.text}
        </Tag>
      </div>

      {/* Step 列表 */}
      <div style={{ padding: '6px 0' }}>
        {job.steps.length === 0 && (
          <div style={{ textAlign: 'center', color: '#ccc', padding: 8, fontSize: 12 }}>
            {t('pipe.noSteps')}
          </div>
        )}
        {job.steps.map((step: GraphStep, idx: number) => (
          <div
            key={`${job.id}-${step.order}-${idx}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '6px 10px',
              fontSize: 12,
              borderTop: idx === 0 ? 'none' : '1px solid #f5f5f5',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flex: 1 }}>
              <span>{step.icon || '🔧'}</span>
              <span style={{ color: '#333' }}>{step.name || step.display_name || step.plugin}</span>
            </div>
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined style={{ fontSize: 11 }} />}
              onClick={() => onDeleteStep(stageId, job.id, step.order)}
            />
          </div>
        ))}
        {/* 添加步骤 */}
        <Dropdown menu={stepMenu} trigger={['click']}>
          <div
            style={{
              textAlign: 'center',
              padding: '4px',
              color: '#1677ff',
              cursor: 'pointer',
              fontSize: 12,
            }}
          >
            <PlusOutlined /> {t('pipe.addStep')}
          </div>
        </Dropdown>
      </div>
    </div>
  )
}

export default memo(StageNode)
