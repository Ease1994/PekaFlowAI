import type { CSSProperties } from 'react'
import { Tooltip } from 'antd'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import type { Pipeline, Project } from '@/api/types'
import { useT } from '@/i18n'

type Props = {
  pipeline?: Pipeline
  pipelineId?: string
  current: string
  /** 流水线名是否链到该线执行历史（执行明细用） */
  pipelineLink?: boolean
  fontSize?: number
}

const LINK: CSSProperties = { color: '#1677ff' }
const CURRENT: CSSProperties = { color: '#333' }
const ELLIPSIS: CSSProperties = {
  maxWidth: 220,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  display: 'inline-block',
  verticalAlign: 'bottom',
}

function Sep() {
  return <span style={{ margin: '0 8px', color: '#666' }}>/</span>
}

/**
 * 执行历史 / 执行明细的层级：项目列表 → 项目下流水线列表 → 当前流水线 → 本页。
 * 文案必须和跳转一致，不要再把「流水线」链到项目列表。
 */
export default function PipelineContextBreadcrumb({
  pipeline,
  pipelineId,
  current,
  pipelineLink = false,
  fontSize = 14,
}: Props) {
  const t = useT()
  const { data: project } = useQuery({
    queryKey: ['project', pipeline?.project_id],
    queryFn: () => get<Project>(`/projects/${pipeline!.project_id}`),
    enabled: !!pipeline?.project_id,
  })

  const projectName = project?.name || (pipeline?.project_id ? t('common.projectN', { id: pipeline.project_id }) : '...')
  const pipelineName = pipeline?.name || '...'

  return (
    <div style={{ fontSize, color: '#666', minWidth: 0, display: 'flex', alignItems: 'center' }}>
      <Link to="/projects" style={LINK}>
        {t('catalog.colProject')}
      </Link>
      <Sep />
      {pipeline?.project_id ? (
        <Tooltip title={projectName}>
          <Link to={`/projects/${pipeline.project_id}`} style={{ ...LINK, ...ELLIPSIS }}>
            {projectName}
          </Link>
        </Tooltip>
      ) : (
        <span style={CURRENT}>...</span>
      )}
      <Sep />
      {pipelineLink && pipelineId ? (
        <Tooltip title={pipelineName}>
          <Link to={`/executions/${pipelineId}`} style={{ ...LINK, ...ELLIPSIS }}>
            {pipelineName}
          </Link>
        </Tooltip>
      ) : (
        <Tooltip title={pipelineName}>
          <span style={{ ...CURRENT, ...ELLIPSIS }}>{pipelineName}</span>
        </Tooltip>
      )}
      <Sep />
      <span style={CURRENT}>{current}</span>
    </div>
  )
}
