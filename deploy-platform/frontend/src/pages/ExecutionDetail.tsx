import { useState, useEffect, useMemo, useRef, useCallback, type CSSProperties } from 'react'
import { Alert, Card, Tag, Button, Space, Row, Col, Empty, message, Tooltip, Typography, Tabs } from 'antd'
import { ArrowLeftOutlined, RobotOutlined, ApartmentOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import { get, post, isHttpNotFound, retryUnlessNotFound } from '@/api/client'
import type { Pipeline } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useIsMobile } from '@/hooks/useIsMobile'
import { useTaskLogStream } from '@/hooks/useTaskLogStream'
import DiagnoseDrawer from '@/components/DiagnoseDrawer'
import ExecutionCanvas from '@/components/execution/ExecutionCanvas'
import PipelineContextBreadcrumb from '@/components/PipelineContextBreadcrumb'
import RiskActionButton from '@/components/RiskActionButton'
import { TONE_COLOR, logTone, stepLogLines, type LogTone } from '@/utils/buildLog'
import { sequenceHeaderMeta, releaseStatusMeta } from '@/utils/releaseStatus'
import { useT } from '@/i18n'

const { Text } = Typography

interface PipelineVariable {
  name: string
  type: string
  value: string
  description: string
}
interface SequenceStep {
  order: number
  /** 同一个 Job 拆成多段时，每步的日志在各自的 task 里；还没开跑时为 null */
  task_id: number | null
  /** 该步骤在所属 task 日志里的序号（order 是整个 Job 重排后的展示序号） */
  log_index: number
  name: string
  plugin: string
  with: Record<string, unknown>
  status: string
  started_at: string | null
  duration: number | null
  duration_label: string | null
}
interface SequenceJob {
  task_id: number | null
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
  id?: string
  name: string
  status: string
  jobs: SequenceJob[]
}
interface SequenceData {
  release_id: number
  build_number: number
  pipeline_id: number
  pipeline_name: string
  version: string
  source_ref: string | null
  trigger_by: string
  status?: string
  /** 启动失败等原因，步骤还没开始时由后端从 error_message 抽出 */
  error?: string
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  total_duration: number | null
  total_duration_label: string | null
  is_running: boolean
  pipeline_variables: PipelineVariable[]
  /** 与编辑页保存的节点坐标相同，执行画布按这个摆 */
  canvas_layout?: Record<string, { x: number; y: number }>
  stages: SequenceStage[]
}

interface CommitInfo {
  short_id: string
  id: string
  title: string
  author_name: string
  created_at: string
}
interface CommitsData {
  from: string | null
  to: string | null
  range: string
  commits: CommitInfo[]
  provider: string | null
}

/** 优先显示用户起的步骤名。这次改动之前跑的发布没存 name，退回插件名。 */
function stepLabel(step: SequenceStep): string {
  return step.name?.trim() || step.plugin
}

// 行样式提到模块级：写在 map 里的话每行每次渲染都新建两个对象，
// 几千行就是上万次分配，React 还得逐个 diff
const LOG_ROW: CSSProperties = { display: 'flex' }
// 报错行整行压一层红底，长日志里一眼能扫到
const LOG_ROW_ERROR: CSSProperties = { display: 'flex', background: 'rgba(248, 113, 113, 0.12)' }
const LOG_GUTTER: CSSProperties = {
  color: '#858585',
  minWidth: 50,
  marginRight: 12,
  textAlign: 'right',
  userSelect: 'none',
}
const LOG_TEXT: Record<LogTone, CSSProperties> = Object.fromEntries(
  (Object.keys(TONE_COLOR) as LogTone[]).map((t) => [t, { flex: 1, color: TONE_COLOR[t] }]),
) as Record<LogTone, CSSProperties>

type StepPick = { taskId: number | null; stepOrder: number; logIndex: number }

// 「已经开始跑过」的状态。不含 cancelled：发布被取消时后面没轮到的步骤也会标成
// cancelled，把它算进来会把人带到一个根本没执行、也没有日志的步骤上
const STARTED = new Set(['running', 'success', 'failed', 'timeout'])

function pickOf(job: SequenceJob, step: SequenceStep): StepPick {
  return {
    taskId: step.task_id ?? job.task_id,
    stepOrder: step.order,
    logIndex: step.log_index ?? step.order,
  }
}

function samePick(a: StepPick | null, b: StepPick | null): boolean {
  return !!a && !!b && a.taskId === b.taskId && a.stepOrder === b.stepOrder
}

function eachStep(seq: SequenceData, fn: (job: SequenceJob, step: SequenceStep) => void): void {
  for (const stage of seq.stages || []) {
    for (const job of stage.jobs || []) {
      for (const step of job.steps || []) fn(job, step)
    }
  }
}

function findFailedStep(seq: SequenceData): StepPick | null {
  let found: StepPick | null = null
  eachStep(seq, (job, step) => {
    if (!found && (step.status === 'failed' || step.status === 'timeout')) found = pickOf(job, step)
  })
  return found
}

/** 执行进度最前沿的那一步：优先正在跑的，否则退到最后一个跑过的。
 *
 * 只认 running 是不够的：序列每 2 秒才拉一次，跑得快的步骤可能在两次轮询之间
 * 就从未开始直接变成了成功，一次 running 都没被看到。这时候若找不到 running 就
 * 不动，日志就会卡在前面某一步不跟了。
 */
function findLiveStep(seq: SequenceData): StepPick | null {
  let running: StepPick | null = null
  let lastStarted: StepPick | null = null
  eachStep(seq, (job, step) => {
    if (!running && step.status === 'running') running = pickOf(job, step)
    if (STARTED.has(step.status)) lastStarted = pickOf(job, step)
  })
  return running ?? lastStarted
}

function firstStep(seq: SequenceData): StepPick | null {
  const job = seq.stages?.[0]?.jobs?.[0]
  const step = job?.steps?.[0]
  return job && step ? pickOf(job, step) : null
}

function hasAnyTask(seq: SequenceData): boolean {
  let found = false
  eachStep(seq, (job, step) => {
    if (step.task_id || job.task_id) found = true
  })
  return found
}

export default function ExecutionDetail() {
  const t = useT()
  const { pipelineId, releaseId } = useParams<{ pipelineId: string; releaseId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedStep, setSelectedStep] = useState<StepPick | null>(null)
  // 日志是否跟着执行进度走。默认跟，手动点步骤就停
  const [followLive, setFollowLive] = useState(true)
  // 画布点空白后主动关掉日志。为 true 时禁止自动再选中步骤，否则日志会立刻弹回来
  const [logDismissed, setLogDismissed] = useState(false)
  const [diagnoseOpen, setDiagnoseOpen] = useState(false)
  /** 窄屏步骤树和日志上下叠，不走 360px 并排。 */
  const isMobile = useIsMobile()
  const [viewMode, setViewMode] = useState<'list' | 'canvas'>(() => {
    try {
      return localStorage.getItem('rp-exec-view') === 'canvas' ? 'canvas' : 'list'
    } catch {
      return 'list'
    }
  })
  const logBottomRef = useRef<HTMLDivElement>(null)
  /** 手机上看列表；画布编排在窄屏里几乎点不到节点。 */
  const layoutMode = isMobile ? 'list' : viewMode

  // 流水线信息（用于面包屑）
  const { data: pipeline } = useQuery({
    queryKey: ['pipeline', pipelineId],
    queryFn: () => get<Pipeline>(`/pipelines/${pipelineId}`),
    enabled: !!pipelineId,
  })

  // 序列化（实时刷新）。清库后旧书签 /executions/1/1 还在，404 不要连弹「发布任务不存在」。
  const {
    data: sequence,
    error: sequenceError,
  } = useQuery({
    queryKey: ['release-sequence', releaseId],
    queryFn: () =>
      get<SequenceData>(`/releases/${releaseId}/sequence`, undefined, { skipErrorToast: true }),
    enabled: !!releaseId,
    retry: retryUnlessNotFound,
    refetchInterval: (q) => (q.state.data?.is_running ? 2000 : false),
  })

  // 代码变更
  const { data: commitsData } = useQuery({
    queryKey: ['release-commits', releaseId],
    queryFn: () =>
      get<CommitsData>(`/releases/${releaseId}/commits`, undefined, { skipErrorToast: true }),
    enabled: !!releaseId,
    retry: retryUnlessNotFound,
  })

  // 发布单已经没了（空库、过期链接）：回到这条流水线的执行历史，不要停在空白明细页。
  useEffect(() => {
    if (!pipelineId || !isHttpNotFound(sequenceError)) return
    navigate(`/executions/${pipelineId}`, { replace: true })
  }, [sequenceError, pipelineId, navigate])

  // 换发布单时清空选中。不清的话，下面那个 effect 见 selectedStep 有值就直接
  // return，页面标题已经是新的一单，左边还高亮着旧步骤
  // （日志不用管：选中清空后 taskId 没了，订阅 hook 自己会清空并断开）
  useEffect(() => {
    setSelectedStep(null)
    setFollowLive(true)
    setLogDismissed(false)
  }, [releaseId])

  // 执行前沿，渲染时也要用它判断用户点的是不是「当前这步」
  const livePick = useMemo(() => (sequence ? findLiveStep(sequence) : null), [sequence])
  const headerMeta = sequence ? sequenceHeaderMeta(sequence) : null

  /** 点列表或画布节点：点当前执行步则继续跟随，否则停下来看日志。 */
  const selectPick = useCallback(
    (pick: StepPick) => {
      setLogDismissed(false)
      setFollowLive(samePick(livePick, pick))
      setSelectedStep(pick)
    },
    [livePick],
  )

  /** 仅画布：点空白关掉右侧日志，并停止跟随，避免自动选中又把日志打开。 */
  const dismissLog = useCallback(() => {
    setLogDismissed(true)
    setFollowLive(false)
    setSelectedStep(null)
  }, [])

  /** 列表 / 画布一键切换，偏好记在本机。切回列表时恢复自动选中，列表交互保持原样。 */
  const toggleView = () => {
    const next = viewMode === 'list' ? 'canvas' : 'list'
    setViewMode(next)
    if (next === 'list') {
      setLogDismissed(false)
    }
    try {
      localStorage.setItem('rp-exec-view', next)
    } catch {
      /* 隐私模式写不了也没关系，当次会话仍能切 */
    }
  }
  const canDiagnose =
    !!sequence &&
    (sequence.status === 'failed' ||
      sequence.status === 'cancelled' ||
      sequence.stages.some((s) => s.status === 'failed'))
  const startupFailed =
    !!sequence && sequence.status === 'failed' && !!sequence.error && !hasAnyTask(sequence)
  // 只有画布允许点空白收起日志；列表始终显示右侧日志
  const showLogPane = layoutMode !== 'canvas' || !logDismissed

  // 选中哪一步。三件事按优先级排：失败了停在错误上、执行中跟着往前走、其余保持不动。
  //
  // 跟着走这件事必须能被用户叫停：翻日志翻到一半被自动拽到下一步，比不跟还难受。
  // 所以手动点过步骤就交还控制权，想跟了再点「回到当前步骤」。
  useEffect(() => {
    if (!sequence) return
    if (layoutMode === 'canvas' && logDismissed) return
    const failed = findFailedStep(sequence)

    if (!selectedStep) {
      const initial = failed ?? livePick ?? firstStep(sequence)
      if (initial) setSelectedStep(initial)
      // 打开页面就是失败的（多半从失败通知点进来），停在错误上别再往后跑
      if (failed) setFollowLive(false)
      return
    }

    // 排队/审批阶段选中的是流水线定义里的「计划步骤」，它没有 task，也就没有日志。
    // 真任务建出来后不切过去，页面会一直停在计划步骤上，看着像卡住了
    if (selectedStep.taskId == null && livePick?.taskId != null) {
      setSelectedStep(livePick)
      return
    }

    if (!followLive) return

    if (failed) {
      setSelectedStep(failed)
      setFollowLive(false) // 停下来让人看清错误，别被后续步骤带走
      return
    }
    if (livePick && !samePick(livePick, selectedStep)) setSelectedStep(livePick)
  }, [sequence, livePick, logDismissed, layoutMode])

  // 选中 step 后 SSE 订阅该 task 日志流（替代 2s 全量轮询）。
  // 整条 task 的日志一次拿到，按步骤切分放在渲染时做，切步骤不用重新拉流
  const { lines: taskLogLines, loading: logLoading } = useTaskLogStream(selectedStep?.taskId)

  // 只取当前步骤那一段（老 Agent 没打标记时退回整段）
  const visibleLines = useMemo(
    () => (selectedStep ? stepLogLines(taskLogLines, selectedStep.logIndex) : []),
    [taskLogLines, selectedStep?.logIndex],
  )

  // 上色规则每行要跑十几个正则，放在 map 里会随每次重渲染重算一遍，
  // 分批铺行时这笔开销要重复几十次。日志本身不变，算一次存着就够
  const tonedLines = useMemo(
    () => visibleLines.map((line) => ({ line, tone: logTone(line) })),
    [visibleLines],
  )
  const errorLines = useMemo(
    () => tonedLines.filter((row) => row.tone === 'error'),
    [tonedLines],
  )
  const logPaneRef = useRef<HTMLDivElement>(null)
  const errorRecapRef = useRef<HTMLDivElement>(null)
  const jumpedError = useRef('')

  // 执行中跟尾巴。失败后滚到文末报错汇总：MSBuild 按文件边编边报，
  // error 会夹在 warning 中间，原始顺序不能改，只能在最后再列一遍。
  useEffect(() => {
    if (sequence?.is_running && followLive) {
      logBottomRef.current?.scrollIntoView({ block: 'end' })
    }
  }, [tonedLines.length, sequence?.is_running, followLive])

  useEffect(() => {
    const key = `${selectedStep?.taskId ?? ''}:${selectedStep?.logIndex ?? ''}`
    if (sequence?.is_running) return
    if (!errorLines.length) return
    if (jumpedError.current === key) return
    jumpedError.current = key
    const id = requestAnimationFrame(() => {
      errorRecapRef.current?.scrollIntoView({ block: 'end' })
    })
    return () => cancelAnimationFrame(id)
  }, [selectedStep?.taskId, selectedStep?.logIndex, sequence?.is_running, errorLines.length, tonedLines.length])

  useEffect(() => {
    jumpedError.current = ''
  }, [selectedStep?.taskId, selectedStep?.logIndex])

  // 取消正在执行的发布
  const [cancelling, setCancelling] = useState(false)
  const handleCancel = async () => {
    if (!releaseId) return
    setCancelling(true)
    try {
      await post(`/releases/${releaseId}/cancel`)
      message.success(t('exec.cancelled'))
      queryClient.invalidateQueries({ queryKey: ['release-sequence', releaseId] })
    } finally {
      setCancelling(false)
    }
  }

  // 当前 step 详情
  const currentStep = (() => {
    if (!sequence || !selectedStep) return null
    for (const stage of sequence.stages) {
      for (const job of stage.jobs) {
        const step = job.steps.find((s) => s.order === selectedStep.stepOrder)
        if (step && (step.task_id ?? job.task_id) === selectedStep.taskId) {
          return { job, step }
        }
      }
    }
    return null
  })()

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)' }}>
      {/* 顶部：面包屑 + 状态 + 按钮 */}
      <div
        style={{
          padding: '12px 16px',
          background: '#fff',
          borderBottom: '1px solid #f0f0f0',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
          flexShrink: 0,
        }}
      >
        <Space size={12}>
          <Button
            icon={<ArrowLeftOutlined />}
            onClick={() => navigate(`/executions/${pipelineId}`)}
          >
            {t('pipe.back')}
          </Button>
          {sequence?.is_running && (
            <Button danger loading={cancelling} onClick={handleCancel}>
              {t('exec.cancelRun')}
            </Button>
          )}
          <PipelineContextBreadcrumb
            pipeline={pipeline}
            pipelineId={pipelineId}
            pipelineLink
            fontSize={13}
            current={t('exec.detailN', { n: sequence?.build_number || releaseId || '' })}
          />
        </Space>
        {sequence && (
          <Space wrap>
            {!isMobile && (
            <Button
              icon={viewMode === 'canvas' ? <UnorderedListOutlined /> : <ApartmentOutlined />}
              onClick={toggleView}
            >
              {viewMode === 'canvas' ? t('exec.listView') : t('pipe.canvasArrange')}
            </Button>
            )}
            {canDiagnose && (
              <Button
                type="primary"
                icon={<RobotOutlined />}
                onClick={() => setDiagnoseOpen(true)}
              >
                {t('exec.aiDiagnose')}
              </Button>
            )}
            <RiskActionButton
              action="rebuild"
              size="middle"
              releaseId={Number(releaseId)}
              buildNumber={sequence.build_number}
              approvalRequired={pipeline?.approval_required}
              allowBypass={pipeline?.allow_emergency_bypass}
              groupType={pipeline?.group_type}
              pipeline={pipeline}
              disabled={sequence.is_running || !sequence.source_ref}
              title={
                !sequence.source_ref
                  ? t('exec.noCommit')
                  : t('exec.rebuildWith', { ref: sequence.source_ref.slice(0, 8) })
              }
              onDone={(nr) => {
                if (nr.status !== 'pending') navigate(`/executions/${pipelineId}/${nr.id}`)
              }}
            />
            <Tag color={headerMeta?.color} style={{ fontSize: 13 }}>
              {headerMeta?.text}
            </Tag>
            {sequence.total_duration_label && (
              <Tag color="blue" style={{ fontSize: 13 }}>⏱ {t('exec.totalDuration', { time: sequence.total_duration_label })}</Tag>
            )}
            {sequence.source_ref && (
              <Tooltip title={sequence.source_ref}>
                <Tag color="cyan" style={{ fontFamily: 'monospace' }}>
                  {sequence.source_ref.slice(0, 8)}
                </Tag>
              </Tooltip>
            )}
          </Space>
        )}
      </div>

      {/* 主体：左 1/3 树 + 右 2/3 大日志 */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden', flexDirection: isMobile ? 'column' : 'row' }}>
        {/* 左侧 stage→job→step 树 */}
        {/* 列表：左步骤树；画布：Dify 风格节点图。右侧都是当前步骤日志。 */}
        <div
          style={{
            width: isMobile ? '100%' : layoutMode === 'list' ? 360 : undefined,
            flex: isMobile || layoutMode === 'canvas' ? 1 : undefined,
            flexShrink: 0,
            minWidth: isMobile ? 0 : layoutMode === 'canvas' ? 0 : 360,
            minHeight: isMobile ? 180 : undefined,
            maxHeight: isMobile && showLogPane ? '46%' : undefined,
            padding: layoutMode === 'list' ? 12 : 0,
            background: layoutMode === 'list' ? '#fafafa' : '#f3f5f8',
            borderRight: !isMobile && showLogPane ? '1px solid #f0f0f0' : 'none',
            borderBottom: isMobile && showLogPane ? '1px solid #f0f0f0' : 'none',
            overflowY: layoutMode === 'list' ? 'auto' : 'hidden',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          {!sequence ? (
            <Empty description={t('exec.loadingSeq')} />
          ) : layoutMode === 'canvas' ? (
            <ExecutionCanvas
              sequence={sequence}
              selected={selectedStep}
              onSelect={selectPick}
              onDeselect={dismissLog}
            />
          ) : (
            <>
              {sequence.error && (
                <Alert
                  type="error"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={
                    sequence.status === 'rejected'
                      ? t('exec.rejected')
                      : sequence.status === 'cancelled'
                        ? t('exec.cancelledRel')
                        : startupFailed
                          ? t('pipe.publishStartFailed')
                          : t('exec.failReason')
                  }
                  description={
                    <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{sequence.error}</div>
                  }
                />
              )}
              {/* 时间轴 */}
              <Card size="small" style={{ marginBottom: 12 }}>
                <Row gutter={8}>
                  <Col span={12}>
                    <Text type="secondary" style={{ fontSize: 11 }}>{t('exec.started')}</Text>
                    <div style={{ fontSize: 12 }}>
                      {sequence.started_at ? dayjs(sequence.started_at).format('MM-DD HH:mm:ss') : '—'}
                    </div>
                  </Col>
                  <Col span={12}>
                    <Text type="secondary" style={{ fontSize: 11 }}>{t('exec.ended')}</Text>
                    <div style={{ fontSize: 12 }}>
                      {sequence.finished_at ? dayjs(sequence.finished_at).format('MM-DD HH:mm:ss') : t('exec.runningEllipsis')}
                    </div>
                  </Col>
                </Row>
              </Card>

              {sequence.pipeline_variables && sequence.pipeline_variables.length > 0 && (
                <Card size="small" style={{ marginBottom: 12 }}>
                  <Text type="secondary" style={{ fontSize: 11 }}>🔧 {t('exec.vars')}</Text>
                  <div style={{ marginTop: 6, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                    {sequence.pipeline_variables.map((v) => (
                      <Tag key={v.name} color="cyan" style={{ fontSize: 11, margin: 0 }}>
                        <strong>{v.name}</strong>: {v.value || '—'}
                      </Tag>
                    ))}
                  </div>
                </Card>
              )}

              {/* 代码变更 */}
              {commitsData && (
                <Card
                  size="small"
                  title={<span style={{ fontSize: 12 }}>🔧 {t('exec.commits')}</span>}
                  extra={<Tag>{commitsData.commits.length}</Tag>}
                  style={{ marginBottom: 12 }}
                >
                  <div style={{ fontSize: 11, color: '#999', marginBottom: 4 }}>
                    {commitsData.range}
                  </div>
                  {commitsData.commits.length > 0 ? (
                    <div style={{ maxHeight: 120, overflowY: 'auto' }}>
                      {commitsData.commits.slice(0, 5).map((c) => (
                        <div key={c.id} style={{ fontSize: 11, padding: '4px 0', borderBottom: '1px dashed #eee' }}>
                          <Tooltip title={c.title}>
                            <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              <Tag color="cyan" style={{ fontFamily: 'monospace', margin: 0, marginRight: 4 }}>
                                {c.short_id.slice(0, 7)}
                              </Tag>
                              {c.title}
                            </span>
                          </Tooltip>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 11 }}>{t('exec.noCommitRange')}</Text>
                  )}
                </Card>
              )}

              {/* stage→job→step 树 */}
              {sequence.stages.map((stage, si) => (
                <Card
                  key={si}
                  size="small"
                  style={{ marginBottom: 8 }}
                  title={
                    <span style={{ fontSize: 13 }}>
                      {stage.status === 'success' ? '✅' : stage.status === 'failed' ? '❌' : stage.status === 'running' ? '▶️' : '⏸'} {stage.name}
                    </span>
                  }
                  extra={
                    <span style={{ fontSize: 11, color: '#999' }}>
                      {releaseStatusMeta(stage.status).text}
                    </span>
                  }
                >
                  {stage.jobs.map((job, ji) => (
                    <div key={ji} style={{ marginBottom: 8 }}>
                      <div style={{ fontSize: 12, marginBottom: 4, display: 'flex', alignItems: 'center' }}>
                        <Tag color="blue" style={{ margin: 0, marginRight: 4 }}>🔨 {job.name}</Tag>
                        {job.duration_label && (
                          <span style={{ fontSize: 11, color: '#999', fontFamily: 'monospace' }}>
                            {job.duration_label}
                          </span>
                        )}
                      </div>
                      {job.steps.map((step, sti) => {
                        const taskId = step.task_id ?? job.task_id
                        const isSelected =
                          selectedStep?.taskId === taskId && selectedStep?.stepOrder === step.order
                        const isFailed = step.status === 'failed' || step.status === 'timeout'
                        return (
                          <Button
                            key={sti}
                            size="small"
                            type={isSelected ? 'primary' : 'default'}
                            danger={isFailed}
                            block
                            style={{ marginBottom: 4, textAlign: 'left', fontFamily: 'monospace' }}
                            onClick={() => {
                              selectPick({
                                taskId,
                                stepOrder: step.order,
                                logIndex: step.log_index ?? step.order,
                              })
                            }}
                          >
                            <span style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <span>
                                {step.status === 'success' ? '✅' : isFailed ? '❌' : step.status === 'running' ? '▶️' : step.status === 'cancelled' ? '⛔' : '⏸'} {step.order + 1}. {stepLabel(step)}
                              </span>
                              {step.duration_label ? (
                                <span
                                  style={{
                                    fontSize: 11,
                                    color: isFailed ? '#ff4d4f' : isSelected ? 'inherit' : '#999',
                                  }}
                                >
                                  {step.duration_label}
                                </span>
                              ) : step.status === 'running' ? (
                                <span style={{ fontSize: 11, color: '#1677ff' }}>{t('deploy.inProgress')}</span>
                              ) : null}
                            </span>
                          </Button>
                        )
                      })}
                    </div>
                  ))}
                </Card>
              ))}
              {canDiagnose && (
                <Button
                  type="primary"
                  icon={<RobotOutlined />}
                  block
                  onClick={() => setDiagnoseOpen(true)}
                >
                  {t('exec.diagnoseFail')}
                </Button>
              )}
            </>
          )}
        </div>

        {/* 右侧大日志区：画布点空白时收起 */}
        {showLogPane ? (
        <div
          style={{
            flex: 1,
            minWidth: 0,
            minHeight: isMobile ? 220 : undefined,
            display: 'flex',
            flexDirection: 'column',
            background: '#1e1e1e',
            color: '#d4d4d4',
            fontFamily: 'Consolas, Monaco, monospace',
            fontSize: 13,
            overflow: 'hidden',
          }}
        >
          {/* 当前 step 头部 */}
          <div
            style={{
              padding: '10px 16px',
              borderBottom: '1px solid #333',
              background: '#252526',
              color: '#d4d4d4',
              fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
              flexShrink: 0,
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <span>
              {startupFailed
                ? t('pipe.publishStartFailed')
                : currentStep
                  ? t('exec.stepN', { n: currentStep.step.order + 1, name: stepLabel(currentStep.step) })
                  : layoutMode === 'canvas'
                    ? t('exec.clickNode')
                    : t('exec.pickStep')}
              {currentStep?.step.duration_label && (
                <span style={{ marginLeft: 10, fontSize: 12, color: '#9ca3af' }}>
                  {t('exec.duration', { time: currentStep.step.duration_label })}
                </span>
              )}
              {startupFailed && (
                <Tag color="error" style={{ marginLeft: 10 }}>
                  {t('exec.stepNotRun')}
                </Tag>
              )}
              {currentStep && (currentStep.step.status === 'failed' || currentStep.step.status === 'timeout') && (
                <Tag color="error" style={{ marginLeft: 10 }}>
                  {t('exec.stepFailed')}
                </Tag>
              )}
            </span>
            {sequence?.is_running &&
              (followLive ? (
                <span style={{ fontSize: 12, color: '#4ec9b0' }}>◉ {t('exec.following')}</span>
              ) : (
                // 不给这个入口的话，手动点过一次步骤就再也回不到「自动跟着跑」了
                <Button
                  size="small"
                  type="link"
                  onClick={() => {
                    setLogDismissed(false)
                    setFollowLive(true)
                    if (livePick) setSelectedStep(livePick)
                  }}
                  style={{ fontSize: 12, padding: 0, height: 'auto' }}
                >
                  ↓ {t('exec.backToLive')}
                </Button>
              ))}
          </div>

          {/* 日志本体（占满剩余高度） */}
          <div
            ref={logPaneRef}
            style={{
              flex: 1,
              padding: '12px 16px',
              overflowY: 'auto',
              lineHeight: 1.6,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {tonedLines.length === 0 ? (
              sequence?.error && (!currentStep?.step.task_id || startupFailed) ? (
                sequence.error.split('\n').map((line, i) => (
                  <div key={i} style={LOG_ROW_ERROR}>
                    <span style={LOG_GUTTER}>{i + 1}</span>
                    <span style={LOG_TEXT.error}>{line || ' '}</span>
                  </div>
                ))
              ) : (
                <span style={{ color: '#666' }}>
                  {currentStep && !currentStep.step.task_id
                    ? t('exec.notStartedLog')
                    : logLoading
                      ? t('exec.loadingLog')
                      : taskLogLines.length
                        ? t('exec.noStepLog')
                        : t('exec.noLog')}
                </span>
              )
            ) : (
              <>
                {tonedLines.map(({ line, tone }, i) => (
                  <div
                    key={i}
                    data-log-line={i}
                    style={tone === 'error' ? LOG_ROW_ERROR : LOG_ROW}
                  >
                    <span style={LOG_GUTTER}>{i + 1}</span>
                    <span style={LOG_TEXT[tone]}>{line || ' '}</span>
                  </div>
                ))}
                {errorLines.length > 0 && (
                  <div
                    ref={errorRecapRef}
                    style={{
                      marginTop: 12,
                      padding: '10px 12px',
                      background: '#3f1d1d',
                      border: '1px solid #7f1d1d',
                    }}
                  >
                    <div style={{ color: '#fca5a5', fontSize: 12, marginBottom: 8 }}>
                      {t('exec.errorRecap', { n: errorLines.length })}
                    </div>
                    {errorLines.map((row, i) => (
                      <div key={i} style={LOG_ROW}>
                        <span style={LOG_TEXT.error}>{row.line}</span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
            <div ref={logBottomRef} />
          </div>
        </div>
        ) : null}
      </div>
      <DiagnoseDrawer
        releaseId={releaseId ? Number(releaseId) : null}
        open={diagnoseOpen}
        onClose={() => setDiagnoseOpen(false)}
      />
    </div>
  )
}
