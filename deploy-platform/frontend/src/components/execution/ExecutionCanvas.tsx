import { useEffect, useMemo, useRef } from 'react'
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
} from 'reactflow'
import 'reactflow/dist/style.css'
import ExecStepNode, { type CanvasStepPick, type ExecStepData } from './ExecStepNode'
import ExecStageNode from './ExecStageNode'
import ExecJobNode from './ExecJobNode'
import {
  computeCompactLayout,
  effectiveSavedLayout,
  jobNodeId,
  resolveNodePos,
  stageNodeId,
  stepNodeId,
  type CanvasLayout,
} from '@/utils/canvasLayout'
import { allNodesMeasured, canvasWidthChanged } from '@/utils/canvasViewport'

const nodeTypes = { execStep: ExecStepNode, execStage: ExecStageNode, execJob: ExecJobNode }

/** 执行序列里画布需要的最小结构。 */
export type CanvasSequence = {
  canvas_layout?: CanvasLayout
  stages: Array<{
    id?: string
    name: string
    status: string
    jobs: Array<{
      task_id: number | null
      id: string
      name: string
      agent: string
      status: string
      steps: Array<{
        order: number
        task_id: number | null
        log_index?: number
        name: string
        plugin: string
        status: string
        duration_label: string | null
      }>
    }>
  }>
}

/**
 * 执行画布节点：坐标与编辑页同一套 id（stage- / job- / step-）。
 * 有保存过的 layout 就用，没有则用自动折行排版，不再摊成一条横排。
 */
export function buildExecutionGraph(
  sequence: CanvasSequence,
  selected: CanvasStepPick | null,
  onSelect: (pick: CanvasStepPick) => void,
): { nodes: Node[]; edges: Edge[] } {
  const stages = sequence.stages.map((stage, si) => ({
    ...stage,
    id: stage.id || `stage-${si + 1}`,
  }))
  const compact = computeCompactLayout(stages)
  const saved = effectiveSavedLayout(sequence.canvas_layout, stages)
  const pos = (id: string) => resolveNodePos(saved, compact, id)

  const nodes: Node[] = []
  const edges: Edge[] = []

  stages.forEach((stage) => {
    const sid = stageNodeId(stage.id)
    nodes.push({
      id: sid,
      type: 'execStage',
      position: pos(sid),
      data: { name: stage.name, status: stage.status },
      draggable: false,
      selectable: false,
    })

    stage.jobs.forEach((job, ji) => {
      const jobId = job.id || `job-${ji}`
      const jid = jobNodeId(stage.id, jobId)
      nodes.push({
        id: jid,
        type: 'execJob',
        position: pos(jid),
        data: { id: jobId, name: job.name, agent: job.agent, status: job.status },
        draggable: false,
        selectable: false,
      })
      if (ji === 0) edges.push(makeEdge(sid, jid, false))

      let prevId = jid
      job.steps.forEach((step, sti) => {
        const id = stepNodeId(stage.id, jobId, sti)
        const pick: CanvasStepPick = {
          taskId: step.task_id ?? job.task_id,
          stepOrder: step.order,
          logIndex: step.log_index ?? step.order,
        }
        const selectedNow =
          !!selected && selected.taskId === pick.taskId && selected.stepOrder === pick.stepOrder
        const data: ExecStepData = {
          order: step.order,
          title: (step.name || '').trim() || step.plugin,
          plugin: step.plugin,
          status: step.status,
          duration: step.duration_label,
          selected: selectedNow,
          pick,
          onSelect,
        }
        nodes.push({
          id,
          type: 'execStep',
          position: pos(id),
          data,
          draggable: false,
        })
        edges.push(makeEdge(prevId, id, step.status === 'running' || step.status === 'failed'))
        prevId = id
      })
    })
  })

  return { nodes, edges }
}

/** 步骤之间的贝塞尔连线；失败/执行中加粗并动画。 */
function makeEdge(source: string, target: string, hot: boolean): Edge {
  return {
    id: `e-${source}-${target}`,
    source,
    target,
    type: 'smoothstep',
    animated: hot,
    style: { stroke: hot ? '#ef4444' : '#cbd5e1', strokeWidth: hot ? 2.4 : 1.6 },
  }
}

/**
 * 执行画布的 React Flow 实例。
 * 受控模式必须把测量结果经 onNodesChange 写回，fitView 才看得到全部节点。
 * 执行中途只在画布宽度明显变化时重新 fit（开关日志）；高度变化不能动镜头。
 */
function CanvasBoard({
  incomingNodes,
  incomingEdges,
  onSelect,
  onDeselect,
}: {
  incomingNodes: Node[]
  incomingEdges: Edge[]
  onSelect: (pick: CanvasStepPick) => void
  onDeselect: () => void
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const { fitView, getNodes } = useReactFlow()
  const [nodes, setNodes, onNodesChange] = useNodesState(incomingNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(incomingEdges)
  /** 节点 id 集合。步骤状态刷新不改 id，不能拿来反复 fitView。 */
  const fingerprint = incomingNodes.map((n) => n.id).join('|')
  const ignorePane = useRef(false)
  /** 上次参与 fit 的画布宽度。只拿宽度判断日志栏开关，忽略顶栏高度抖动。 */
  const lastWidth = useRef(0)

  useEffect(() => {
    setNodes((current) => {
      const byId = new Map(current.map((n) => [n.id, n]))
      return incomingNodes.map((n) => {
        const prev = byId.get(n.id)
        if (!prev) return n
        return { ...n, width: prev.width, height: prev.height, position: n.position }
      })
    })
    setEdges(incomingEdges)
  }, [incomingNodes, incomingEdges, setNodes, setEdges])

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    let timer = 0
    let tries = 0

    const fitIfComplete = () => {
      window.clearTimeout(timer)
      const current = getNodes()
      if (!allNodesMeasured(current)) {
        if (tries < 20) {
          tries += 1
          timer = window.setTimeout(fitIfComplete, 50)
        }
        return
      }
      if (el.clientWidth < 16 || el.clientHeight < 8) return
      lastWidth.current = el.clientWidth
      fitView({ padding: 0.18, duration: 200, includeHiddenNodes: true })
    }

    const onResize = () => {
      if (!canvasWidthChanged(lastWidth.current, el.clientWidth)) return
      tries = 0
      fitIfComplete()
    }

    timer = window.setTimeout(fitIfComplete, 50)
    const ro = new ResizeObserver(onResize)
    ro.observe(el)
    return () => {
      window.clearTimeout(timer)
      ro.disconnect()
    }
  }, [fingerprint, fitView, getNodes])

  return (
    <div ref={wrapRef} style={{ width: '100%', height: '100%' }}>
      <ReactFlow
        className="rp-flow"
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodesDraggable={false}
        nodesConnectable={false}
        panOnDrag
        zoomOnScroll
        minZoom={0.2}
        maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_e, node) => {
          if (node.type !== 'execStep') return
          ignorePane.current = true
          const pick = (node.data as ExecStepData).pick
          if (pick) onSelect(pick)
        }}
        onPaneClick={() => {
          if (ignorePane.current) {
            ignorePane.current = false
            return
          }
          onDeselect()
        }}
        style={{ width: '100%', height: '100%' }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} color="#d5dbe3" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          style={{ background: '#f8fafc' }}
          nodeColor={(n) =>
            n.type === 'execStage' ? '#94a3b8' : n.type === 'execJob' ? '#1f2937' : '#93c5fd'
          }
        />
      </ReactFlow>
    </div>
  )
}

/**
 * 执行详情画布：点步骤打开右侧日志，点空白关闭；空白处拖动画布。
 * 节点摆放与编辑页保存的 layout 一致。
 */
export default function ExecutionCanvas({
  sequence,
  selected,
  onSelect,
  onDeselect,
}: {
  sequence: CanvasSequence
  selected: CanvasStepPick | null
  onSelect: (pick: CanvasStepPick) => void
  onDeselect: () => void
}) {
  const { nodes, edges } = useMemo(
    () => buildExecutionGraph(sequence, selected, onSelect),
    [sequence, selected, onSelect],
  )
  return (
    <div className="rp-flow" style={{ flex: 1, minWidth: 0, height: '100%', background: '#f3f5f8' }}>
      <ReactFlowProvider>
        <CanvasBoard incomingNodes={nodes} incomingEdges={edges} onSelect={onSelect} onDeselect={onDeselect} />
      </ReactFlowProvider>
    </div>
  )
}
