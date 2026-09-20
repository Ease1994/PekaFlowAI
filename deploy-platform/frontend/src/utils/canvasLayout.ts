/**
 * 编辑画布和执行画布共用的节点坐标。
 * 保存进流水线 YAML 的 canvas_layout，两边用同一套 id 取值，避免执行页又摊成一条横排。
 */

export type CanvasLayout = Record<string, { x: number; y: number }>

/** 步骤节点宽，编辑 / 执行卡片保持一致，保存的 x 才对得上 */
export const NODE_W = 220
export const NODE_H = 88
export const GAP_X = 40
export const GAP_Y = 36
export const STAGE_W = 140
/** 阶段卡片估算高度，用来判断旧坐标是否叠在 Job 上 */
export const STAGE_H = 56
export const JOB_W = 120
/** Job 标签估算高度，用来判断是否叠在步骤上 */
export const JOB_H = 52
/** 每个 Job 里步骤一行最多几个，折行后是块状而不是一条长蛇 */
export const STEPS_PER_ROW = 3
export const STAGE_GAP = 72
export const JOB_GAP = 28
export const ORIGIN_X = 40
export const ORIGIN_Y = 40

/** 画布阶段节点 id，与编辑器 `stage-${stage.id}` 相同。 */
export function stageNodeId(stageId: string): string {
  return `stage-${stageId}`
}

/** 画布 Job 节点 id。 */
export function jobNodeId(stageId: string, jobId: string): string {
  return `job-${stageId}-${jobId}`
}

/** 画布步骤节点 id，stepIndex 是 Job 内从 0 起的下标。 */
export function stepNodeId(stageId: string, jobId: string, stepIndex: number): string {
  return `step-${stageId}-${jobId}-${stepIndex}`
}

/** 顺序箭头 id，编辑画布和删线记录共用。 */
export function canvasEdgeId(sourceId: string, targetId: string): string {
  return `e-${sourceId}-${targetId}`
}

type LayoutJob = { id: string; steps: { length: number } }
type LayoutStage = { id: string; jobs: LayoutJob[] }

/**
 * 按阶段分列、Job 上下叠、步骤每行 3 个折行。
 * 没有保存过坐标、或新加了节点时用这套默认摆法。
 */
export function computeCompactLayout(stages: LayoutStage[]): CanvasLayout {
  const layout: CanvasLayout = {}
  let colX = ORIGIN_X

  stages.forEach((stage) => {
    const sid = stageNodeId(stage.id)
    layout[sid] = { x: colX, y: ORIGIN_Y }

    let y = ORIGIN_Y + 72
    let colWidth = Math.max(STAGE_W, JOB_W)

    stage.jobs.forEach((job) => {
      const jid = jobNodeId(stage.id, job.id)
      layout[jid] = { x: colX, y: y + 8 }
      const count = job.steps.length
      const cols = Math.min(count, STEPS_PER_ROW)
      const gridW = cols <= 0 ? 0 : cols * NODE_W + (cols - 1) * GAP_X
      const stepOriginX = colX + JOB_W + GAP_X
      for (let sti = 0; sti < count; sti += 1) {
        const col = sti % STEPS_PER_ROW
        const row = Math.floor(sti / STEPS_PER_ROW)
        layout[stepNodeId(stage.id, job.id, sti)] = {
          x: stepOriginX + col * (NODE_W + GAP_X),
          y: y + row * (NODE_H + GAP_Y),
        }
      }
      if (gridW) colWidth = Math.max(colWidth, JOB_W + GAP_X + gridW)
      const rows = Math.max(1, Math.ceil(count / STEPS_PER_ROW) || 1)
      y += rows * (NODE_H + GAP_Y) + JOB_GAP
    })

    colX += colWidth + STAGE_GAP
  })
  return layout
}

/**
 * 已保存的坐标优先；缺的节点用自动排版补上，避免加步骤后飘到原点。
 */
export function resolveNodePos(saved: CanvasLayout, compact: CanvasLayout, id: string): { x: number; y: number } {
  return saved[id] || compact[id] || { x: ORIGIN_X, y: ORIGIN_Y }
}

/**
 * 两个轴对齐矩形是否相交。用来识别旧版画布把阶段 / Job / 步骤堆在同一块。
 */
function rectsOverlap(
  a: { x: number; y: number; w: number; h: number },
  b: { x: number; y: number; w: number; h: number },
): boolean {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y
}

/**
 * 判断 YAML 里的坐标是不是「步骤嵌在 Job 组里」时存下的相对值。
 * 那种坐标在独立节点排版下会叠到画布左上角，整图应改走自动排版。
 */
export function isGroupRelativeLayout(saved: CanvasLayout, stages: LayoutStage[]): boolean {
  for (const stage of stages) {
    for (const job of stage.jobs) {
      const jobPos = saved[jobNodeId(stage.id, job.id)]
      if (!jobPos) continue
      for (let i = 0; i < job.steps.length; i += 1) {
        const stepPos = saved[stepNodeId(stage.id, job.id, i)]
        if (stepPos && stepPos.x < jobPos.x) return true
      }
    }
  }
  return false
}

/**
 * 判断已存坐标是否把阶段、Job、第一步叠在一起。
 * 旧版分组节点拆开后常出现这种情况，应整图改走自动排版。
 */
export function isOverlappingLayout(saved: CanvasLayout, stages: LayoutStage[]): boolean {
  for (const stage of stages) {
    const stagePos = saved[stageNodeId(stage.id)]
    for (const job of stage.jobs) {
      const jobPos = saved[jobNodeId(stage.id, job.id)]
      if (
        stagePos &&
        jobPos &&
        rectsOverlap(
          { x: stagePos.x, y: stagePos.y, w: STAGE_W, h: STAGE_H },
          { x: jobPos.x, y: jobPos.y, w: JOB_W, h: JOB_H },
        )
      ) {
        return true
      }
      if (!jobPos || job.steps.length === 0) continue
      const stepPos = saved[stepNodeId(stage.id, job.id, 0)]
      if (
        stepPos &&
        rectsOverlap(
          { x: jobPos.x, y: jobPos.y, w: JOB_W, h: JOB_H },
          { x: stepPos.x, y: stepPos.y, w: NODE_W, h: NODE_H },
        )
      ) {
        return true
      }
    }
  }
  return false
}

/**
 * 取画布实际使用的已存坐标。
 * 组相对坐标或卡片互相重叠时整份丢弃，打开编辑器就按自动排版摆。
 */
export function effectiveSavedLayout(saved: CanvasLayout | undefined, stages: LayoutStage[]): CanvasLayout {
  const raw = saved || {}
  if (isGroupRelativeLayout(raw, stages) || isOverlappingLayout(raw, stages)) return {}
  return raw
}

