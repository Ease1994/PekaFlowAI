/**
 * 编排画布上拉线 / 删线 / 拆并行，映射到 Stage → Job → Step 结构。
 *
 * 同一 Job 里的步骤顺序执行，同一阶段里的 Job 并行。
 * 拉线 = 把目标步骤接到源后面；
 * 删线 = 只拿掉箭头，不改 Job / 步骤归属（对齐 Dify）；未接上则保存失败；
 * 右键「拆成并行 Job」才真正新建构建环境。
 */
import type { GraphJob, GraphStage, GraphStep } from '@/api/types'
import { t } from '@/i18n'
import { canvasEdgeId, jobNodeId, stageNodeId, stepNodeId } from '@/utils/canvasLayout'

export type CanvasRef =
  | { kind: 'stage'; stageId: string }
  | { kind: 'job'; stageId: string; jobId: string }
  | { kind: 'step'; stageId: string; jobId: string; stepIndex: number }

export type CanvasGraphOk = {
  stages: GraphStage[]
  hint: string
  /** 删线时记下来的箭头 id，画布不再画这条线，也不新建 Job */
  cut_edge_id?: string
  /**
   * 拉线成功后应显示的那条顺序箭头。
   * 步骤下标会变，必须用改完之后的 id 清掉断开记录，否则第一次只改了顺序、箭头仍被旧记录挡住。
   */
  linked_edge_id?: string
}
export type CanvasGraphErr = { error: string }
export type CanvasGraphResult = CanvasGraphOk | CanvasGraphErr

export function isCanvasGraphErr(r: CanvasGraphResult): r is CanvasGraphErr {
  return 'error' in r
}

/** 有断开的步骤连线时不允许保存，避免 YAML 里仍是顺序执行却和画布不一致。 */
export const OPEN_CUTS_SAVE_ERROR = () => t('pipe.openCutsSave')

/** 画布节点 id，和 EditorCanvas 里的节点 id 一致。 */
export function canvasNodeId(ref: CanvasRef): string {
  if (ref.kind === 'stage') return stageNodeId(ref.stageId)
  if (ref.kind === 'job') return jobNodeId(ref.stageId, ref.jobId)
  return stepNodeId(ref.stageId, ref.jobId, ref.stepIndex)
}

/** 只保留「当前编排里确实还存在」的断开记录，步骤调序后旧 id 作废。 */
export function pruneOpenCuts(stages: GraphStage[], cuts: string[] | undefined): string[] {
  if (!cuts?.length) return []
  const valid = sequentialEdgeIds(stages)
  return cuts.filter((id) => valid.has(id))
}

function sequentialEdgeIds(stages: GraphStage[]): Set<string> {
  const ids = new Set<string>()
  for (const stage of stages) {
    for (const job of stage.jobs) {
      let prev = jobNodeId(stage.id, job.id)
      job.steps.forEach((_, sti) => {
        const id = stepNodeId(stage.id, job.id, sti)
        ids.add(canvasEdgeId(prev, id))
        prev = id
      })
    }
  }
  return ids
}

/** 从断开记录里去掉刚接上的那条箭头。 */
export function dropCut(cuts: string[] | undefined, source: CanvasRef, target: CanvasRef): string[] {
  const id = canvasEdgeId(canvasNodeId(source), canvasNodeId(target))
  return (cuts || []).filter((c) => c !== id)
}

/**
 * 画布上哪些圆点之间允许拉线。
 * 步骤可接到步骤/Job；Job 可接到步骤/Job；阶段只能接到 Job（换所属阶段）。
 */
export function canCanvasLink(source?: CanvasRef, target?: CanvasRef): boolean {
  if (!source || !target) return false
  if (sameRef(source, target)) return false
  if (source.kind === 'step' && (target.kind === 'step' || target.kind === 'job')) return true
  if (source.kind === 'job' && (target.kind === 'step' || target.kind === 'job')) return true
  if (source.kind === 'stage' && target.kind === 'job') return true
  return false
}

/** 深拷贝编排树，后续 splice 不改调用方手里的对象。 */
function cloneStages(stages: GraphStage[]): GraphStage[] {
  return stages.map((s) => ({
    ...s,
    jobs: s.jobs.map((j) => ({
      ...j,
      steps: j.steps.map((st) => ({ ...st })),
    })),
  }))
}

function stageOf(stages: GraphStage[], stageId: string): GraphStage | undefined {
  return stages.find((s) => s.id === stageId)
}

function jobOf(stage: GraphStage, jobId: string): GraphJob | undefined {
  return stage.jobs.find((j) => j.id === jobId)
}

function reindex(job: GraphJob): void {
  job.steps = job.steps.map((st, i) => ({ ...st, order: i + 1 }))
}

function dropEmptyJobs(stage: GraphStage): void {
  stage.jobs = stage.jobs.filter((j) => j.steps.length > 0)
}

function nextJobId(stage: GraphStage): string {
  const used = new Set(stage.jobs.map((j) => j.id))
  let n = 1
  while (used.has(`${stage.order}-${n}`)) n += 1
  return `${stage.order}-${n}`
}

function takeStep(
  stages: GraphStage[],
  stageId: string,
  jobId: string,
  stepIndex: number,
): GraphStep | null {
  const stage = stageOf(stages, stageId)
  const job = stage ? jobOf(stage, jobId) : undefined
  if (!job || stepIndex < 0 || stepIndex >= job.steps.length) return null
  const [step] = job.steps.splice(stepIndex, 1)
  reindex(job)
  return step
}

function insertStep(job: GraphJob, index: number, step: GraphStep): void {
  const at = Math.max(0, Math.min(index, job.steps.length))
  job.steps.splice(at, 0, step)
  reindex(job)
}

function stepLabel(step: GraphStep): string {
  return (step.name || step.display_name || step.plugin || 'step').trim()
}

/**
 * 从源拉到目标：目标立刻接到源后面，这一次就把箭头画上。
 * 步骤接到步骤 = 改顺序（可跨 Job / 阶段）；接到 Job = 并入该 Job。
 */
export function applyCanvasLink(
  stages: GraphStage[],
  source: CanvasRef,
  target: CanvasRef,
): CanvasGraphResult {
  if (sameRef(source, target)) {
    return { error: t('pipe.cannotLinkSelf') }
  }
  const next = cloneStages(stages)

  if (source.kind === 'step' && target.kind === 'step') {
    return linkStepAfterStep(next, source, target)
  }
  if (source.kind === 'job' && target.kind === 'step') {
    return moveStepToJob(next, target, source, 0, t('pipe.linkedJobHead'))
  }
  if (source.kind === 'step' && target.kind === 'job') {
    const jobStage = stageOf(next, target.stageId)
    const job = jobStage ? jobOf(jobStage, target.jobId) : undefined
    if (!job) return { error: t('pipe.destJobMissing') }
    return moveStepToJob(next, source, target, job.steps.length, t('pipe.mergedJobTail'))
  }
  if (source.kind === 'stage' && target.kind === 'job') {
    return moveJobToStage(next, target, source.stageId)
  }
  if (source.kind === 'job' && target.kind === 'job') {
    return mergeJobs(next, source, target)
  }
  return { error: t('pipe.linkUnsupported') }
}

/**
 * 删相邻步骤之间的箭头：只记一条断开，不改 Job / 步骤，也不新建构建环境。
 * 阶段→Job、Job→第一步是从属关系，不能删。
 */
export function applyCanvasUnlink(
  stages: GraphStage[],
  source: CanvasRef,
  target: CanvasRef,
): CanvasGraphResult {
  if (source.kind !== 'step' || target.kind !== 'step') {
    return { error: t('pipe.cannotUnlinkBelong') }
  }
  if (
    source.stageId !== target.stageId ||
    source.jobId !== target.jobId ||
    target.stepIndex !== source.stepIndex + 1
  ) {
    return { error: t('pipe.unlinkAdjacentOnly') }
  }
  return {
    stages,
    hint: t('pipe.unlinkedHint'),
    cut_edge_id: canvasEdgeId(canvasNodeId(source), canvasNodeId(target)),
  }
}

/**
 * 把指定步骤（及其后在本函数里只移这一格）拆到同阶段的新 Job，与原 Job 并行。
 * 这是显式操作，不再绑在 Delete 箭头上。
 */
export function applyCanvasSplitParallel(
  stages: GraphStage[],
  stepRef: CanvasRef,
): CanvasGraphResult {
  if (stepRef.kind !== 'step') {
    return { error: t('pipe.splitStepOnly') }
  }
  return splitStepToParallelJob(cloneStages(stages), stepRef)
}

/**
 * 同一 Job 内调整步骤顺序（画布右键「前移 / 后移」、列表拖拽共用）。
 */
export function applyCanvasReorderStep(
  stages: GraphStage[],
  stepRef: CanvasRef,
  toIndex: number,
): CanvasGraphResult {
  if (stepRef.kind !== 'step') {
    return { error: t('pipe.reorderStepOnly') }
  }
  const next = cloneStages(stages)
  const stage = stageOf(next, stepRef.stageId)
  const job = stage ? jobOf(stage, stepRef.jobId) : undefined
  if (!job) return { error: t('pipe.jobMissing') }
  if (toIndex < 0 || toIndex >= job.steps.length) {
    return { error: t('pipe.atHead') }
  }
  if (toIndex === stepRef.stepIndex) {
    return { error: t('pipe.alreadyThere') }
  }
  const moved = takeStep(next, stepRef.stageId, stepRef.jobId, stepRef.stepIndex)
  if (!moved) return { error: t('pipe.stepMissing') }
  insertStep(job, toIndex, moved)
  return { stages: next, hint: t('pipe.reordered', { name: stepLabel(moved) }) }
}

function sameRef(a: CanvasRef, b: CanvasRef): boolean {
  if (a.kind !== b.kind) return false
  if (a.kind === 'stage' && b.kind === 'stage') return a.stageId === b.stageId
  if (a.kind === 'job' && b.kind === 'job') return a.stageId === b.stageId && a.jobId === b.jobId
  if (a.kind === 'step' && b.kind === 'step') {
    return a.stageId === b.stageId && a.jobId === b.jobId && a.stepIndex === b.stepIndex
  }
  return false
}

function linkStepAfterStep(
  stages: GraphStage[],
  source: Extract<CanvasRef, { kind: 'step' }>,
  target: Extract<CanvasRef, { kind: 'step' }>,
): CanvasGraphResult {
  if (
    source.stageId === target.stageId &&
    source.jobId === target.jobId &&
    target.stepIndex === source.stepIndex + 1
  ) {
    return {
      stages,
      hint: t('pipe.alreadyLinked'),
      linked_edge_id: canvasEdgeId(canvasNodeId(source), canvasNodeId(target)),
    }
  }
  const moved = takeStep(stages, target.stageId, target.jobId, target.stepIndex)
  if (!moved) return { error: t('pipe.destStepMissing') }

  const srcStage = stageOf(stages, source.stageId)
  const srcJob = srcStage ? jobOf(srcStage, source.jobId) : undefined
  if (!srcJob) return { error: t('pipe.srcJobMissing') }

  let insertAt = source.stepIndex + 1
  if (source.stageId === target.stageId && source.jobId === target.jobId && target.stepIndex < source.stepIndex) {
    insertAt = source.stepIndex
  }
  insertStep(srcJob, insertAt, moved)
  for (const s of stages) dropEmptyJobs(s)

  // 改完顺序后源就在 insertAt-1，目标在 insertAt。断开记录按节点 id 存，必须用这对新下标去清。
  const fromIndex = Math.max(0, insertAt - 1)
  const linked_edge_id = canvasEdgeId(
    stepNodeId(source.stageId, source.jobId, fromIndex),
    stepNodeId(source.stageId, source.jobId, insertAt),
  )
  return { stages, hint: '', linked_edge_id }
}

function moveStepToJob(
  stages: GraphStage[],
  stepRef: Extract<CanvasRef, { kind: 'step' }>,
  jobRef: Extract<CanvasRef, { kind: 'job' }>,
  insertIndex: number,
  hint: string,
): CanvasGraphResult {
  const destStage = stageOf(stages, jobRef.stageId)
  const destJob = destStage ? jobOf(destStage, jobRef.jobId) : undefined
  if (!destJob) return { error: t('pipe.destJobMissing') }

  let at = insertIndex
  if (stepRef.stageId === jobRef.stageId && stepRef.jobId === jobRef.jobId) {
    if (stepRef.stepIndex < insertIndex) at = insertIndex - 1
    if (at === stepRef.stepIndex) return { error: t('pipe.alreadyThere') }
  }
  const moved = takeStep(stages, stepRef.stageId, stepRef.jobId, stepRef.stepIndex)
  if (!moved) return { error: t('pipe.stepMissing') }
  const job = jobOf(stageOf(stages, jobRef.stageId)!, jobRef.jobId)
  if (!job) return { error: t('pipe.destJobMissing') }
  insertStep(job, at, moved)
  for (const s of stages) dropEmptyJobs(s)
  return { stages, hint }
}

function moveJobToStage(
  stages: GraphStage[],
  jobRef: Extract<CanvasRef, { kind: 'job' }>,
  stageId: string,
): CanvasGraphResult {
  if (jobRef.stageId === stageId) {
    return { error: t('pipe.jobAlreadyInStage') }
  }
  const from = stageOf(stages, jobRef.stageId)
  const to = stageOf(stages, stageId)
  if (!from || !to) return { error: t('pipe.stageMissing') }
  const idx = from.jobs.findIndex((j) => j.id === jobRef.jobId)
  if (idx < 0) return { error: t('pipe.jobMissing') }
  const [job] = from.jobs.splice(idx, 1)
  let id = job.id
  if (to.jobs.some((j) => j.id === id)) id = nextJobId(to)
  to.jobs.push({ ...job, id })
  return { stages, hint: t('pipe.jobMoved', { name: to.name }) }
}

function mergeJobs(
  stages: GraphStage[],
  source: Extract<CanvasRef, { kind: 'job' }>,
  target: Extract<CanvasRef, { kind: 'job' }>,
): CanvasGraphResult {
  if (source.stageId !== target.stageId) {
    return { error: t('pipe.mergeSameStage') }
  }
  const stage = stageOf(stages, source.stageId)
  const a = stage ? jobOf(stage, source.jobId) : undefined
  const b = stage ? jobOf(stage, target.jobId) : undefined
  if (!a || !b) return { error: t('pipe.jobMissing') }
  a.steps.push(...b.steps)
  reindex(a)
  stage!.jobs = stage!.jobs.filter((j) => j.id !== b.id)
  return { stages, hint: t('pipe.jobsMerged') }
}

function splitStepToParallelJob(
  stages: GraphStage[],
  stepRef: Extract<CanvasRef, { kind: 'step' }>,
): CanvasGraphResult {
  const stage = stageOf(stages, stepRef.stageId)
  const job = stage ? jobOf(stage, stepRef.jobId) : undefined
  if (!job) return { error: t('pipe.jobMissing') }
  const moved = takeStep(stages, stepRef.stageId, stepRef.jobId, stepRef.stepIndex)
  if (!moved) return { error: t('pipe.stepMissing') }
  const fresh = newJobLike(stage!, job)
  insertStep(fresh, 0, moved)
  stage!.jobs.push(fresh)
  dropEmptyJobs(stage!)
  return {
    stages,
    hint: t('pipe.splitDone', { name: stepLabel(moved) }),
  }
}

/** 新 Job 沿用原 Job 的名称和构建环境，只换编号。 */
function newJobLike(stage: GraphStage, job: GraphJob): GraphJob {
  return {
    id: nextJobId(stage),
    name: job.name,
    agent: job.agent,
    agent_icon: job.agent_icon,
    steps: [],
  }
}
