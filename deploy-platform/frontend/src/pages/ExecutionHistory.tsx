import { useState, useEffect, useMemo } from 'react'
import { Card, Tag, Button, Space, Row, Col, Empty, message, Drawer, Tooltip, Typography, Tabs } from 'antd'
import DataTable from '@/components/DataTable'
import { ClockCircleOutlined, ExperimentOutlined, BuildOutlined, EditOutlined, UserOutlined, RocketOutlined, FilterOutlined, RobotOutlined } from '@ant-design/icons'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { get, post } from '@/api/client'
import type { Paged, Release, Pipeline } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useTaskLogStream } from '@/hooks/useTaskLogStream'
import DiagnoseDrawer from '@/components/DiagnoseDrawer'
import ExecuteReleaseButton from '@/components/ExecuteReleaseButton'
import PipelineContextBreadcrumb from '@/components/PipelineContextBreadcrumb'
import RiskActionButton from '@/components/RiskActionButton'
import { useT } from '@/i18n'
import { releaseStatusMeta } from '@/utils/releaseStatus'

const { Text } = Typography

interface CommitInfo {
  short_id: string
  id: string
  title: string
  message: string
  author_name: string
  author_email: string
  created_at: string
}
interface CommitsData {
  from: string | null
  to: string | null
  range: string
  commits: CommitInfo[]
  provider: string | null
}
interface PipelineVariable {
  name: string
  type: string
  value: string
  description: string
}
interface SequenceStep {
  order: number
  plugin: string
  with: Record<string, unknown>
  status: string
  duration: number | null
  duration_label: string | null
}
interface SequenceJob {
  task_id: number
  id: string
  name: string
  agent: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration: number | null
  duration_label: string | null
  steps: SequenceStep[]
}
interface SequenceStage {
  name: string
  status: string
  jobs: SequenceJob[]
}
interface SequenceData {
  release_id: number
  pipeline_id: number
  pipeline_name: string
  version: string
  source_ref: string | null
  trigger_by: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  total_duration: number | null
  total_duration_label: string | null
  is_running: boolean
  pipeline_variables: PipelineVariable[]
  stages: SequenceStage[]
}

export default function ExecutionHistory() {
  const t = useT()
  const { pipelineId } = useParams<{ pipelineId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  /** 触发方式：Webhook / AI / Rebuild 保持原文，其余按当前语言。 */
  const triggerByMap: Record<string, string> = {
    manual: t('exec.trigManual'),
    webhook: 'Webhook',
    cron: t('exec.trigCron'),
    ai: 'AI',
    rebuild: 'Rebuild',
    rollback: t('pipe.actionRollback'),
  }
  const [activeDetailId, setActiveDetailId] = useState<number | null>(null)
  const [diagnoseId, setDiagnoseId] = useState<number | null>(null)
  const [selectedStep, setSelectedStep] = useState<{ taskId: number; stepOrder: number } | null>(null)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  // 流水线信息（用于面包屑、标题）
  const { data: pipeline } = useQuery({
    queryKey: ['pipeline', pipelineId],
    queryFn: () => get<Pipeline>(`/pipelines/${pipelineId}`),
    enabled: !!pipelineId,
  })

  // 按权限决定按钮可见性：没权限就不显示（详情接口已按当前用户算好 can_*）
  const user = useAuthStore((s) => s.user)
  const canEdit = !!user?.is_admin || !!pipeline?.can_update
  const canExecute = !!user?.is_admin || !!pipeline?.can_execute

  // 该流水线的执行记录（不带 admin_only，走项目可见性）。
  // 服务端分页：一条常跑的流水线几个月就能攒几千条发布记录，全量拉回来
  // 再每 3 秒轮询一次，光这一个页面就能把后端和浏览器都拖垮
  const { data: releasePage, isLoading } = useQuery({
    queryKey: ['history', pipelineId, page, pageSize],
    queryFn: () =>
      get<Paged<Release>>('/releases', {
        pipeline_id: Number(pipelineId),
        page,
        page_size: pageSize,
      }),
    enabled: !!pipelineId,
    // 没有在跑的发布就停掉轮询，避免空闲时每 3 秒打一遍列表
    refetchInterval: (q) =>
      (q.state.data?.items || []).some((r) =>
        ['running', 'queued', 'pending', 'rolling_back'].includes(r.status),
      )
        ? 3000
        : false,
  })
  const releases = releasePage?.items || []

  // 序列化接口（Drawer 实时刷新）
  const { data: sequence } = useQuery({
    queryKey: ['release-sequence', activeDetailId],
    queryFn: () => get<SequenceData>(`/releases/${activeDetailId}/sequence`),
    enabled: !!activeDetailId,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  })

  // 代码变更 Tab
  const { data: commitsData } = useQuery({
    queryKey: ['release-commits', activeDetailId],
    queryFn: () => get<CommitsData>(`/releases/${activeDetailId}/commits`),
    enabled: !!activeDetailId,
  })

  // 默认选中第一个 step
  useEffect(() => {
    if (sequence?.stages?.[0]?.jobs?.[0] && !selectedStep) {
      const firstJob = sequence.stages[0].jobs[0]
      if (firstJob.steps?.[0]) {
        setSelectedStep({ taskId: firstJob.task_id, stepOrder: 0 })
      }
    }
  }, [sequence])

  // step 日志：SSE 订阅选中任务这一条流。
  // 原来是每 2 秒把整次发布所有任务的完整日志全拉一遍，再 find 出一条、扔掉其余
  const { lines: stepLogLines } = useTaskLogStream(selectedStep?.taskId)

  // 取消正在执行的发布（执行卡死/主动取消）
  const handleCancel = async (id: number) => {
    await post(`/releases/${id}/cancel`)
    message.success(t('exec.cancelled'))
    queryClient.invalidateQueries({ queryKey: ['history', pipelineId] })
  }

  const columns = [
    {
      title: t('deploy.buildNo'),
      dataIndex: 'build_number',
      width: 90,
      render: (num: number | undefined, r: Release) => (
        <a
          onClick={() => navigate(`/executions/${pipelineId}/${r.id}`)}
          style={{ fontFamily: 'monospace', color: '#1677ff' }}
        >
          #{num || r.id}
        </a>
      ),
    },
    {
      title: t('exec.source'),
      dataIndex: 'source_ref',
      width: 320,
      render: (v: string | null, r: Release) => {
        const branch = (r.source_branch || '').trim() || 'master'
        const short = v ? (v.length > 8 ? v.slice(0, 8) : v) : ''
        return (
          <Space size={4} style={{ fontSize: 12 }}>
            <span style={{ color: '#1677ff' }}>{`${r.pipeline_name}@${branch}`}</span>
            {short && short.toLowerCase() !== branch.toLowerCase() ? (
              <Tooltip title={v || undefined}>
                <Tag color="cyan" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  → {short}
                </Tag>
              </Tooltip>
            ) : null}
          </Space>
        )
      },
    },
    {
      title: t('exec.trigger'),
      dataIndex: 'trigger_by',
      width: 90,
      render: (v: string) => <Tag>{triggerByMap[v] || v}</Tag>,
    },
    {
      title: t('exec.operator'),
      dataIndex: 'operator_name',
      width: 110,
      render: (v: string) => (
        <Space size={4}>
          <UserOutlined style={{ color: '#999' }} />
          <span>{v || '—'}</span>
        </Space>
      ),
    },
    {
      title: t('exec.startedAt'),
      dataIndex: 'started_at',
      width: 160,
      render: (v: string | null) =>
        v ? (
          <Space size={4} style={{ color: '#999', fontSize: 12 }}>
            <ClockCircleOutlined />
            {dayjs(v).format('MM-DD HH:mm:ss')}
          </Space>
        ) : (
          <span style={{ color: '#bbb' }}>--</span>
        ),
    },
    {
      title: t('exec.finishedAt'),
      dataIndex: 'finished_at',
      width: 160,
      render: (v: string | null) =>
        v ? (
          <Space size={4} style={{ color: '#999', fontSize: 12 }}>
            <ClockCircleOutlined />
            {dayjs(v).format('MM-DD HH:mm:ss')}
          </Space>
        ) : (
          <span style={{ color: '#bbb' }}>--</span>
        ),
    },
    {
      title: t('exec.artifacts'),
      dataIndex: 'version',
      width: 120,
      render: (v: string) => <Tag>{v || '--'}</Tag>,
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 220,
      render: (v: string, r: Release) => {
        const s = releaseStatusMeta(v)
        return (
          <div>
            <Tag color={s.color}>{s.text}</Tag>
            {r.error_summary ? (
              <Tooltip title={r.error_summary}>
                <div
                  style={{
                    color: '#ff4d4f',
                    fontSize: 12,
                    marginTop: 4,
                    maxWidth: 200,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {r.error_summary}
                </div>
              </Tooltip>
            ) : null}
          </div>
        )
      },
    },
    {
      title: t('common.action'),
      width: 250,
      fixed: 'right' as const,
      render: (_: unknown, r: Release) => (
        <Space size={4} onClick={(e) => e.stopPropagation()}>
          {r.status === 'failed' && (
            <Button
              size="small"
              type="primary"
              ghost
              icon={<RobotOutlined />}
              onClick={(e) => {
                e.stopPropagation()
                setDiagnoseId(r.id)
              }}
            >
              {t('exec.aiDiagnose')}
            </Button>
          )}
          <span onClick={(e) => e.stopPropagation()}>
            <Space>
              <RiskActionButton
                action="rebuild"
                releaseId={r.id}
                buildNumber={r.build_number}
                approvalRequired={r.approval_required ?? pipeline?.approval_required}
                allowBypass={r.allow_emergency_bypass ?? pipeline?.allow_emergency_bypass}
                groupType={r.group_type ?? pipeline?.group_type}
                pipeline={pipeline}
                disabled={['pending', 'queued', 'running'].includes(r.status) || !r.source_ref}
                title={!r.source_ref ? t('exec.noCommit') : t('exec.rebuildWith', { ref: r.source_ref?.slice(0, 8) })}
                onDone={(nr) => {
                  queryClient.invalidateQueries({ queryKey: ['history', pipelineId] })
                  if (nr.status !== 'pending') navigate(`/executions/${pipelineId}/${nr.id}`)
                }}
              />
              {['success', 'failed'].includes(r.status) && (
                <RiskActionButton
                  action="rollback"
                  releaseId={r.id}
                  buildNumber={r.build_number}
                  approvalRequired={r.approval_required ?? pipeline?.approval_required}
                  allowBypass={r.allow_emergency_bypass ?? pipeline?.allow_emergency_bypass}
                  groupType={r.group_type ?? pipeline?.group_type}
                  onDone={(nr) => {
                    queryClient.invalidateQueries({ queryKey: ['history', pipelineId] })
                    if (nr.status !== 'pending') navigate(`/executions/${pipelineId}/${nr.id}`)
                  }}
                />
              )}
            </Space>
          </span>
          {['running', 'pending', 'queued'].includes(r.status) && (
            <Button
              size="small"
              danger
              onClick={(e) => {
                e.stopPropagation()
                handleCancel(r.id)
              }}
            >
              {t('common.cancel')}
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div>
      {/* 顶部：面包屑 + 按钮 */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
          marginBottom: 16,
          padding: '8px 0',
        }}
      >
        <PipelineContextBreadcrumb pipeline={pipeline} current={t('exec.history')} />
        <Space>
          {canExecute && (
            <ExecuteReleaseButton
              pipeline={pipeline}
              pipelineId={pipelineId}
              onDone={() => queryClient.invalidateQueries({ queryKey: ['history', pipelineId] })}
            />
          )}
          {canEdit && (
            <Button
              icon={<EditOutlined />}
              onClick={() => navigate(`/pipeline/${pipelineId}/edit`)}
            >
              {t('common.edit')}
            </Button>
          )}
        </Space>
      </div>

      {/* 列表 */}
      <Card
        title={
          <Space>
            <ExperimentOutlined />
            <span>{t('exec.historyTitle', { name: pipeline?.name || '...' })}</span>
            <Tag>{t('exec.nRuns', { n: releasePage?.total ?? 0 })}</Tag>
          </Space>
        }
        size="small"
      >
        {isLoading || releases.length > 0 ? (
          <DataTable
            chromeKey="execution-history"
            rowKey="id"
            loading={isLoading}
            columns={canExecute ? columns : columns.filter((c) => c.title !== t('common.action'))}
            dataSource={releases}
            size="small"
            locale={isLoading ? { emptyText: ' ' } : undefined}
            pagination={{
              current: releasePage?.page || page,
              pageSize: releasePage?.page_size || pageSize,
              total: releasePage?.total || 0,
              showSizeChanger: true,
              showTotal: (n) => t('exec.total', { n }),
            }}
            onChange={(p) => {
              setPage(p.current || 1)
              setPageSize(p.pageSize || 20)
            }}
            onRow={(record) => ({
              onClick: () => navigate(`/executions/${pipelineId}/${record.id}`),
              style: { cursor: 'pointer' },
            })}
          />
        ) : (
          <Empty description={t('exec.empty')}>
            {canExecute && (
              <ExecuteReleaseButton
                pipeline={pipeline}
                pipelineId={pipelineId}
                text={t('exec.runNow')}
                onDone={() => queryClient.invalidateQueries({ queryKey: ['history', pipelineId] })}
              />
            )}
          </Empty>
        )}
      </Card>

      <DiagnoseDrawer
        releaseId={diagnoseId}
        open={!!diagnoseId}
        onClose={() => setDiagnoseId(null)}
      />
    </div>
  )
}
