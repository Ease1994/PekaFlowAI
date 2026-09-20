import { useState, useEffect, useMemo } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Card,
  Tabs,
  Button,
  Space,
  Modal,
  Input,
  Form,
  Select,
  Radio,
  message,
  Spin,
  Dropdown,
  Tag,
  Tooltip,
  Alert,
} from 'antd'
import {
  ArrowLeftOutlined,
  SaveOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  DeleteOutlined,
  CopyOutlined,
  HolderOutlined,
  MoreOutlined,
} from '@ant-design/icons'
import { get, put, postR, del } from '@/api/client'
import { envColor, envLabel } from '@/env'
import { useAuthStore } from '@/stores/auth'
import type {
  Pipeline,
  PipelineGraph,
  GraphStage,
  GraphJob,
  GraphStep,
  GraphTrigger,
  Plugin,
  Variable,
} from '@/api/types'
import PluginSelector from '@/components/pipeline/PluginSelector'
import JobTypeSelector from '@/components/pipeline/JobTypeSelector'
import ConfigDrawer, { type ConfigTarget } from '@/components/pipeline/ConfigDrawer'
import { VariableEditor } from '@/components/pipeline/VariableEditor'
import TriggerPanel from '@/components/pipeline/TriggerPanel'
import EditorCanvas, { type EditorCanvasActions } from '@/components/pipeline/EditorCanvas'
import ReleaseGateFields from '@/components/ReleaseGateFields'
import { useT } from '@/i18n'
import type { StartParam } from '@/components/pipeline/RunPipelineFields'
import { applyCanvasLink, applyCanvasSplitParallel, applyCanvasUnlink, dropCut, isCanvasGraphErr, OPEN_CUTS_SAVE_ERROR, pruneOpenCuts } from '@/utils/canvasGraph'
import {
  editorApprovalRequired,
  graphManifestDefault,
  graphNeedsManifest,
  manifestInputRequired,
  pipelineNeedsManifest,
} from '@/utils/releaseGate'

const AGENT_ICON: Record<string, string> = { linux: '🐧', windows: '🪟', macos: '🍎', any: '🌐' }
/** 新建 Job 时的默认名称键，按操作系统取对应句子。 */
const AGENT_NAME_KEY: Record<string, string> = {
  linux: 'pipe.envLinux',
  windows: 'pipe.envWindows',
  macos: 'pipe.envMacos',
  any: 'pipe.envAny',
}

/**
 * 编辑器初始视图：URL ?mode= 优先（新建选画布会带上），再读本机偏好。
 * 流水线自己的 editor_view 在详情返回后再对齐一次。
 */
function readInitialEditorView(pipelineId: string | undefined): 'form' | 'canvas' {
  const q = new URLSearchParams(window.location.search).get('mode')
  if (q === 'canvas' || q === 'form') return q
  try {
    const stored = localStorage.getItem(`rp-editor-view:${pipelineId || ''}`)
    if (stored === 'canvas' || stored === 'form') return stored
  } catch {
    /* 隐私模式读不了就走默认列表 */
  }
  return 'form'
}

export default function PipelineEditor() {
  const { pipelineId } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const user = useAuthStore((s) => s.user)
  const t = useT()

  const [graph, setGraph] = useState<PipelineGraph | null>(null)
  const [configTarget, setConfigTarget] = useState<ConfigTarget | null>(null)
  const [jobTypePicker, setJobTypePicker] = useState<string | null>(null)
  const [pluginPicker, setPluginPicker] = useState<{
    stageId: string
    jobId: string
    /** 有值=重选已存在的 step；undefined=新增 */
    stepIdx?: number
    /** 新增时插到该下标后面；缺省追加到末尾 */
    afterIndex?: number
  } | null>(null)
  const [releaseModalOpen, setReleaseModalOpen] = useState(false)
  const [releaseVersion, setReleaseVersion] = useState('v1.0.0')
  /** 保存并执行弹窗：应急跳审勾选 */
  const [releaseBypass, setReleaseBypass] = useState(false)
  /** 保存并执行弹窗：跳审原因 */
  const [releaseBypassReason, setReleaseBypassReason] = useState('')
  /** 保存并执行弹窗：本次发布清单。步骤未写死文件时必填，空着会拒绝执行。 */
  const [releaseManifest, setReleaseManifest] = useState('')
  /** 保存并执行弹窗：执行时显示的变量 */
  const [releaseStartParams, setReleaseStartParams] = useState<StartParam[]>([])
  const [releaseRunParams, setReleaseRunParams] = useState<Record<string, string>>({})
  // 基础信息（名称/描述）：在「基础设置」里改，跟编排一起由左上角「保存」提交
  const [basic, setBasic] = useState({ name: '', description: '', approval_mode: 'inherit' })
  const [viewMode, setViewMode] = useState<'form' | 'canvas'>(() => readInitialEditorView(pipelineId))
  const queryClient = useQueryClient()

  const { data: pipeline } = useQuery({
    queryKey: ['pipeline', pipelineId],
    queryFn: () => get<Pipeline>(`/pipelines/${pipelineId}`),
  })

  const { data: graphData, isLoading } = useQuery({
    queryKey: ['pipeline-graph', pipelineId],
    queryFn: () => get<PipelineGraph>(`/pipelines/${pipelineId}/graph`),
  })

  const { data: plugins = [] } = useQuery({
    queryKey: ['plugins'],
    queryFn: () => get<Plugin[]>('/store/plugins'),
  })

  useEffect(() => {
    if (graphData) setGraph(graphData)
  }, [graphData])

  useEffect(() => {
    if (pipeline)
      setBasic({
        name: pipeline.name || '',
        description: pipeline.description || '',
        approval_mode: pipeline.approval_mode || 'inherit',
      })
  }, [pipeline])

  useEffect(() => {
    if (!pipeline) return
    const q = searchParams.get('mode')
    if (q === 'canvas' || q === 'form') return
    if (pipeline.editor_view === 'canvas' || pipeline.editor_view === 'form') {
      setViewMode(pipeline.editor_view)
    }
  }, [pipeline?.id, pipeline?.editor_view])

  /** 切换列表/画布：URL、本机、流水线各记一份，下次打开还是这个视图。 */
  const persistView = (next: 'form' | 'canvas') => {
    setViewMode(next)
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('mode', next)
    setSearchParams(nextParams, { replace: true })
    try {
      localStorage.setItem(`rp-editor-view:${pipelineId || ''}`, next)
    } catch {
      /* 写不了本机也不挡当次切换 */
    }
    if (pipeline?.can_update === false) return
    put(`/pipelines/${pipelineId}`, { editor_view: next })
      .then(() => queryClient.invalidateQueries({ queryKey: ['pipeline', pipelineId] }))
      .catch(() => undefined)
  }

  // ---- 图状态操作 ----
  const updateStages = (updater: (s: GraphStage[]) => GraphStage[]) => {
    setGraph((g) => (g ? { ...g, stages: updater(g.stages) } : g))
  }

  const updateTriggers = (triggers: GraphTrigger[]) => {
    setGraph((g) => (g ? { ...g, triggers } : g))
  }

  const updateVariables = (variables: Variable[]) => {
    setGraph((g) => (g ? { ...g, variables } : g))
  }

  const addStageAfter = (afterStageId: string | null) => {
    updateStages((stages) => {
      const order = stages.length + 1
      const newStage: GraphStage = {
        id: `stage-${Date.now()}`,
        name: `stage-${order}`,
        order,
        jobs: [],
      }
      if (afterStageId === null || stages.length === 0) return [...stages, newStage]
      const idx = stages.findIndex((s) => s.id === afterStageId)
      if (idx === -1) return [...stages, newStage]
      const result = [...stages]
      result.splice(idx + 1, 0, newStage)
      return result.map((s, i) => ({ ...s, order: i + 1 }))
    })
  }

  // 删除是不可撤销的（本地图改完还要点保存，但误删的内容找不回来），
  // 所以只在真的会丢东西时拦一道，空的直接删不打扰。
  const confirmRemove = (title: string, content: string, onOk: () => void) => {
    Modal.confirm({
      title,
      content,
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk,
    })
  }

  const removeStage = (stageId: string) => {
    const doRemove = () =>
      updateStages((stages) =>
        stages.filter((s) => s.id !== stageId).map((s, i) => ({ ...s, order: i + 1 }))
      )
    const stage = graph?.stages.find((s) => s.id === stageId)
    const jobCount = stage?.jobs.length ?? 0
    if (jobCount === 0) return doRemove()
    const stepCount = stage?.jobs.reduce((n, j) => n + j.steps.length, 0) ?? 0
    confirmRemove(
      t('pipe.deleteStageTitle', { name: stage?.name ?? '' }),
      t('pipe.deleteStageBody', { jobs: jobCount, steps: stepCount }),
      doRemove,
    )
  }

  const addJob = (stageId: string, agent: 'linux' | 'windows' | 'macos') => {
    updateStages((stages) =>
      stages.map((s) => {
        if (s.id !== stageId) return s
        const jobId = `${s.order}-${s.jobs.length + 1}`
        const newJob: GraphJob = {
          id: jobId,
          name: t(AGENT_NAME_KEY[agent] || 'pipe.envAny'),
          agent,
          agent_icon: AGENT_ICON[agent],
          steps: [],
        }
        return { ...s, jobs: [...s.jobs, newJob] }
      })
    )
  }

  const removeJob = (stageId: string, jobId: string) => {
    const doRemove = () =>
      updateStages((stages) =>
        stages.map((s) =>
          s.id === stageId ? { ...s, jobs: s.jobs.filter((j) => j.id !== jobId) } : s
        )
      )
    const job = graph?.stages.find((s) => s.id === stageId)?.jobs.find((j) => j.id === jobId)
    if (!job || job.steps.length === 0) return doRemove()
    confirmRemove(
      t('pipe.deleteJobTitle', { name: job.name || job.id }),
      t('pipe.deleteJobBody', { n: job.steps.length }),
      doRemove,
    )
  }

  const addStep = (stageId: string, jobId: string, plugin: Plugin, afterIndex?: number) => {
    updateStages((stages) =>
      stages.map((s) => {
        if (s.id !== stageId) return s
        return {
          ...s,
          jobs: s.jobs.map((j) => {
            if (j.id !== jobId) return j
            const newStep: GraphStep = {
              name: pluginStepName(plugin),
              plugin: plugin.name,
              display_name: plugin.display_name,
              icon: getCategoryEmoji(plugin.category),
              order: j.steps.length + 1,
              with: schemaDefaults(plugin),
              built_in: true,
            }
            const steps = [...j.steps]
            const at = afterIndex == null ? steps.length : Math.min(steps.length, afterIndex + 1)
            steps.splice(at, 0, newStep)
            return { ...j, steps: steps.map((st, i) => ({ ...st, order: i + 1 })) }
          }),
        }
      })
    )
  }

  // 重选插件：替换现有 step 的 plugin，参数（with）清空（不同插件参数不同）
  const updateStepPlugin = (stageId: string, jobId: string, stepIdx: number, plugin: Plugin) => {
    updateStages((stages) =>
      stages.map((s) => {
        if (s.id !== stageId) return s
        return {
          ...s,
          jobs: s.jobs.map((j) => {
            if (j.id !== jobId) return j
            return {
              ...j,
              steps: j.steps.map((st, i) => {
                if (i !== stepIdx) return st
                return {
                  name: nextStepNameAfterPluginChange(st, plugin),
                  plugin: plugin.name,
                  display_name: plugin.display_name,
                  icon: getCategoryEmoji(plugin.category),
                  order: st.order,
                  with: schemaDefaults(plugin), // 换插件即换 schema，参数重置为新插件的默认值
                  built_in: true,
                }
              }),
            }
          }),
        }
      })
    )
  }

  // 复制一个步骤，插到原步骤后面。IIS 发布这类流水线里，停应用池/停站点/起应用池/起站点
  // 参数只差一两个字段，从头配一遍很费事，复制完改个名字最省心
  const duplicateStep = (stageId: string, jobId: string, stepIndex: number) => {
    // 插入会让后面的步骤下标整体后移，抽屉记的还是老下标，改动会落到隔壁那一步
    setConfigTarget(null)
    updateStages((stages) =>
      stages.map((s) => {
        if (s.id !== stageId) return s
        return {
          ...s,
          jobs: s.jobs.map((j) => {
            if (j.id !== jobId) return j
            const src = j.steps[stepIndex]
            if (!src) return j
            const copy: GraphStep = {
              ...src,
              // 参数必须深拷贝：浅拷贝下两个步骤共用同一个 with 对象，
              // 改副本的应用池名会把原步骤一起改掉
              with: JSON.parse(JSON.stringify(src.with || {})),
              name: t('pipe.copyName', { name: (src.name || src.display_name || src.plugin).trim() }),
            }
            const steps = [...j.steps]
            steps.splice(stepIndex + 1, 0, copy)
            return { ...j, steps: steps.map((st, i) => ({ ...st, order: i + 1 })) }
          }),
        }
      })
    )
    message.success(t('pipe.stepCopied'))
  }

  const removeStep = (stageId: string, jobId: string, stepIndex: number) => {
    const doRemove = () => {
      // 抽屉是按下标认步骤的，删完下标就串位了。画布和抽屉能同时操作，
      // 真让它接着编辑，改动会落到隔壁那一步上
      setConfigTarget(null)
      updateStages((stages) =>
        stages.map((s) => {
          if (s.id !== stageId) return s
          return {
            ...s,
            jobs: s.jobs.map((j) => {
              if (j.id !== jobId) return j
              return {
                ...j,
                steps: j.steps
                  .filter((_, idx) => idx !== stepIndex)
                  .map((st, i) => ({ ...st, order: i + 1 })),
              }
            }),
          }
        })
      )
    }
    const step = graph?.stages
      .find((s) => s.id === stageId)
      ?.jobs.find((j) => j.id === jobId)?.steps[stepIndex]
    // 删除图标就挨着步骤行，配过参数的步骤误删代价高，配过才拦
    if (!step || Object.keys(step.with || {}).length === 0) return doRemove()
    confirmRemove(
      t('pipe.deleteStepTitle', { name: step.name || step.display_name || step.plugin }),
      t('pipe.deleteStepConfigured'),
      doRemove,
    )
  }

  const reorderStep = (stageId: string, jobId: string, fromIdx: number, toIdx: number) => {
    if (fromIdx === toIdx) return
    setConfigTarget(null) // 同上：换了位置，抽屉手里的下标就不指向原来那步了
    updateStages((stages) =>
      stages.map((s) => {
        if (s.id !== stageId) return s
        return {
          ...s,
          jobs: s.jobs.map((j) => {
            if (j.id !== jobId) return j
            const steps = [...j.steps]
            const [moved] = steps.splice(fromIdx, 1)
            steps.splice(toIdx, 0, moved)
            return { ...j, steps: steps.map((st, i) => ({ ...st, order: i + 1 })) }
          }),
        }
      })
    )
  }

  // 抽屉里的表单基于 configTarget 里的那份对象拼装改动，所以每次落盘都要把它一起更新。
  // 少了这步，configTarget 会一直停在打开抽屉那一刻：先改名字再改参数，那次参数改动
  // 带的还是旧快照里的空名字，刚填的名字就被冲回插件默认名了。
  const saveStage = (stage: GraphStage) => {
    updateStages((stages) => stages.map((s) => (s.id === stage.id ? stage : s)))
    if (configTarget?.type === 'stage') setConfigTarget({ ...configTarget, stage })
  }
  const saveJob = (job: GraphJob) => {
    if (!configTarget || configTarget.type !== 'job') return
    const { stageIndex, jobIndex } = configTarget
    updateStages((stages) =>
      stages.map((s, i) => {
        if (i !== stageIndex) return s
        return {
          ...s,
          jobs: s.jobs.map((j, ji) => (ji === jobIndex ? job : j)),
        }
      })
    )
    setConfigTarget({ ...configTarget, job })
  }
  const saveStep = (step: GraphStep) => {
    if (!configTarget || configTarget.type !== 'step') return
    const { stageIndex, jobIndex, stepIndex } = configTarget
    updateStages((stages) =>
      stages.map((s, i) => {
        if (i !== stageIndex) return s
        return {
          ...s,
          jobs: s.jobs.map((j, ji) => {
            if (ji !== jobIndex) return j
            return {
              ...j,
              steps: j.steps.map((st, si) => (si === stepIndex ? step : st)),
            }
          }),
        }
      })
    )
    setConfigTarget({ ...configTarget, step })
  }

  // ---- 保存与发布 ----
  const basicDirty =
    !!pipeline &&
    (basic.name.trim() !== (pipeline.name || '') ||
      (basic.description || '') !== (pipeline.description || '') ||
      basic.approval_mode !== (pipeline.approval_mode || 'inherit'))

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (basicDirty && !basic.name.trim()) throw new Error(t('pipe.nameRequired'))
      if (!graph) throw new Error(t('pipe.graphNotReady'))
      const cuts = pruneOpenCuts(graph.stages, graph.open_cuts)
      if (cuts.length) throw new Error(OPEN_CUTS_SAVE_ERROR())
      // 把当前断开记录原样交给后端；有值时后端也会拒，避免只靠前端拦住
      await put<Pipeline>(`/pipelines/${pipelineId}/graph`, { ...graph, open_cuts: cuts })
      if (basicDirty) {
        await put(`/pipelines/${pipelineId}`, {
          name: basic.name.trim(),
          description: basic.description || '',
          approval_mode: basic.approval_mode,
        })
      }
    },
    onSuccess: () => {
      message.success(basicDirty ? t('pipe.savedWithBasic') : t('pipe.savedGraph'))
      queryClient.invalidateQueries({ queryKey: ['pipeline', pipelineId] })
      queryClient.invalidateQueries({ queryKey: ['pipelines'] })
    },
    onError: (e: Error) => message.error(e.message || t('pipe.saveFailed')),
  })

  const confirmDelete = () => {
    Modal.confirm({
      title: t('pipe.deletePipelineTitle', { name: pipeline?.name || '' }),
      content:
        t('pipe.deletePipelineBody'),
      okText: t('pipe.confirmDelete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await del(`/pipelines/${pipelineId}`)
        message.success(t('pipe.trashed'))
        queryClient.invalidateQueries({ queryKey: ['pipelines'] })
        queryClient.invalidateQueries({ queryKey: ['recycled-pipelines'] })
        navigate(pipeline?.project_id ? `/projects/${pipeline.project_id}` : '/projects')
      },
    })
  }

  const releaseMutation = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        pipeline_id: Number(pipelineId),
        version: releaseVersion,
        // 代码版本（commit SHA）由后端自动从 git-checkout 步骤拉取，Rebuild 复用
        strategy: 'rolling',
        trigger_by: 'manual',
      }
      if (releaseBypass && pipeline?.allow_emergency_bypass) {
        body.emergency_bypass = true
        body.emergency_bypass_reason = releaseBypassReason.trim()
      }
      if (graphNeedsManifest(graph) || pipelineNeedsManifest(pipeline)) {
        body.deploy_manifest = releaseManifest
      }
      if (releaseStartParams.length) body.run_params = releaseRunParams
      return postR<{ status: string; id: number; build_number?: number }>('/releases', body)
    },
    onSuccess: (res) => {
      const r = res.data
      setReleaseModalOpen(false)
      setReleaseBypass(false)
      setReleaseBypassReason('')
      if (!r) return
      const num = r.build_number || r.id
      if (r.status === 'failed' || (res.message && res.message !== 'ok' && res.message.includes(t('pipe.startFailedToken')))) {
        message.error(res.message && res.message !== 'ok' ? res.message : t('pipe.publishStartFailed'), 8)
      } else if (r.status === 'pending') {
        message.success(t('pipe.submittedPending', { n: num }))
      } else if (releaseBypass) {
        message.warning(t('pipe.emergencyRan', { n: num }))
      } else {
        message.success(t('pipe.queued'))
      }
      // 跳转到本流水线的发布记录/执行详情（不要进管理员用的「发布管理」）
      navigate(`/executions/${pipelineId}/${r.id}`)
    },
  })

  const editorActions: EditorCanvasActions = useMemo(
    () => ({
      onOpenConfig: (target) => setConfigTarget(target),
      onAddJob: (stageId) => setJobTypePicker(stageId),
      onAddStep: (stageId, jobId, afterIndex) => setPluginPicker({ stageId, jobId, afterIndex }),
      onAddStage: (afterStageId) => addStageAfter(afterStageId),
      onCloseConfig: () => setConfigTarget(null),
      onLayoutChange: (layout) => setGraph((g) => (g ? { ...g, layout } : g)),
      onLink: (source, target) => {
        setGraph((g) => {
          if (!g) return g
          const result = applyCanvasLink(g.stages, source, target)
          if (isCanvasGraphErr(result)) {
            message.warning(result.error)
            return g
          }
          if (result.hint) message.success(result.hint)
          const cuts = dropCut(g.open_cuts, source, target).filter(
            (id) => id !== result.linked_edge_id,
          )
          return {
            ...g,
            stages: result.stages,
            open_cuts: pruneOpenCuts(result.stages, cuts),
          }
        })
        setConfigTarget(null)
      },
      onUnlink: (source, target, positions) => {
        setGraph((g) => {
          if (!g) return g
          const result = applyCanvasUnlink(g.stages, source, target)
          if (isCanvasGraphErr(result)) {
            message.warning(result.error)
            return g
          }
          const cuts = pruneOpenCuts(g.stages, g.open_cuts)
          if (result.cut_edge_id) cuts.push(result.cut_edge_id)
          message.success(result.hint)
          return {
            ...g,
            layout: { ...(g.layout || {}), ...positions },
            open_cuts: [...new Set(cuts)],
          }
        })
        setConfigTarget(null)
      },
      onSplitParallel: (step) => {
        setGraph((g) => {
          if (!g) return g
          const result = applyCanvasSplitParallel(g.stages, step)
          if (isCanvasGraphErr(result)) {
            message.warning(result.error)
            return g
          }
          message.success(result.hint)
          return { ...g, stages: result.stages, open_cuts: pruneOpenCuts(result.stages, g.open_cuts) }
        })
        setConfigTarget(null)
      },
      onDuplicateStep: (stageId, jobId, stepIndex) => duplicateStep(stageId, jobId, stepIndex),
      onDeleteStage: (stageId) => removeStage(stageId),
      onDeleteJob: (stageId, jobId) => removeJob(stageId, jobId),
      onDeleteStep: (stageId, jobId, stepIndex) => removeStep(stageId, jobId, stepIndex),
      onMoveStep: (stageId, jobId, from, to) => reorderStep(stageId, jobId, from, to),
    }),
    [graph, t],
  )

  useEffect(() => {
    if (viewMode !== 'canvas') return undefined
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const el = e.target as HTMLElement | null
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return
      if (!configTarget) return
      e.preventDefault()
      if (configTarget.type === 'stage') removeStage(configTarget.stage.id)
      else if (configTarget.type === 'job') {
        const stage = graph?.stages[configTarget.stageIndex]
        if (stage) removeJob(stage.id, configTarget.job.id)
      } else if (configTarget.type === 'step') {
        const stage = graph?.stages[configTarget.stageIndex]
        const job = stage?.jobs[configTarget.jobIndex]
        if (stage && job) removeStep(stage.id, job.id, configTarget.stepIndex)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [viewMode, configTarget, graph])

  if (isLoading || !graph) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin size="large" />
      </div>
    )
  }

  const needReleaseManifest = graphNeedsManifest(graph) || pipelineNeedsManifest(pipeline)
  const needReleaseApproval = editorApprovalRequired(pipeline, basic.approval_mode)
  const canReleaseBypass = !!pipeline?.allow_emergency_bypass
  const blockReleaseManifest = manifestInputRequired(
    needReleaseManifest,
    graphManifestDefault(graph) || pipeline?.deploy_manifest_default || '',
    releaseManifest,
  )

  return (
    <div>
      {/* 顶部工具栏 */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space>
            <Button icon={<ArrowLeftOutlined />} onClick={() => navigate(-1)}>
              {t('pipe.back')}
            </Button>
            <span style={{ fontWeight: 600, fontSize: 16 }}>
              {pipeline?.name}（v{graph.version}）
            </span>
            <Tooltip title={viewMode === 'canvas' ? t('pipe.switchToList') : t('pipe.switchToCanvas')}>
              <Tag
                color={viewMode === 'canvas' ? 'purple' : 'blue'}
                style={{ cursor: 'pointer', userSelect: 'none' }}
                onClick={() => persistView(viewMode === 'form' ? 'canvas' : 'form')}
              >
                {t('pipe.freeMode')}（{viewMode === 'canvas' ? t('pipe.canvasArrange') : t('pipe.listArrange')}）
              </Tag>
            </Tooltip>
          </Space>
          <Space>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={saveMutation.isPending}
              onClick={() => saveMutation.mutate()}
            >
              {t('common.save')}
            </Button>
            <Button
              type="primary"
              ghost
              icon={<PlayCircleOutlined />}
              onClick={async () => {
                const cuts = pruneOpenCuts(graph.stages, graph.open_cuts)
                if (cuts.length) {
                  message.error(OPEN_CUTS_SAVE_ERROR())
                  return
                }
                setReleaseBypass(false)
                setReleaseBypassReason('')
                setReleaseManifest(graphManifestDefault(graph) || pipeline?.deploy_manifest_default || '')
                try {
                  const list = await get<StartParam[]>(`/pipelines/${pipelineId}/start-params`)
                  setReleaseStartParams(list)
                  setReleaseRunParams(Object.fromEntries(list.map((p) => [p.name, p.default_value ?? ''])))
                } catch {
                  setReleaseStartParams([])
                  setReleaseRunParams({})
                }
                setReleaseModalOpen(true)
              }}
            >
              {t('pipe.saveAndRun')}
            </Button>
            <Dropdown
              menu={{
                items: [{ key: 'delete', label: t('pipe.deletePipeline'), danger: true }],
                onClick: ({ key }) => {
                  if (key === 'delete') confirmDelete()
                },
              }}
            >
              <Button icon={<MoreOutlined />} />
            </Dropdown>
          </Space>
        </Space>
      </Card>

      {/* Tab */}
      <Tabs
        size="small"
        items={[
          {
            key: 'pipeline',
            label: t('pipe.graphTab'),
            children:
              viewMode === 'canvas' ? (
                <div style={{ height: 'calc(100vh - 210px)', minHeight: 520 }}>
                  <EditorCanvas graph={graph} configTarget={configTarget} actions={editorActions} />
                </div>
              ) : (
              <div style={{ padding: '32px 16px', overflowX: 'auto', minHeight: 500, background: '#fafbfc' }}>
                <div style={{ display: 'flex', alignItems: 'flex-start' }}>
                  {graph.stages.length === 0 ? (
                    <Button
                      type="primary"
                      size="large"
                      icon={<PlusOutlined />}
                      onClick={() => addStageAfter(null)}
                    >
                      {t('pipe.addFirstStage')}
                    </Button>
                  ) : (
                    graph.stages.map((stage, sIdx) => (
                      <div key={stage.id} style={{ display: 'flex', alignItems: 'center' }}>
                        <StageColumn
                          stage={stage}
                          stageIndex={sIdx}
                          onOpenConfig={(target) => setConfigTarget(target)}
                          onAddJob={() => setJobTypePicker(stage.id)}
                          onAddStep={(jobId) => setPluginPicker({ stageId: stage.id, jobId })}
                          onDeleteStage={() => removeStage(stage.id)}
                          onDeleteJob={(jobId) => removeJob(stage.id, jobId)}
                          onDeleteStep={(jobId, stepIdx) => removeStep(stage.id, jobId, stepIdx)}
                          onDuplicateStep={(jobId, stepIdx) =>
                            duplicateStep(stage.id, jobId, stepIdx)
                          }
                          onReorderStep={(jobId, from, to) => reorderStep(stage.id, jobId, from, to)}
                        />
                        {/* Stage 串行箭头 */}
                        <div
                          style={{
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            margin: '0 6px',
                            paddingTop: 32,
                          }}
                        >
                          <span style={{ fontSize: 11, color: '#999' }}>{t('pipe.serial')}</span>
                          <span style={{ fontSize: 20, color: '#b0b7c3', margin: '2px 0' }}>→</span>
                          <Button
                            shape="circle"
                            icon={<PlusOutlined />}
                            size="small"
                            type="dashed"
                            onClick={() => addStageAfter(stage.id)}
                            title={t('pipe.addStageAfter')}
                          />
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
              ),
          },
          {
            key: 'trigger',
            label: t('pipe.triggersTab'),
            children: (
              <TriggerPanel
                triggers={graph.triggers}
                onChange={updateTriggers}
                pipelineId={Number(pipelineId)}
              />
            ),
          },
          {
            key: 'variables',
            label: t('pipe.varsTab'),
            children: (
              <div style={{ padding: '16px 20px 24px' }}>
                <VariableEditor variables={graph.variables || []} onChange={updateVariables} />
              </div>
            ),
          },
          {
            key: 'setting',
            label: basicDirty ? t('pipe.basicTabDirty') : t('pipe.basicTab'),
            children: (
              <div style={{ padding: '24px 16px', maxWidth: 620 }}>
                <Form layout="vertical">
                  <Form.Item
                    label={t("pipe.pipelineName")}
                    required
                    validateStatus={basic.name.trim() ? undefined : 'error'}
                    help={basic.name.trim() ? '' : t('pipe.nameEmpty')}
                  >
                    <Input
                      value={basic.name}
                      maxLength={64}
                      placeholder={t("pipe.namePlaceholder")}
                      onChange={(e) => setBasic((b) => ({ ...b, name: e.target.value }))}
                    />
                  </Form.Item>
                  <Form.Item label={t("common.description")}>
                    <Input.TextArea
                      rows={3}
                      value={basic.description}
                      maxLength={255}
                      placeholder={t("pipe.descPlaceholder")}
                      onChange={(e) => setBasic((b) => ({ ...b, description: e.target.value }))}
                    />
                  </Form.Item>
                  <Form.Item label={t("pipe.group")}>
                    <Space>
                      <Tag color={envColor(pipeline?.group_type)}>
                        {pipeline?.group_type ? envLabel(pipeline.group_type) : '—'}
                      </Tag>
                      {pipeline?.approval_required ? <Tag color="gold">{t('pipe.needsApproval')}</Tag> : <Tag>{t('pipe.noApproval')}</Tag>}
                      {pipeline?.allow_emergency_bypass ? <Tag color="volcano">{t('pipe.canBypass')}</Tag> : null}
                    </Space>
                    <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>
                      {t('pipe.overrideEnvHint')}
                    </div>
                  </Form.Item>
                  <Form.Item label={t("pipe.releaseApproval")}>
                    <Radio.Group
                      value={basic.approval_mode}
                      onChange={(e) => setBasic((b) => ({ ...b, approval_mode: e.target.value }))}
                    >
                      <Radio value="inherit">
                        {t('pipe.followEnv')}（
                        {pipeline?.group_approval_required ? t('pipe.currentlyRequired') : t('pipe.currentlyExempt')}）
                      </Radio>
                      <Radio value="force">{t("pipe.forceApprove")}</Radio>
                      <Radio value="exempt" disabled={!pipeline?.can_exempt_approval}>
                        {t('pipe.exemptApprove')}
                      </Radio>
                    </Radio.Group>
                    <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>
                      {pipeline?.can_exempt_approval
                        ? t('pipe.overrideHint')
                        : t('pipe.noExemptPerm')}
                    </div>
                  </Form.Item>
                </Form>
                <div style={{ color: '#999', fontSize: 12 }}>
                  {t('pipe.saveWithGraphHint')}
                </div>
              </div>
            ),
          },
        ]}
      />

      {/* 配置 Drawer */}
      <ConfigDrawer
        target={configTarget}
        plugins={plugins}
        variables={graph.variables || []}
        projectId={pipeline?.project_id}
        pipelineId={pipeline?.id}
        envCode={pipeline?.group_type}
        onClose={() => setConfigTarget(null)}
        onSaveStage={(s) => {
          saveStage(s)
        }}
        onSaveJob={(j) => saveJob(j)}
        onSaveStep={(s) => saveStep(s)}
        onDeleteStage={() => {
          if (configTarget?.type === 'stage') removeStage(configTarget.stage.id)
          setConfigTarget(null)
        }}
        onDeleteJob={() => {
          if (configTarget?.type === 'job') {
            const { stageIndex, jobIndex } = configTarget
            const stage = graph.stages[stageIndex]
            const job = stage?.jobs[jobIndex]
            if (stage && job) removeJob(stage.id, job.id)
          }
          setConfigTarget(null)
        }}
        onDeleteStep={() => {
          if (configTarget?.type === 'step') {
            const { stageIndex, jobIndex, stepIndex } = configTarget
            const stage = graph.stages[stageIndex]
            const job = stage?.jobs[jobIndex]
            if (stage && job) removeStep(stage.id, job.id, stepIndex)
          }
          setConfigTarget(null)
        }}
        onDuplicateStep={() => {
          if (configTarget?.type === 'step') {
            const { stageIndex, jobIndex, stepIndex } = configTarget
            const stage = graph.stages[stageIndex]
            const job = stage?.jobs[jobIndex]
            if (stage && job) duplicateStep(stage.id, job.id, stepIndex)
          }
        }}
        onReselectPlugin={() => {
          if (configTarget?.type === 'step') {
            const { stageIndex, jobIndex, stepIndex } = configTarget
            const stage = graph.stages[stageIndex]
            const job = stage?.jobs[jobIndex]
            if (stage && job) {
              setPluginPicker({ stageId: stage.id, jobId: job.id, stepIdx: stepIndex })
              setConfigTarget(null) // 关闭当前 Drawer，让出选择器
            }
          }
        }}
      />

      {/* 插件选择器（添加 or 重选） */}
      <PluginSelector
        open={!!pluginPicker}
        onClose={() => setPluginPicker(null)}
        onSelect={(plugin) => {
          if (!pluginPicker) return
          if (pluginPicker.stepIdx !== undefined) {
            // 重选
            updateStepPlugin(pluginPicker.stageId, pluginPicker.jobId, pluginPicker.stepIdx, plugin)
          } else {
            addStep(pluginPicker.stageId, pluginPicker.jobId, plugin, pluginPicker.afterIndex)
          }
          setPluginPicker(null)
        }}
      />

      {/* Job 类型选择器 */}
      <JobTypeSelector
        open={!!jobTypePicker}
        onClose={() => setJobTypePicker(null)}
        onSelect={(agent) => {
          if (jobTypePicker) addJob(jobTypePicker, agent)
        }}
      />

      {/* 发布弹窗：先保存编排，再按执行弹窗同一套规则带清单和跳审 */}
      <Modal
        title={t("pipe.saveAndRun")}
        open={releaseModalOpen}
        onOk={() => {
          saveMutation.mutate(undefined, {
            onSuccess: () => releaseMutation.mutate(),
          })
        }}
        onCancel={() => {
          setReleaseModalOpen(false)
          setReleaseBypass(false)
          setReleaseBypassReason('')
        }}
        confirmLoading={saveMutation.isPending || releaseMutation.isPending}
        okText={
          needReleaseApproval ? (releaseBypass ? t('pipe.saveEmergency') : t('pipe.saveSubmit')) : t('pipe.saveAndRun')
        }
        okButtonProps={{
          danger: releaseBypass,
          disabled: (releaseBypass && !releaseBypassReason.trim()) || blockReleaseManifest,
        }}
        width={needReleaseManifest || needReleaseApproval || releaseStartParams.length ? 560 : 520}
      >
        <div style={{ marginBottom: 16 }}>
          {t('pipe.artifactVersion')}
          <Input
            value={releaseVersion}
            onChange={(e) => setReleaseVersion(e.target.value)}
            style={{ marginTop: 8 }}
            placeholder={t("pipe.versionPlaceholder")}
          />
        </div>
        <div style={{ marginBottom: 16, color: '#999', fontSize: 12 }}>
          {t('pipe.commitHint')}
        </div>
        {releaseStartParams.length > 0 && (
          <Form layout="vertical" style={{ marginBottom: needReleaseApproval || needReleaseManifest ? 16 : 0 }}>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={t("pipe.runParams")}
              description={t("pipe.runParamsHint")}
            />
            {releaseStartParams.map((p) => {
              const value = releaseRunParams[p.name] ?? ''
              const setValue = (v: string) => setReleaseRunParams((prev) => ({ ...prev, [p.name]: v }))
              const options = (p.options || []).map((o) => ({ label: o, value: o }))
              const multi = new Set(['checkbox', 'multiple', 'multiselect', 'checkbox-group'])
              return (
                <Form.Item key={p.name} label={p.name} extra={p.description || undefined}>
                  {multi.has((p.type || '').toLowerCase()) && options.length ? (
                    <Select
                      mode="multiple"
                      options={options}
                      value={value ? value.split(',').filter(Boolean) : []}
                      onChange={(vals: string[]) => setValue(vals.join(','))}
                    />
                  ) : options.length ? (
                    <Select options={options} value={value || undefined} onChange={setValue} />
                  ) : (
                    <Input value={value} onChange={(e) => setValue(e.target.value)} placeholder={p.default_value || ''} />
                  )}
                </Form.Item>
              )
            })}
          </Form>
        )}
        <ReleaseGateFields
          needManifest={needReleaseManifest}
          manifest={releaseManifest}
          onManifestChange={setReleaseManifest}
          needApproval={needReleaseApproval}
          allowBypass={canReleaseBypass}
          allowSelfApproval={pipeline?.allow_self_approval}
          bypass={releaseBypass}
          onBypassChange={setReleaseBypass}
          reason={releaseBypassReason}
          onReasonChange={setReleaseBypassReason}
          kind="execute"
        />
      </Modal>
    </div>
  )
}

// ============================================================
// 子组件：StageColumn（蓝盾风格的列表式 Stage 卡片）
// ============================================================
function StageColumn({
  stage,
  stageIndex,
  onOpenConfig,
  onAddJob,
  onAddStep,
  onDeleteStage,
  onDeleteJob,
  onDeleteStep,
  onDuplicateStep,
  onReorderStep,
}: {
  stage: GraphStage
  stageIndex: number
  onOpenConfig: (t: ConfigTarget) => void
  onAddJob: () => void
  onAddStep: (jobId: string) => void
  onDeleteStage: () => void
  onDeleteJob: (jobId: string) => void
  onDeleteStep: (jobId: string, stepIdx: number) => void
  onDuplicateStep: (jobId: string, stepIdx: number) => void
  onReorderStep: (jobId: string, fromIdx: number, toIdx: number) => void
}) {
  const t = useT()
  const totalSteps = useMemo(
    () => stage.jobs.reduce((sum, j) => sum + j.steps.length, 0),
    [stage.jobs]
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 0, flexShrink: 0 }}>
      {/* Stage 标题（蓝色背景）*/}
      <div
        style={{
          background: '#e6f4ff',
          color: '#1677ff',
          padding: '8px 12px',
          borderRadius: 4,
          fontWeight: 600,
          textAlign: 'center',
          minWidth: 280,
          cursor: 'pointer',
          position: 'relative',
        }}
        onClick={() =>
          onOpenConfig({ type: 'stage', stage, index: stageIndex })
        }
      >
        <HolderOutlined style={{ marginRight: 4, color: '#91caff' }} />
        {stage.name}
        <Tag color="cyan" style={{ marginLeft: 8, fontSize: 11 }}>
          {stage.jobs.length} Job · {totalSteps} Task
        </Tag>
      </div>

      {/* Job 并行执行提示 */}
      {stage.jobs.length > 1 && (
        <div
          style={{
            textAlign: 'center',
            fontSize: 11,
            color: '#1677ff',
            marginTop: 4,
          }}
        >
          ⚡ {t('pipe.jobsParallel')}
        </div>
      )}

      {/* Job 卡片列表 */}
      <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
        {stage.jobs.map((job, jIdx) => (
          <JobCard
            key={job.id}
            job={job}
            onClick={() =>
              onOpenConfig({ type: 'job', job, stageIndex, jobIndex: jIdx })
            }
            onAddStep={() => onAddStep(job.id)}
            onDeleteJob={() => onDeleteJob(job.id)}
            onDeleteStep={(sIdx) => onDeleteStep(job.id, sIdx)}
            onDuplicateStep={(sIdx) => onDuplicateStep(job.id, sIdx)}
            onReorderStep={(from, to) => onReorderStep(job.id, from, to)}
            onClickStep={(step, sIdx) =>
              onOpenConfig({
                type: 'step',
                step,
                stageIndex,
                jobIndex: jIdx,
                stepIndex: sIdx,
              })
            }
          />
        ))}
        {/* 添加 Job 按钮 */}
        <Button
          type="dashed"
          block
          icon={<PlusOutlined />}
          onClick={onAddJob}
          style={{ minWidth: 280 }}
        >
          {t('pipe.addParallelJob')}
        </Button>
      </div>

      {/* 删除 Stage 按钮（底部）*/}
      <Button
        type="link"
        danger
        icon={<DeleteOutlined />}
        onClick={onDeleteStage}
        style={{ marginTop: 8, alignSelf: 'center' }}
      >
        {t('pipe.deleteStage')}
      </Button>
    </div>
  )
}

function JobCard({
  job,
  onClick,
  onAddStep,
  onDeleteJob,
  onDeleteStep,
  onDuplicateStep,
  onClickStep,
  onReorderStep,
}: {
  job: GraphJob
  onClick: () => void
  onAddStep: () => void
  onDeleteJob: () => void
  onDeleteStep: (stepIdx: number) => void
  onDuplicateStep: (stepIdx: number) => void
  onClickStep: (step: GraphStep, idx: number) => void
  onReorderStep: (fromIdx: number, toIdx: number) => void
}) {
  const t = useT()
  const [dragIdx, setDragIdx] = useState<number | null>(null)
  const [overIdx, setOverIdx] = useState<number | null>(null)

  return (
    <div
      style={{
        background: '#1f2937',
        color: '#fff',
        borderRadius: 4,
        minWidth: 280,
        overflow: 'hidden',
        boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
      }}
    >
      {/* Job 标题（深色背景）*/}
      <div
        onClick={onClick}
        style={{
          padding: '8px 12px',
          background: '#111827',
          fontSize: 13,
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          cursor: 'pointer',
        }}
      >
        <span>
          <span style={{ color: '#60a5fa' }}>{job.id}</span> {job.name}
        </span>
        <Tag color="cyan" style={{ marginRight: 0, fontSize: 11 }}>
          {job.agent_icon} {t('pipe.nItems', { n: job.steps.length })}
        </Tag>
      </div>

      {/* 步骤列表 */}
      <div style={{ padding: '4px 0', background: '#1f2937' }}>
        {job.steps.length === 0 && (
          <div style={{ color: '#9ca3af', padding: 8, fontSize: 12, textAlign: 'center' }}>
            {t('pipe.noSteps')}
          </div>
        )}
        {job.steps.map((step, idx) => (
          <div
            key={`${job.id}-${idx}`}
            draggable
            onDragStart={(e) => {
              setDragIdx(idx)
              e.dataTransfer.effectAllowed = 'move'
              e.dataTransfer.setData('text/plain', String(idx))
            }}
            onDragOver={(e) => {
              e.preventDefault()
              e.dataTransfer.dropEffect = 'move'
              setOverIdx(idx)
            }}
            onDragLeave={() => setOverIdx(null)}
            onDrop={(e) => {
              e.preventDefault()
              e.stopPropagation()
              const from = dragIdx
              setDragIdx(null)
              setOverIdx(null)
              if (from !== null && from !== idx) {
                onReorderStep(from, idx)
              }
            }}
            onDragEnd={() => {
              setDragIdx(null)
              setOverIdx(null)
            }}
            onClick={() => onClickStep(step, idx)}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '6px 12px',
              fontSize: 12,
              borderTop: idx === 0 ? 'none' : '1px solid #374151',
              borderTopWidth: overIdx === idx && dragIdx !== null ? 2 : undefined,
              borderTopColor: overIdx === idx && dragIdx !== null ? '#1677ff' : undefined,
              cursor: dragIdx !== null ? 'grabbing' : 'pointer',
              color: '#e5e7eb',
              opacity: dragIdx === idx ? 0.5 : 1,
            }}
            onMouseEnter={(e) => {
              if (dragIdx === null) e.currentTarget.style.background = '#374151'
            }}
            onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <HolderOutlined style={{ color: '#6b7280', cursor: 'grab', fontSize: 11 }} />
              <span>{step.icon}</span>
              <span>{step.name || step.display_name || step.plugin}</span>
            </div>
            <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Tooltip title={t("pipe.copyThisStep")}>
                <CopyOutlined
                  onClick={(e) => {
                    e.stopPropagation()
                    onDuplicateStep(idx)
                  }}
                  style={{ fontSize: 11, color: '#9ca3af' }}
                />
              </Tooltip>
              <Tooltip title={t("pipe.deleteThisStep")}>
                <DeleteOutlined
                  onClick={(e) => {
                    e.stopPropagation()
                    onDeleteStep(idx)
                  }}
                  style={{ fontSize: 11, color: '#f87171' }}
                />
              </Tooltip>
            </span>
          </div>
        ))}
        {/* 添加步骤 */}
        <div
          onClick={onAddStep}
          style={{
            textAlign: 'center',
            padding: '6px',
            color: '#60a5fa',
            cursor: 'pointer',
            fontSize: 12,
            borderTop: '1px solid #374151',
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = '#374151')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          <PlusOutlined /> {t('pipe.addStep')}
        </div>
      </div>

      {/* 删除 Job */}
      <div
        onClick={onDeleteJob}
        style={{
          textAlign: 'center',
          padding: 4,
          fontSize: 11,
          color: '#f87171',
          cursor: 'pointer',
          background: '#111827',
        }}
      >
        {t('pipe.deleteJob')}
      </div>
    </div>
  )
}

/**
 * 新建步骤默认用插件显示名，用户可再改成更具体的叫法。
 */
function pluginStepName(plugin: Plugin): string {
  return (plugin.display_name || plugin.name).trim()
}

/**
 * 重选插件时：名称还是旧插件默认名（或空）就换成新插件名；用户改过的保留。
 */
function nextStepNameAfterPluginChange(step: GraphStep, plugin: Plugin): string {
  const previousDefault = (step.display_name || step.plugin).trim()
  const current = (step.name || '').trim()
  if (!current || current === previousDefault) {
    return pluginStepName(plugin)
  }
  return current
}

/**
 * 按插件 schema 预填参数默认值。
 * 表单只是「显示」default，不写进 with，不动这个字段保存后就丢了默认值，
 * 执行时拿到的是空参数 —— 界面上看到的和实际跑的不一致。
 */
function schemaDefaults(plugin: Plugin): Record<string, unknown> {
  if (!plugin.config_schema) return {}
  try {
    const schema = JSON.parse(plugin.config_schema) as {
      fields?: { key: string; type?: string; default?: unknown; children?: unknown[] }[]
    }
    const out: Record<string, unknown> = {}
    for (const f of schema.fields || []) {
      if (f.type === 'group') {
        const children = (f.children || []) as { key: string; default?: unknown }[]
        const sub: Record<string, unknown> = {}
        for (const c of children) if (c.default !== undefined) sub[c.key] = c.default
        if (Object.keys(sub).length > 0) out[f.key] = sub
      } else if (f.default !== undefined) {
        out[f.key] = f.default
      }
    }
    return out
  } catch {
    return {}
  }
}

function getCategoryEmoji(category: string): string {
  const map: Record<string, string> = {
    source: '📦',
    build: '☕',
    deploy: '☸️',
    notify: '💬',
    trigger: '▶️',
    exec: '💻',
    artifact: '🗂️',
    pipeline: '🔁',
  }
  return map[category] || '🔧'
}
