/**
 * 编排画布拉线 / 删线 / 拆并行的回归：同一 Job 顺序执行，同一阶段 Job 并行。
 * 删相邻步骤的线只断开箭头、不新建 Job；阶段到 Job 的从属线不能删。
 */
import { describe, expect, it, beforeAll } from 'vitest'
import { useI18nStore } from '@/i18n'
import type { GraphJob, GraphStage, GraphStep } from '@/api/types'
import {
  applyCanvasLink,
  applyCanvasReorderStep,
  applyCanvasSplitParallel,
  applyCanvasUnlink,
  canCanvasLink,
  isCanvasGraphErr,
  pruneOpenCuts,
  type CanvasRef,
} from './canvasGraph'
import { canvasEdgeId, stepNodeId } from './canvasLayout'

beforeAll(() => {
  useI18nStore.getState().setLocale('zh-CN')
})

/** 拼一个步骤，只填测试要用的字段。 */
function step(plugin: string, order: number): GraphStep {
  return {
    plugin,
    display_name: plugin,
    icon: plugin,
    order,
    built_in: true,
  }
}

function job(id: string, name: string, agent: string, plugins: string[]): GraphJob {
  return {
    id,
    name,
    agent,
    agent_icon: agent,
    steps: plugins.map((p, i) => step(p, i + 1)),
  }
}

/**
 * 用户流水线常见错序：Git、发送文件、提取增量挤在一个 Job 里，
 * 提取增量被放在发送文件后面，构建产物发不出去。
 */
function messyBuildStage(): GraphStage[] {
  return [
    {
      id: 's1',
      name: '构建',
      order: 1,
      jobs: [job('1-1', '构建机', 'builder', ['git-checkout', 'file-transfer', 'pack-incremental'])],
    },
  ]
}

/** 两个并行 Job：构建机顺序链 + 节点发送文件。 */
function twoJobs(): GraphStage[] {
  return [
    {
      id: 's1',
      name: '构建',
      order: 1,
      jobs: [
        job('1-1', '构建机', 'builder', ['git-checkout', 'pack-incremental']),
        job('1-2', '节点', 'node', ['file-transfer']),
      ],
    },
  ]
}

function orderedChain(): GraphStage[] {
  return [
    {
      id: 's1',
      name: '构建',
      order: 1,
      jobs: [job('1-1', '构建机', 'builder', ['git-checkout', 'pack-incremental', 'file-transfer'])],
    },
  ]
}

function jobPlugins(stages: GraphStage[]): string[][] {
  return stages[0].jobs.map((j) => j.steps.map((s) => s.plugin))
}

function mustLink(stages: GraphStage[], source: CanvasRef, target: CanvasRef) {
  const result = applyCanvasLink(stages, source, target)
  if (isCanvasGraphErr(result)) throw new Error(result.error)
  return result.stages
}

function mustSplit(stages: GraphStage[], step: CanvasRef) {
  const result = applyCanvasSplitParallel(stages, step)
  if (isCanvasGraphErr(result)) throw new Error(result.error)
  return result.stages
}

describe('canCanvasLink', () => {
  it('步骤可以接到步骤或 Job，阶段只能接到 Job，不能接到自己', () => {
    const gitStep: CanvasRef = { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 }
    const pack: CanvasRef = { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 }
    const jobRef: CanvasRef = { kind: 'job', stageId: 's1', jobId: '1-1' }
    const stage: CanvasRef = { kind: 'stage', stageId: 's1' }
    expect(canCanvasLink(gitStep, pack)).toBe(true)
    expect(canCanvasLink(gitStep, jobRef)).toBe(true)
    expect(canCanvasLink(jobRef, gitStep)).toBe(true)
    expect(canCanvasLink(stage, jobRef)).toBe(true)
    expect(canCanvasLink(stage, gitStep)).toBe(false)
    expect(canCanvasLink(gitStep, gitStep)).toBe(false)
  })
})

describe('applyCanvasLink 改顺序', () => {
  it('从 Git 拉到提取增量：提取增量接到 Git 后面，发送文件排到最后', () => {
    const stages = mustLink(
      messyBuildStage(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 },
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 2 },
    )
    expect(jobPlugins(stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
  })

  it('不能接到自己', () => {
    const gitStep: CanvasRef = { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 }
    const r = applyCanvasLink(messyBuildStage(), gitStep, gitStep)
    expect(isCanvasGraphErr(r)).toBe(true)
    if (isCanvasGraphErr(r)) expect(r.error).toContain('自己')
  })

  it('已经接在后面再拉一次：接上断开的线，不改结构', () => {
    const r = applyCanvasLink(
      orderedChain(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 },
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 },
    )
    if (isCanvasGraphErr(r)) throw new Error(r.error)
    expect(r.hint).toContain('已接上')
    expect(jobPlugins(r.stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
  })

  it('同一 Job 里把后面的步骤拉到中间：一次就接上，旧断开记录不能挡住新箭头', () => {
    const stages: GraphStage[] = [
      {
        id: 's1',
        name: '构建',
        order: 1,
        jobs: [job('1-1', '构建机', 'builder', ['git-checkout', 'maven-build', 'docker-deploy', 'docker-build'])],
      },
    ]
    const maven: CanvasRef = { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 }
    const image: CanvasRef = { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 3 }
    const r = applyCanvasLink(stages, maven, image)
    if (isCanvasGraphErr(r)) throw new Error(r.error)
    expect(jobPlugins(r.stages)).toEqual([['git-checkout', 'maven-build', 'docker-build', 'docker-deploy']])
    expect(r.hint).toBe('')
    const expectedEdge = canvasEdgeId(stepNodeId('s1', '1-1', 1), stepNodeId('s1', '1-1', 2))
    expect(r.linked_edge_id).toBe(expectedEdge)
    const staleCut = canvasEdgeId(stepNodeId('s1', '1-1', 1), stepNodeId('s1', '1-1', 2))
    const leftover = pruneOpenCuts(r.stages, [staleCut].filter((id) => id !== r.linked_edge_id))
    expect(leftover).toEqual([])
  })

  it('把发送文件并入构建机 Job 末尾', () => {
    const stages = mustLink(
      twoJobs(),
      { kind: 'step', stageId: 's1', jobId: '1-2', stepIndex: 0 },
      { kind: 'job', stageId: 's1', jobId: '1-1' },
    )
    expect(jobPlugins(stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
  })

  it('从提取增量拉到发送文件：跨 Job 把发送文件接到提取增量后面', () => {
    const stages = mustLink(
      twoJobs(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 },
      { kind: 'step', stageId: 's1', jobId: '1-2', stepIndex: 0 },
    )
    expect(jobPlugins(stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
  })

  it('合并同一阶段两个 Job 变成一条顺序链', () => {
    const stages = mustLink(
      twoJobs(),
      { kind: 'job', stageId: 's1', jobId: '1-1' },
      { kind: 'job', stageId: 's1', jobId: '1-2' },
    )
    expect(jobPlugins(stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
  })

  it('从空阶段拉到另一个阶段的 Job：Job 换所属阶段', () => {
    const stages = mustLink(
      [
        {
          id: 's1',
          name: '构建',
          order: 1,
          jobs: [job('1-1', '构建机', 'builder', ['git-checkout'])],
        },
        { id: 's2', name: '部署', order: 2, jobs: [] },
      ],
      { kind: 'stage', stageId: 's2' },
      { kind: 'job', stageId: 's1', jobId: '1-1' },
    )
    expect(stages[0].jobs).toHaveLength(0)
    expect(stages[1].jobs.map((j) => j.steps.map((s) => s.plugin))).toEqual([['git-checkout']])
  })
})

describe('applyCanvasUnlink 删相邻步骤的线', () => {
  it('只断开箭头，不新建 Job，步骤仍在原 Job', () => {
    const r = applyCanvasUnlink(
      orderedChain(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 },
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 2 },
    )
    if (isCanvasGraphErr(r)) throw new Error(r.error)
    expect(jobPlugins(r.stages)).toEqual([['git-checkout', 'pack-incremental', 'file-transfer']])
    expect(r.cut_edge_id).toBeTruthy()
  })

  it('删线不会把后续步骤拆到新的构建环境', () => {
    const stages: GraphStage[] = [
      {
        id: 's1',
        name: '构建',
        order: 1,
        jobs: [job('1-1', '构建机', 'builder', ['git-checkout', 'maven-build', 'docker-deploy', 'docker-build'])],
      },
    ]
    const r = applyCanvasUnlink(
      stages,
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 },
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 2 },
    )
    if (isCanvasGraphErr(r)) throw new Error(r.error)
    expect(jobPlugins(r.stages)).toEqual([['git-checkout', 'maven-build', 'docker-deploy', 'docker-build']])
    expect(r.stages[0].jobs).toHaveLength(1)
  })

  it('Job 到第一步的从属线不能删', () => {
    const r = applyCanvasUnlink(
      orderedChain(),
      { kind: 'job', stageId: 's1', jobId: '1-1' },
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 },
    )
    expect(isCanvasGraphErr(r)).toBe(true)
  })

  it('阶段到 Job 的从属线不能删', () => {
    const r = applyCanvasUnlink(
      orderedChain(),
      { kind: 'stage', stageId: 's1' },
      { kind: 'job', stageId: 's1', jobId: '1-1' },
    )
    expect(isCanvasGraphErr(r)).toBe(true)
  })
})

describe('applyCanvasSplitParallel 显式拆并行', () => {
  it('把发送文件拆成同阶段并行 Job', () => {
    const stages = mustSplit(orderedChain(), {
      kind: 'step',
      stageId: 's1',
      jobId: '1-1',
      stepIndex: 2,
    })
    expect(jobPlugins(stages)).toEqual([['git-checkout', 'pack-incremental'], ['file-transfer']])
  })

  it('把 Job 里第一步拆出去，剩下的步骤留在原 Job', () => {
    const stages = mustSplit(
      [
        {
          id: 's1',
          name: '构建',
          order: 1,
          jobs: [job('1-1', '构建机', 'builder', ['git-checkout', 'pack-incremental'])],
        },
      ],
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 },
    )
    expect(jobPlugins(stages)).toEqual([['pack-incremental'], ['git-checkout']])
  })
})

describe('applyCanvasReorderStep', () => {
  it('后移一步：提取增量排到发送文件后面', () => {
    const r = applyCanvasReorderStep(
      orderedChain(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 1 },
      2,
    )
    if (isCanvasGraphErr(r)) throw new Error(r.error)
    expect(jobPlugins(r.stages)).toEqual([['git-checkout', 'file-transfer', 'pack-incremental']])
  })

  it('已经在队头再前移会提示', () => {
    const r = applyCanvasReorderStep(
      orderedChain(),
      { kind: 'step', stageId: 's1', jobId: '1-1', stepIndex: 0 },
      -1,
    )
    expect(isCanvasGraphErr(r)).toBe(true)
  })
})
