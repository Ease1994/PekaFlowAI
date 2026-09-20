/**
 * 阶段分列、Job 独立节点在步骤左侧，步骤每行最多 3 个折行。
 */
import { describe, expect, it } from 'vitest'
import {
  computeCompactLayout,
  effectiveSavedLayout,
  isGroupRelativeLayout,
  isOverlappingLayout,
  jobNodeId,
  stageNodeId,
  stepNodeId,
  JOB_W,
  NODE_H,
  NODE_W,
  STEPS_PER_ROW,
} from './canvasLayout'

describe('computeCompactLayout', () => {
  it('第四步折到第二行，和第一列对齐', () => {
    const layout = computeCompactLayout([
      { id: 's1', jobs: [{ id: '1-1', steps: [{}, {}, {}, {}] }] },
    ])
    const a = layout[stepNodeId('s1', '1-1', 0)]
    const d = layout[stepNodeId('s1', '1-1', 3)]
    expect(STEPS_PER_ROW).toBe(3)
    expect(d.x).toBe(a.x)
    expect(d.y).toBeGreaterThan(a.y)
  })

  it('并行 Job 上下叠，步骤在 Job 标签右侧', () => {
    const layout = computeCompactLayout([
      {
        id: 's1',
        jobs: [
          { id: '1-1', steps: [{}, {}, {}] },
          { id: '1-2', steps: [{}] },
        ],
      },
    ])
    const job1 = layout[jobNodeId('s1', '1-1')]
    const job2 = layout[jobNodeId('s1', '1-2')]
    const step0 = layout[stepNodeId('s1', '1-1', 0)]
    expect(job2.x).toBe(job1.x)
    expect(job2.y).toBeGreaterThan(job1.y)
    expect(step0.x).toBeGreaterThan(job1.x + JOB_W / 2)
    expect(step0.y + NODE_H).toBeGreaterThan(job1.y)
    expect(NODE_W).toBeGreaterThan(0)
  })

  it('步骤落在 Job 左侧时视为组相对坐标，改用自动排版', () => {
    const stages = [{ id: 's1', jobs: [{ id: '1-1', steps: [{}, {}] }] }]
    const saved = {
      [jobNodeId('s1', '1-1')]: { x: 40, y: 112 },
      [stepNodeId('s1', '1-1', 0)]: { x: 16, y: 56 },
      [stepNodeId('s1', '1-1', 1)]: { x: 276, y: 56 },
    }
    expect(isGroupRelativeLayout(saved, stages)).toBe(true)
    expect(effectiveSavedLayout(saved, stages)).toEqual({})
  })

  it('阶段和 Job 叠在一起时改用自动排版', () => {
    const stages = [{ id: 's1', jobs: [{ id: '1-1', steps: [{}] }] }]
    const saved = {
      [stageNodeId('s1')]: { x: 40, y: 40 },
      [jobNodeId('s1', '1-1')]: { x: 48, y: 52 },
      [stepNodeId('s1', '1-1', 0)]: { x: 200, y: 112 },
    }
    expect(isOverlappingLayout(saved, stages)).toBe(true)
    expect(effectiveSavedLayout(saved, stages)).toEqual({})
  })

  it('Job 和第一步叠在一起时改用自动排版', () => {
    const stages = [{ id: 's1', jobs: [{ id: '1-1', steps: [{}] }] }]
    const saved = {
      [stageNodeId('s1')]: { x: 40, y: 40 },
      [jobNodeId('s1', '1-1')]: { x: 40, y: 120 },
      [stepNodeId('s1', '1-1', 0)]: { x: 80, y: 128 },
    }
    expect(isOverlappingLayout(saved, stages)).toBe(true)
    expect(effectiveSavedLayout(saved, stages)).toEqual({})
  })

  it('自动排版坐标本身不算重叠，保留用户拖过的合法坐标', () => {
    const stages = [
      {
        id: 's1',
        jobs: [
          { id: '1-1', steps: [{}, {}, {}] },
          { id: '1-2', steps: [{}] },
        ],
      },
    ]
    const compact = computeCompactLayout(stages)
    expect(isOverlappingLayout(compact, stages)).toBe(false)
    expect(effectiveSavedLayout(compact, stages)).toEqual(compact)
  })
})
