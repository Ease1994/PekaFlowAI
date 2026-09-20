import { Card, Tag, Button, Space, Statistic, Row, Col, Modal, Empty, message, Drawer, Tooltip, DatePicker, Select, Form, Alert, Typography, Tabs } from 'antd'
import DataTable from '@/components/DataTable'
import { CheckCircleOutlined, PlayCircleOutlined, FileTextOutlined, FilterOutlined, UnorderedListOutlined, EditOutlined, UserOutlined, ClockCircleOutlined } from '@ant-design/icons'
import { useState, useEffect, useMemo } from 'react'
import dayjs from 'dayjs'
const { Text } = Typography
import { useSearchParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, postR } from '@/api/client'
import { agentDisplay } from '@/utils/agentLabel'
import { useAuthStore } from '@/stores/auth'
import { useReleaseLogStream, useTaskLogStream } from '@/hooks/useTaskLogStream'
import type { Paged, Release, Pipeline, Project, UserInfo as User } from '@/api/types'
import ExecuteReleaseButton from '@/components/ExecuteReleaseButton'
import RiskActionButton from '@/components/RiskActionButton'
import { sequenceHeaderMeta, releaseStatusMeta } from '@/utils/releaseStatus'
import { t as translate, useT } from '@/i18n'

/** 触发方式码 → 界面句子。Webhook / Rebuild / AI 保持专有名。 */
function triggerLabel(code: string): string {
  const keys: Record<string, string> = {
    manual: 'releases.triggerManual',
    webhook: 'releases.triggerWebhook',
    cron: 'releases.triggerCron',
    ai: 'releases.triggerAi',
    rebuild: 'releases.triggerRebuild',
    rollback: 'releases.triggerRollback',
  }
  return keys[code] ? translate(keys[code]) : code
}

export default function Releases() {
  const t = useT()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const user = useAuthStore((s) => s.user)
  const [activeLogId, setActiveLogId] = useState<number | null>(null)
  const { lines: logLines, done: logDone } = useReleaseLogStream(activeLogId)
  // 序列详情 Drawer（蓝盾左中右风格：序列 + 步骤日志）
  const [activeDetailId, setActiveDetailId] = useState<number | null>(null)
  const [selectedStep, setSelectedStep] = useState<{ taskId: number; stepOrder: number } | null>(null)
  // 蓝盾风格的多维筛选（日期/项目/流水线/发布人/状态/触发方式）
  const [fDate, setFDate] = useState<[string, string] | null>(null)
  const [fProject, setFProject] = useState<number | undefined>()
  const [fPipeline, setFPipeline] = useState<number | undefined>()
  const [fOperator, setFOperator] = useState<number | undefined>()
  const [fStatus, setFStatus] = useState<string | undefined>()
  const [fTrigger, setFTrigger] = useState<string | undefined>()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  // URL ?pipeline_id=X 自动筛选（项目详情页点流水线名进入）
  // 带了 pipeline_id 时筛选区不显示，不用回填下拉；在 render 里 setState 只会多绕一轮渲染
  const pipelineId = searchParams.get('pipeline_id')
    ? Number(searchParams.get('pipeline_id'))
    : undefined

  // 加载当前过滤的流水线名（用于标题）
  const { data: filteredPipeline } = useQuery({
    queryKey: ['pipeline', pipelineId],
    queryFn: () => get<Pipeline>(`/pipelines/${pipelineId}`),
    enabled: !!pipelineId,
  })

  // 管理员全局视图才用：admin_only + 跨项目下拉
  const isAdminView = !pipelineId && user?.is_admin

  // 筛选下拉数据（按需启用）
  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
    enabled: !!isAdminView || !!fProject,
  })
  const { data: pipelines = [] } = useQuery({
    queryKey: ['pipelines-by-project', fProject],
    queryFn: () => get<Pipeline[]>('/pipelines', fProject ? { project_id: fProject } : undefined),
    enabled: !!fProject,
  })
  const { data: operators = [] } = useQuery({
    queryKey: ['operators'],
    queryFn: () => get<User[]>('/users'),
    enabled: !!isAdminView,
  })

  const clearFilter = () => {
    setSearchParams({})
  }

  // 筛完往往只剩几条，还停在第 3 页的话看到的是一张空表，像是「没有符合的记录」
  useEffect(() => {
    setPage(1)
  }, [fStatus, fTrigger, fOperator, fProject, fPipeline, fDate?.[0], fDate?.[1]])

  const releaseParams: Record<string, unknown> = {}
  // 下拉里选的流水线和 URL 带进来的是同一个筛选，URL 优先
  const effectivePipelineId = pipelineId ?? fPipeline
  if (effectivePipelineId) releaseParams.pipeline_id = effectivePipelineId
  else if (isAdminView) releaseParams.admin_only = true
  if (fStatus) releaseParams.status = fStatus
  if (fTrigger) releaseParams.trigger_by = fTrigger
  if (fOperator) releaseParams.operator_id = fOperator
  if (fProject) releaseParams.project_id = fProject
  if (fDate?.[0]) releaseParams.date_from = fDate[0]
  if (fDate?.[1]) releaseParams.date_to = fDate[1]

  // 服务端分页：发布记录只增不减，全平台视角下几万条全拉回来毫无意义，
  // 何况还带着轮询。筛选条件后端都支持，翻页只取当页
  const { data: releasePage } = useQuery({
    queryKey: ['releases', effectivePipelineId, isAdminView, fStatus, fTrigger, fOperator, fProject, fDate?.[0], fDate?.[1], page, pageSize],
    queryFn: () =>
      get<Paged<Release>>('/releases', { ...releaseParams, page, page_size: pageSize }),
    // 全部跑完就没必要继续轮询，页面挂着也不该一直打后端
    refetchInterval: (q) =>
      (q.state.data?.items || []).some((r) =>
        ['running', 'queued', 'pending', 'rolling_back'].includes(r.status),
      )
        ? 3000
        : false,
  })
  const releases = releasePage?.items || []

  const { data: stats } = useQuery({
    queryKey: ['release-stats'],
    queryFn: () => get<{ total: number; success: number; failed: number; running: number; success_rate: number }>('/releases/statistics'),
  })

  // 连点会把同一条排队中的发布推两次
  const [executingId, setExecutingId] = useState<number | null>(null)

  const handleExecute = async (id: number) => {
    setExecutingId(id)
    try {
      const res = await postR(`/releases/${id}/execute`)
      if (res.message && res.message !== 'ok') {
        message.error(res.message, 8)
      }
      queryClient.invalidateQueries({ queryKey: ['releases'] })
      queryClient.invalidateQueries({ queryKey: ['release-stats'] })
    } finally {
      setExecutingId(null)
    }
  }

  const refreshReleases = () => {
    queryClient.invalidateQueries({ queryKey: ['releases'] })
    queryClient.invalidateQueries({ queryKey: ['release-stats'] })
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
interface PipelineVariable {
  name: string
  type: string
  value: string
  description: string
}
interface SequenceData {
  release_id: number
  pipeline_id: number
  pipeline_name: string
  version: string
  source_ref: string | null
  trigger_by: string
  status?: string
  error?: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  total_duration: number | null
  total_duration_label: string | null
  is_running: boolean
  pipeline_variables: PipelineVariable[]
  stages: SequenceStage[]
}
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

  // 序列接口实时轮询：执行中每 2s，执行完停止
  const { data: sequence } = useQuery({
    queryKey: ['release-sequence', activeDetailId],
    queryFn: () => get<SequenceData>(`/releases/${activeDetailId}/sequence`),
    enabled: !!activeDetailId,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  })

  // 打开 Drawer 时默认选中第一个 step（只选一次）
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

  // 代码变更（本次发布 vs 上次发布 commit 区间）
  const { data: commitsData } = useQuery({
    queryKey: ['release-commits', activeDetailId],
    queryFn: () => get<CommitsData>(`/releases/${activeDetailId}/commits`),
    enabled: !!activeDetailId,
  })

  const statusFilterOptions = [
    'pending',
    'queued',
    'running',
    'success',
    'failed',
    'rejected',
    'rolling_back',
    'rolled_back',
  ].map((k) => ({ value: k, label: releaseStatusMeta(k).text }))

  const columns = [
    {
      title: t('acl.project'),
      dataIndex: 'project_name',
      width: 130,
      render: (v: string) => (v && v !== '—' ? <Tag color="blue">{v}</Tag> : <span style={{ color: '#bbb' }}>—</span>),
    },
    {
      title: t('acl.pipeline'),
      dataIndex: 'pipeline_name',
      width: 200,
      render: (v: string, r: Release) => (
        <a
          onClick={() => navigate(`/executions/${r.pipeline_id}`)}
          style={{ fontWeight: 500 }}
        >
          {v}
        </a>
      ),
    },
    { title: t('common.version'), dataIndex: 'version', width: 120, render: (v: string) => <Tag>{v || '—'}</Tag> },
    {
      title: t('releases.colSourceRef'),
      dataIndex: 'source_ref',
      width: 120,
      render: (v: string | null) =>
        v ? (
          <Tooltip title={v}>
            <Tag color="cyan" style={{ fontFamily: 'monospace' }}>
              {v.length > 12 ? v.slice(0, 8) + '…' : v}
            </Tag>
          </Tooltip>
        ) : (
          <Tag>latest</Tag>
        ),
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
      title: t('releases.colOperator'),
      dataIndex: 'operator_name',
      width: 120,
      render: (v: string) => (
        <Space size={4}>
          <UserOutlined style={{ color: '#999' }} />
          <span>{v || '—'}</span>
        </Space>
      ),
    },
    {
      title: t('releases.colTrigger'),
      dataIndex: 'trigger_by',
      width: 90,
      render: (v: string) => triggerLabel(v),
    },
    {
      title: t('releases.colTime'),
      dataIndex: 'created_at',
      width: 160,
      render: (v: string) => (
        <Space size={4} style={{ color: '#999', fontSize: 12 }}>
          <ClockCircleOutlined />
          {v ? dayjs(v).format('MM-DD HH:mm:ss') : '—'}
        </Space>
      ),
    },
    {
      title: t('common.action'),
      width: 280,
      fixed: 'right' as const,
      // 发布管理（全局视图，只读统计）：隐藏操作按钮
      // 进入单流水线执行历史（?pipeline_id=X）才显示
      render: (_: unknown, r: Release) =>
        pipelineId ? (
          <Space>
            <Button
              size="small"
              icon={<UnorderedListOutlined />}
              onClick={() => {
                setActiveDetailId(r.id)
                setSelectedStep(null)
              }}
            >
              {t('releases.sequence')}
            </Button>
            {r.status === 'queued' && (
              <Button
                size="small"
                type="primary"
                icon={<PlayCircleOutlined />}
                loading={executingId === r.id}
                onClick={() => handleExecute(r.id)}
              >
                {t('common.execute')}
              </Button>
            )}
            <RiskActionButton
              action="rebuild"
              releaseId={r.id}
              buildNumber={r.build_number}
              approvalRequired={r.approval_required}
              allowBypass={r.allow_emergency_bypass}
              groupType={r.group_type}
              disabled={['pending', 'queued', 'running'].includes(r.status)}
              onDone={(nr) => {
                refreshReleases()
                if (nr.status !== 'pending') navigate(`/executions/${nr.pipeline_id}/${nr.id}`)
              }}
            />
            {['success', 'failed'].includes(r.status) && (
              <RiskActionButton
                action="rollback"
                releaseId={r.id}
                buildNumber={r.build_number}
                approvalRequired={r.approval_required}
                allowBypass={r.allow_emergency_bypass}
                groupType={r.group_type}
                onDone={(nr) => {
                  refreshReleases()
                  if (nr.status !== 'pending') navigate(`/executions/${nr.pipeline_id}/${nr.id}`)
                }}
              />
            )}
            {['running', 'success', 'failed'].includes(r.status) && (
              <Button
                size="small"
                icon={<FileTextOutlined />}
                onClick={() => setActiveLogId(r.id)}
              >
                {t('releases.log')}
              </Button>
            )}
          </Space>
        ) : null,
    },
  ]

  return (
    <div>
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card><Statistic title={t('releases.totalCount')} value={stats?.total ?? 0} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title={t('dash.success')} value={stats?.success ?? 0} valueStyle={{ color: '#52c41a' }} /></Card>
        </Col>
        <Col span={6}>
          <Card><Statistic title={t('dash.failed')} value={stats?.failed ?? 0} valueStyle={{ color: '#ff4d4f' }} /></Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title={t('dash.successRate')}
              value={stats?.success_rate ?? 0}
              precision={1}
              suffix="%"
              prefix={<CheckCircleOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card
        title={
          pipelineId ? (
            <Space>
              <FilterOutlined />
              <span>
                {t('releases.historyTitle')} <strong>{filteredPipeline?.name || `#${pipelineId}`}</strong>
              </span>
              <Button type="link" size="small" onClick={clearFilter}>
                {t('releases.viewAll')}
              </Button>
            </Space>
          ) : (
            <Space>
              <FilterOutlined />
              <span>{t('releases.adminTitle')}</span>
            </Space>
          )
        }
        extra={
          pipelineId ? (
            <Space>
              <ExecuteReleaseButton
                pipeline={filteredPipeline}
                pipelineId={pipelineId}
                onDone={() => queryClient.invalidateQueries({ queryKey: ['releases'] })}
              />
              <Button
                icon={<EditOutlined />}
                onClick={() => navigate(`/pipeline/${pipelineId}/edit`)}
              >
                {t('releases.editPipeline')}
              </Button>
            </Space>
          ) : null
        }
      >
        {/* 蓝盾风格：多维查询条件（仅全局视图显示） */}
        {!pipelineId && user?.is_admin && (
          <div
            style={{
              display: 'flex',
              gap: 12,
              flexWrap: 'wrap',
              alignItems: 'center',
              padding: '12px 0',
              borderBottom: '1px solid #f0f0f0',
              marginBottom: 12,
            }}
          >
            <span style={{ color: '#666' }}>{t('releases.filterDate')}</span>
            <DatePicker.RangePicker
              value={
                fDate
                  ? [dayjs(fDate[0]), fDate[1] ? dayjs(fDate[1]) : null]
                  : null
              }
              onChange={(d) => setFDate(d ? [d[0]!.toISOString(), d[1] ? d[1].toISOString() : ''] : null)}
            />
            <span style={{ color: '#666' }}>{t('releases.filterProject')}</span>
            <Select
              allowClear
              placeholder={t('releases.allProjects')}
              style={{ width: 180 }}
              value={fProject}
              onChange={(v) => {
                setFProject(v)
                setFPipeline(undefined)  // 切换项目时清空流水线
              }}
              options={projects.map((p) => ({ value: p.id, label: p.name }))}
            />
            <span style={{ color: '#666' }}>{t('releases.filterPipeline')}</span>
            <Select
              allowClear
              placeholder={fProject ? t('releases.allPipelines') : t('releases.pickProjectFirst')}
              style={{ width: 200 }}
              value={fPipeline}
              onChange={setFPipeline}
              disabled={!fProject}
              options={pipelines.map((p) => ({ value: p.id, label: p.name }))}
            />
            <span style={{ color: '#666' }}>{t('releases.filterOperator')}</span>
            <Select
              allowClear
              placeholder={t('releases.allOperators')}
              style={{ width: 140 }}
              value={fOperator}
              onChange={setFOperator}
              options={operators.map((o) => ({ value: o.id, label: o.display_name || o.username }))}
            />
            <span style={{ color: '#666' }}>{t('releases.filterStatus')}</span>
            <Select
              allowClear
              placeholder={t('releases.allStatuses')}
              style={{ width: 130 }}
              value={fStatus}
              onChange={setFStatus}
              options={statusFilterOptions}
            />
            <span style={{ color: '#666' }}>{t('releases.filterTrigger')}</span>
            <Select
              allowClear
              placeholder={t('common.all')}
              style={{ width: 120 }}
              value={fTrigger}
              onChange={setFTrigger}
              options={[
                { value: 'manual', label: t('releases.triggerManual') },
                { value: 'webhook', label: t('releases.triggerWebhook') },
                { value: 'cron', label: t('releases.triggerCron') },
                { value: 'rebuild', label: t('releases.triggerRebuild') },
                { value: 'rollback', label: t('releases.triggerRollback') },
              ]}
            />
            <Button
              size="small"
              onClick={() => {
                setFDate(null); setFProject(undefined); setFPipeline(undefined)
                setFOperator(undefined); setFStatus(undefined); setFTrigger(undefined)
              }}
            >
              {t('releases.reset')}
            </Button>
          </div>
        )}

        {/* 非管理员：只显示"无权访问"提示 */}
        {!pipelineId && !user?.is_admin && (
          <Alert
            type="warning"
            showIcon
            message={t('releases.adminOnlyTitle')}
            description={t('releases.adminOnlyDesc')}
            style={{ marginBottom: 16 }}
          />
        )}

        <DataTable
          chromeKey="releases"
          rowKey="id"
          columns={columns}
          dataSource={releases}
          pagination={{
            current: releasePage?.page || page,
            pageSize: releasePage?.page_size || pageSize,
            total: releasePage?.total || 0,
            showSizeChanger: true,
            showTotal: (n) => t('releases.totalRecords', { n }),
          }}
          onChange={(p) => {
            setPage(p.current || 1)
            setPageSize(p.pageSize || 10)
          }}
        />
      </Card>

      {/* 执行日志弹窗（SSE 实时流） */}
      <Modal
        title={t('releases.logTitle', { n: activeLogId ?? '' })}
        open={!!activeLogId}
        onCancel={() => setActiveLogId(null)}
        footer={<Button onClick={() => setActiveLogId(null)}>{t('releases.close')}</Button>}
        width={720}
      >
        <div style={{ marginBottom: 12 }}>
          <Tag color={logDone ? 'green' : 'processing'}>
            {logDone ? t('status.done') : t('releases.logRunning')}
          </Tag>
        </div>
        <div
          style={{
            background: '#1e1e1e',
            color: '#d4d4d4',
            fontFamily: 'monospace',
            fontSize: 12,
            padding: 16,
            borderRadius: 6,
            maxHeight: 420,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            lineHeight: 1.7,
          }}
        >
          {logLines.length === 0
            ? t('releases.waitingLog')
            : logLines.join('\n')}
        </div>
      </Modal>

      {/* 发布序列详情 Drawer（蓝盾风格：时间轴 + 全局变量 + stage/job/step 树 + 步骤日志） */}
      <Drawer
        title={
          sequence ? (
            <Space>
              <span>{t('releases.releaseTitle', { name: sequence.pipeline_name, version: sequence.version })}</span>
              <Tag color={sequenceHeaderMeta(sequence).color}>
                {sequenceHeaderMeta(sequence).text}
              </Tag>
              {sequence.total_duration_label && (
                <Tag color="blue">{t('releases.totalDuration', { time: sequence.total_duration_label })}</Tag>
              )}
            </Space>
          ) : t('releases.detailTitle', { n: activeDetailId ?? '' })
        }
        open={!!activeDetailId}
        onClose={() => setActiveDetailId(null)}
        width={1100}
        destroyOnClose
      >
        {!sequence ? (
          <Empty description={t('releases.loadingSeq')} />
        ) : (
          <Tabs
            defaultActiveKey="detail"
            items={[
              {
                key: 'detail',
                label: t('releases.tabDetail'),
                children: (
                  <>
                    {/* 顶部：时间轴 + 流水线全局变量（按 pipeline 隔离） */}
                    <div
                      style={{
                        padding: 12,
                        background: '#fafafa',
                        borderRadius: 6,
                        marginBottom: 12,
                      }}
                    >
                      <Row gutter={16}>
                        <Col span={6}>
                          <Space direction="vertical" size={2}>
                            <Text type="secondary" style={{ fontSize: 11 }}>{t('releases.triggeredAt')}</Text>
                            <Text style={{ fontSize: 12 }}>
                              {sequence.created_at ? dayjs(sequence.created_at).format('YYYY-MM-DD HH:mm:ss') : '—'}
                            </Text>
                          </Space>
                        </Col>
                        <Col span={6}>
                          <Space direction="vertical" size={2}>
                            <Text type="secondary" style={{ fontSize: 11 }}>{t('releases.startedAt')}</Text>
                            <Text style={{ fontSize: 12 }}>
                              {sequence.started_at ? dayjs(sequence.started_at).format('YYYY-MM-DD HH:mm:ss') : '—'}
                            </Text>
                          </Space>
                        </Col>
                        <Col span={6}>
                          <Space direction="vertical" size={2}>
                            <Text type="secondary" style={{ fontSize: 11 }}>{t('releases.finishedAt')}</Text>
                            <Text style={{ fontSize: 12 }}>
                              {sequence.finished_at ? dayjs(sequence.finished_at).format('YYYY-MM-DD HH:mm:ss') : t('releases.stillRunning')}
                            </Text>
                          </Space>
                        </Col>
                        <Col span={6}>
                          <Space direction="vertical" size={2}>
                            <Text type="secondary" style={{ fontSize: 11 }}>{t('releases.duration')}</Text>
                            <Text strong style={{ fontSize: 13, color: '#1677ff' }}>
                              {sequence.total_duration_label || t('releases.calculating')}
                            </Text>
                          </Space>
                        </Col>
                      </Row>
                      {sequence.pipeline_variables && sequence.pipeline_variables.length > 0 && (
                        <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid #e8e8e8' }}>
                          <Text type="secondary" style={{ fontSize: 11 }}>{t('releases.pipeVars', { name: sequence.pipeline_name })}</Text>
                          <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                            {sequence.pipeline_variables.map((v) => (
                              <Tag key={v.name} color="cyan" style={{ fontSize: 11 }}>
                                <strong>{v.name}</strong>: {v.value || '—'}
                              </Tag>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>

                    {sequence.error && (
                      <Alert
                        type="error"
                        showIcon
                        style={{ marginBottom: 12 }}
                        message={t('releases.failReason')}
                        description={
                          <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{sequence.error}</div>
                        }
                      />
                    )}

                    {/* 主体：左 stage→job→step 树 + 右 step 日志 */}
                    <Row gutter={12}>
                      <Col span={11} style={{ borderRight: '1px solid #f0f0f0', paddingRight: 8 }}>
                        {sequence.stages.map((stage, si) => (
                          <div key={si} style={{ marginBottom: 12 }}>
                            <div style={{ fontWeight: 600, marginBottom: 6, color: statusColor(stage.status) }}>
                              {stageIcon(stage.status)} {stage.name}
                              <Tag style={{ marginLeft: 8 }} color={statusColor(stage.status)}>
                                {releaseStatusMeta(stage.status).text}
                              </Tag>
                            </div>
                            {stage.jobs.map((job, ji) => (
                              <div
                                key={ji}
                                style={{
                                  marginLeft: 12,
                                  marginBottom: 8,
                                  padding: 8,
                                  background: '#fafafa',
                                  borderRadius: 4,
                                }}
                              >
                                <div style={{ fontWeight: 500, marginBottom: 4, display: 'flex', alignItems: 'center' }}>
                                  <span style={{ flex: 1 }}>
                                    🔨 {job.name}
                                    <Tag style={{ marginLeft: 6 }} color={statusColor(job.status)}>
                                      {releaseStatusMeta(job.status).text}
                                    </Tag>
                                    <Tag>{agentDisplay(job.agent).text}</Tag>
                                  </span>
                                  {job.duration_label && (
                                    <Tag color="blue" style={{ fontFamily: 'monospace' }}>
                                      {job.duration_label}
                                    </Tag>
                                  )}
                                </div>
                                {job.steps.map((step, sti) => (
                                  <span key={sti} style={{ display: 'inline-block', marginRight: 6, marginBottom: 4 }}>
                                    <Tooltip title={step.plugin}>
                                      <Button
                                        size="small"
                                        type={
                                          selectedStep?.taskId === job.task_id &&
                                          selectedStep?.stepOrder === step.order
                                            ? 'primary'
                                            : 'default'
                                        }
                                        onClick={() =>
                                          setSelectedStep({ taskId: job.task_id, stepOrder: step.order })
                                        }
                                        style={{ fontFamily: 'monospace' }}
                                      >
                                        {stageIcon(step.status)} {step.order + 1}. {step.plugin}
                                      </Button>
                                    </Tooltip>
                                    {step.duration_label && (
                                      <span style={{ marginLeft: -2, fontSize: 11, color: '#999' }}>
                                        {' '}{step.duration_label}
                                      </span>
                                    )}
                                  </span>
                                ))}
                              </div>
                            ))}
                          </div>
                        ))}
                      </Col>
                      <Col span={13}>
                        <div style={{ marginBottom: 8, fontSize: 13, color: '#666' }}>
                          {selectedStep
                            ? t('releases.stepLog', { n: selectedStep.stepOrder + 1, id: selectedStep.taskId })
                            : t('releases.pickStep')}
                          {sequence.is_running && (
                            <Tag color="processing" style={{ marginLeft: 8 }}>{t('releases.liveRefresh')}</Tag>
                          )}
                        </div>
                        <pre
                          style={{
                            background: '#1e1e1e',
                            color: '#d4d4d4',
                            fontFamily: 'monospace',
                            fontSize: 12,
                            padding: 16,
                            borderRadius: 6,
                            maxHeight: 560,
                            overflow: 'auto',
                            whiteSpace: 'pre-wrap',
                            lineHeight: 1.7,
                            minHeight: 400,
                          }}
                        >
                          {stepLogLines.length === 0
                            ? sequence.error
                              ? sequence.error
                              : t('releases.noLog')
                            : stepLogLines.join('\n')}
                        </pre>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'commits',
                label: commitsData ? t('releases.commitsTabN', { n: commitsData.commits.length }) : t('releases.commitsTab'),
                children: commitsData ? (
                  <div style={{ padding: 4 }}>
                    <div style={{ marginBottom: 12, padding: '8px 12px', background: '#fafafa', borderRadius: 6 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {t('releases.commitsHint')}
                      </Text>
                      <Tag color="cyan" style={{ marginLeft: 8, fontFamily: 'monospace', fontSize: 13 }}>
                        {commitsData.range}
                      </Tag>
                      {commitsData.provider && (
                        <Tag style={{ marginLeft: 4 }}>{commitsData.provider}</Tag>
                      )}
                    </div>
                    {commitsData.commits.length === 0 ? (
                      <Empty description={t('releases.noCommits')} />
                    ) : (
                      <DataTable
                        chromeKey="release-commits"
                        rowKey="id"
                        size="small"
                        pagination={false}
                        dataSource={commitsData.commits}
                        columns={[
                          { title: t('releases.colNote'), dataIndex: 'title', ellipsis: true },
                          {
                            title: 'Commit',
                            dataIndex: 'short_id',
                            width: 110,
                            render: (v: string, r: CommitInfo) => (
                              <Tooltip title={r.id}>
                                <Tag color="cyan" style={{ fontFamily: 'monospace' }}>
                                  {v || r.id.slice(0, 7)}
                                </Tag>
                              </Tooltip>
                            ),
                          },
                          {
                            title: t('releases.colCommitter'),
                            dataIndex: 'author_name',
                            width: 140,
                            render: (v: string) => <Tag>{v || '—'}</Tag>,
                          },
                          {
                            title: t('releases.colCommitTime'),
                            dataIndex: 'created_at',
                            width: 180,
                            render: (v: string) =>
                              v ? (
                                <span style={{ color: '#999', fontSize: 12 }}>
                                  {dayjs(v).format('YYYY-MM-DD HH:mm:ss')}
                                </span>
                              ) : (
                                '—'
                              ),
                          },
                        ]}
                      />
                    )}
                  </div>
                ) : (
                  <Empty description={t('releases.loadingCommits')} />
                ),
              },
            ]}
          />
        )}
      </Drawer>
    </div>
  )
}

function statusColor(s: string): string {
  return releaseStatusMeta(s).color
}

function stageIcon(s: string): string {
  return ({ success: '✅', failed: '❌', running: '▶️', pending: '⏸' }[s] || '·')
}
