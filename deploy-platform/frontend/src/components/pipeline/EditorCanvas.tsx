import { useCallback, useEffect, useMemo, useRef, type CSSProperties, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import ReactFlow, {
  Background,
  BackgroundVariant,
  ConnectionLineType,
  Controls,
  MiniMap,
  Panel,
  ReactFlowProvider,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
} from 'reactflow'
import 'reactflow/dist/style.css'
import { Button, Dropdown } from 'antd'
import type { MenuProps } from 'antd'
import {
  PlusOutlined,
  AppstoreOutlined,
  CopyOutlined,
  DeleteOutlined,
  LeftOutlined,
  RightOutlined,
} from '@ant-design/icons'
import type { GraphJob, GraphStep, PipelineGraph } from '@/api/types'
import type { ConfigTarget } from './ConfigDrawer'
import { canCanvasLink, pruneOpenCuts, type CanvasRef } from '@/utils/canvasGraph'
import {
  computeCompactLayout,
  effectiveSavedLayout,
  jobNodeId,
  NODE_W,
  resolveNodePos,
  STAGE_W,
  stageNodeId,
  stepNodeId,
  canvasEdgeId,
  type CanvasLayout,
} from '@/utils/canvasLayout'
import { allNodesMeasured } from '@/utils/canvasViewport'
import { useT } from '@/i18n'

export type { CanvasLayout }

/**
 * 编辑画布操作。排版仍是阶段 → Job → 步骤链式节点；
 * 操作上：拉线改顺序、右键复制/调序/删除/拆并行、Delete 删选中节点。
 */
export type EditorCanvasActions = {
  /** 左键打开配置抽屉 */
  onOpenConfig: (target: ConfigTarget) => void
  /** 在该阶段新增 Job */
  onAddJob: (stageId: string) => void
  /** 在 Job 末尾或指定步骤后插入步骤 */
  onAddStep: (stageId: string, jobId: string, afterIndex?: number) => void
  /** 在指定阶段后插入阶段；null 表示第一条 */
  onAddStage: (afterStageId: string | null) => void
  onCloseConfig: () => void
  /** 拖动节点后写回 YAML 坐标 */
  onLayoutChange: (layout: CanvasLayout) => void
  /** 从圆点拉线：改顺序或并入 Job */
  onLink: (source: CanvasRef, target: CanvasRef) => void
  /**
   * 选中线条后 Delete：只断开箭头，不新建构建环境。
   * positions 是当前画布坐标。
   */
  onUnlink: (source: CanvasRef, target: CanvasRef, positions: CanvasLayout) => void
  /** 把该步骤拆到同阶段新 Job，与原 Job 并行 */
  onSplitParallel: (step: Extract<CanvasRef, { kind: 'step' }>) => void
  onDuplicateStep: (stageId: string, jobId: string, stepIndex: number) => void
  onDeleteStage: (stageId: string) => void
  onDeleteJob: (stageId: string, jobId: string) => void
  onDeleteStep: (stageId: string, jobId: string, stepIndex: number) => void
  /** 同一 Job 内前移 / 后移 */
  onMoveStep: (stageId: string, jobId: string, from: number, to: number) => void
}

const nodeTypes = {
  editorStage: EditorStageNode,
  editorJob: EditorJobNode,
  editorStep: EditorStepNode,
}

/**
 * 编辑节点的业务数据：画布引用、选中态、以及右键/加号对应的操作。
 * kind 区分阶段 / Job / 步骤；canvasRef 给拉线用；其余回调按节点类型选用。
 */
type EditorNodeData = {
  /** 节点种类，决定卡片样式和菜单 */
  kind: 'stage' | 'job' | 'step'
  /** 拉线时识别源/目标 */
  canvasRef: CanvasRef
  /** 是否正在被配置抽屉选中 */
  selected: boolean
  /** 左键打开配置 */
  onOpen: () => void
  onAddJob?: () => void
  onAddStep?: () => void
  onAddStage?: () => void
  onSplitParallel?: () => void
  onDuplicate?: () => void
  onDelete?: () => void
  onMovePrev?: () => void
  onMoveNext?: () => void
  name?: string
  order?: number
  job?: GraphJob
  step?: GraphStep
}

/**
 * 节点坐标：用户拖过且不重叠的 layout 优先，否则用与「自动排版」相同的折行分列。
 */
export function buildEditorGraph(
  graph: PipelineGraph,
  target: ConfigTarget | null,
  actions: EditorCanvasActions,
): { nodes: Node[]; edges: Edge[] } {
  const saved = effectiveSavedLayout(graph.layout, graph.stages)
  const compact = computeCompactLayout(graph.stages)
  const pos = (id: string) => resolveNodePos(saved, compact, id)
  const hidden = new Set(pruneOpenCuts(graph.stages, graph.open_cuts))
  const nodes: Node[] = []
  const edges: Edge[] = []

  graph.stages.forEach((stage, si) => {
    const sid = stageNodeId(stage.id)
    nodes.push({
      id: sid,
      type: 'editorStage',
      position: pos(sid),
      data: {
        kind: 'stage',
        canvasRef: { kind: 'stage', stageId: stage.id },
        name: stage.name,
        order: stage.order,
        selected: target?.type === 'stage' && target.stage.id === stage.id,
        onOpen: () => actions.onOpenConfig({ type: 'stage', stage, index: si }),
        onAddJob: () => actions.onAddJob(stage.id),
        onAddStage: () => actions.onAddStage(stage.id),
        onDelete: () => actions.onDeleteStage(stage.id),
      } satisfies EditorNodeData,
    })

    stage.jobs.forEach((job, ji) => {
      const jid = jobNodeId(stage.id, job.id)
      nodes.push({
        id: jid,
        type: 'editorJob',
        position: pos(jid),
        data: {
          kind: 'job',
          canvasRef: { kind: 'job', stageId: stage.id, jobId: job.id },
          job,
          selected: target?.type === 'job' && target.job.id === job.id && target.stageIndex === si,
          onOpen: () => actions.onOpenConfig({ type: 'job', job, stageIndex: si, jobIndex: ji }),
          onAddJob: () => actions.onAddJob(stage.id),
          onAddStep: () => actions.onAddStep(stage.id, job.id),
          onDelete: () => actions.onDeleteJob(stage.id, job.id),
        } satisfies EditorNodeData,
      })
      if (ji === 0) pushEdge(edges, hidden, sid, jid)

      let prevId = jid
      job.steps.forEach((step, sti) => {
        const id = stepNodeId(stage.id, job.id, sti)
        const stepRef: Extract<CanvasRef, { kind: 'step' }> = {
          kind: 'step',
          stageId: stage.id,
          jobId: job.id,
          stepIndex: sti,
        }
        nodes.push({
          id,
          type: 'editorStep',
          position: pos(id),
          data: {
            kind: 'step',
            canvasRef: stepRef,
            step,
            selected:
              target?.type === 'step' &&
              target.stageIndex === si &&
              target.jobIndex === ji &&
              target.stepIndex === sti,
            onOpen: () =>
              actions.onOpenConfig({
                type: 'step',
                step,
                stageIndex: si,
                jobIndex: ji,
                stepIndex: sti,
              }),
            onAddStep: () => actions.onAddStep(stage.id, job.id, sti),
            onSplitParallel: () => actions.onSplitParallel(stepRef),
            onDuplicate: () => actions.onDuplicateStep(stage.id, job.id, sti),
            onDelete: () => actions.onDeleteStep(stage.id, job.id, sti),
            onMovePrev: sti > 0 ? () => actions.onMoveStep(stage.id, job.id, sti, sti - 1) : undefined,
            onMoveNext:
              sti < job.steps.length - 1
                ? () => actions.onMoveStep(stage.id, job.id, sti, sti + 1)
                : undefined,
          } satisfies EditorNodeData,
        })
        pushEdge(edges, hidden, prevId, id)
        prevId = id
      })
    })
  })

  return { nodes, edges }
}

/** 顺序箭头可选中、可 Delete；不能拖端点改接，改顺序请从加号拖出拉线。 */
function makeEdge(source: string, target: string): Edge {
  return {
    id: canvasEdgeId(source, target),
    source,
    target,
    type: 'smoothstep',
    updatable: false,
    deletable: true,
    focusable: true,
    className: 'rp-seq-edge',
    interactionWidth: 24,
    style: { stroke: '#cbd5e1', strokeWidth: 2 },
  }
}

/** 被用户删掉的顺序箭头不再画出来，Job 归属不变。 */
function pushEdge(edges: Edge[], hidden: Set<string>, source: string, target: string) {
  const edge = makeEdge(source, target)
  if (!hidden.has(edge.id)) edges.push(edge)
}

/** 从 React Flow 节点取出拉线用的画布引用。 */
function refOf(node: Node | undefined): CanvasRef | undefined {
  return node ? (node.data as EditorNodeData).canvasRef : undefined
}

/**
 * React Flow 画板：同步节点、连线、自动排版、拉线改顺序、选中线条 Delete 断开。
 * 节点 Delete 由外层按选中配置项处理；线条 Delete 在这里拦截，避免误删节点。
 */
function CanvasBoard({
  graph,
  initialNodes,
  initialEdges,
  onCloseConfig,
  onLayoutChange,
  onLink,
  onUnlink,
}: {
  graph: PipelineGraph
  initialNodes: Node[]
  initialEdges: Edge[]
  onCloseConfig: () => void
  onLayoutChange: (layout: CanvasLayout) => void
  onLink: (source: CanvasRef, target: CanvasRef) => void
  onUnlink: (source: CanvasRef, target: CanvasRef, positions: CanvasLayout) => void
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges)
  const { fitView, getNodes, getNode } = useReactFlow()
  const ignorePane = useRef(false)
  const t = useT()
  /**
   * 等全部节点量到宽高后再 fitView，打开编辑器时图在画布里居中，
   * 不会贴在左上角盖住说明。加步骤 / 删线不再拉镜头。
   */
  const didFit = useRef(false)
  /** 最新连线（含选中态），键盘 Delete 不能闭包到旧的 edges。 */
  const edgesRef = useRef(edges)
  edgesRef.current = edges

  /** 把选中的线交给编排逻辑：断开后面那一步，节点留在同一行。 */
  const unlinkSelected = useCallback(
    (selected: Edge[]) => {
      if (!selected.length) return
      const positions: CanvasLayout = {}
      for (const n of getNodes()) positions[n.id] = n.position
      for (const ed of selected) {
        const source = refOf(getNode(ed.source))
        const target = refOf(getNode(ed.target))
        if (source && target) onUnlink(source, target, positions)
      }
    },
    [getNode, getNodes, onUnlink],
  )

  useEffect(() => {
    setNodes((current) => {
      const byId = new Map(current.map((n) => [n.id, n]))
      return initialNodes.map((n) => {
        const prev = byId.get(n.id)
        return prev ? { ...n, position: prev.position } : n
      })
    })
    setEdges((current) => {
      const selectedIds = new Set(current.filter((e) => e.selected).map((e) => e.id))
      return initialEdges.map((e) => (selectedIds.has(e.id) ? { ...e, selected: true } : e))
    })
  }, [initialNodes, initialEdges, setNodes, setEdges])

  useEffect(() => {
    if (didFit.current) return
    let timer = 0
    let tries = 0
    const fitWhenReady = () => {
      if (didFit.current) return
      if (!allNodesMeasured(getNodes())) {
        if (tries < 24) {
          tries += 1
          timer = window.setTimeout(fitWhenReady, 50)
        }
        return
      }
      didFit.current = true
      fitView({ padding: 0.22, duration: 280, includeHiddenNodes: true })
    }
    timer = window.setTimeout(fitWhenReady, 50)
    return () => window.clearTimeout(timer)
  }, [fitView, getNodes])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const el = e.target as HTMLElement | null
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return
      const selected = edgesRef.current.filter((ed) => ed.selected)
      if (!selected.length) return
      e.preventDefault()
      e.stopPropagation()
      unlinkSelected(selected)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [unlinkSelected])

  /** 一键按阶段分列、步骤折行，并缩放到整图可见。 */
  const applyAutoLayout = () => {
    const next = computeCompactLayout(graph.stages)
    setNodes((nds) => nds.map((n) => ({ ...n, position: next[n.id] || n.position })))
    onLayoutChange(next)
    window.setTimeout(() => fitView({ padding: 0.22, duration: 280, includeHiddenNodes: true }), 40)
  }

  const applyConnection = (c: Connection) => {
    const source = refOf(getNode(c.source || ''))
    const target = refOf(getNode(c.target || ''))
    if (!canCanvasLink(source, target) || !source || !target) return
    onLink(source, target)
  }

  return (
    <ReactFlow
      className="rp-flow"
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={applyConnection}
      isValidConnection={(c) =>
        canCanvasLink(refOf(getNode(c.source || '')), refOf(getNode(c.target || '')))
      }
      nodesDraggable
      nodesConnectable
      edgesFocusable
      deleteKeyCode={null}
      onEdgeContextMenu={(ev, edge) => {
        ev.preventDefault()
        unlinkSelected([edge])
      }}
      connectionLineType={ConnectionLineType.SmoothStep}
      // 吸附半径：对准目标节点左侧时更容易接上
      connectionRadius={36}
      selectNodesOnDrag={false}
      panOnDrag
      zoomOnScroll
      minZoom={0.3}
      maxZoom={1.6}
      proOptions={{ hideAttribution: true }}
      onNodeClick={(_e, node) => {
        if (Date.now() < ignoreNodeClickUntil) return
        ignorePane.current = true
        const data = node.data as EditorNodeData
        data.onOpen?.()
      }}
      onEdgeClick={() => {
        ignorePane.current = true
        onCloseConfig()
      }}
      onNodeDragStop={() => {
        const next: CanvasLayout = {}
        for (const n of getNodes()) next[n.id] = n.position
        onLayoutChange(next)
      }}
      onPaneClick={() => {
        if (ignorePane.current) {
          ignorePane.current = false
          return
        }
        onCloseConfig()
      }}
      style={{ width: '100%', height: '100%' }}
    >
      <Panel position="top-left">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxWidth: 380 }}>
          <Button
            size="small"
            icon={<AppstoreOutlined />}
            className="nodrag nopan"
            onClick={applyAutoLayout}
          >
            {t('pipe.autoLayout')}
          </Button>
          <div style={{ fontSize: 11, color: '#64748b', lineHeight: 1.45, pointerEvents: 'none' }}>
            {t('pipe.canvasHint')}
          </div>
          {!!pruneOpenCuts(graph.stages, graph.open_cuts).length && (
            <div style={{ fontSize: 12, color: '#d46b08', lineHeight: 1.45, pointerEvents: 'none' }}>
              {t('pipe.openCutsWarn')}
            </div>
          )}
        </div>
      </Panel>
      <Background variant={BackgroundVariant.Dots} gap={18} size={1.2} color="#d5dbe3" />
      <Controls />
      <MiniMap pannable zoomable style={{ background: '#f8fafc' }} nodeColor="#93c5fd" />
    </ReactFlow>
  )
}

/**
 * 编辑画布：排版与改操作前一致；操作走加号、拉线和右键。
 */
export default function EditorCanvas({
  graph,
  configTarget,
  actions,
}: {
  graph: PipelineGraph
  configTarget: ConfigTarget | null
  actions: EditorCanvasActions
}) {
  const t = useT()
  const { nodes, edges } = useMemo(
    () => buildEditorGraph(graph, configTarget, actions),
    [graph, configTarget, actions],
  )

  if (graph.stages.length === 0) {
    return (
      <div
        style={{
          width: '100%',
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f3f5f8',
        }}
      >
        <Button type="primary" size="large" icon={<PlusOutlined />} onClick={() => actions.onAddStage(null)}>
          {t('pipe.addFirstStage')}
        </Button>
      </div>
    )
  }

  return (
    <div className="rp-flow" style={{ width: '100%', height: '100%', background: '#f3f5f8' }}>
      <ReactFlowProvider>
        <CanvasBoard
          graph={graph}
          initialNodes={nodes}
          initialEdges={edges}
          onCloseConfig={actions.onCloseConfig}
          onLayoutChange={actions.onLayoutChange}
          onLink={actions.onLink}
          onUnlink={actions.onUnlink}
        />
      </ReactFlowProvider>
    </div>
  )
}

/** 悬停才出现的圆形加号，点了不拖动节点。 */
function HoverPlus({
  title,
  onClick,
  style,
}: {
  title: string
  onClick: () => void
  style: CSSProperties
}) {
  return (
    <button
      type="button"
      className="nodrag nopan rp-hover-plus"
      title={title}
      style={style}
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => {
        e.stopPropagation()
        onClick()
      }}
    >
      <PlusOutlined />
    </button>
  )
}

/**
 * 右键菜单关掉后，同一击的 click 会落到 React Flow 节点上，误开配置抽屉。
 * 点过菜单后短暂忽略节点 click。
 */
let ignoreNodeClickUntil = 0

/**
 * 加号即连接点：点击新增，拖出加号拉线。
 * 整颗圆都要能拖出线，图标本身不抢指针，避免只能点到中间一小块。
 */
function PlusSourceHandle({
  title,
  onAdd,
}: {
  title: string
  onAdd: () => void
}) {
  const origin = useRef<{ x: number; y: number } | null>(null)
  const dragged = useRef(false)

  return (
    <Handle
      type="source"
      position={Position.Right}
      className="nodrag nopan rp-plus-handle"
      title={title}
      style={{ width: 32, height: 32, pointerEvents: 'all' }}
      onPointerDownCapture={(e) => {
        origin.current = { x: e.clientX, y: e.clientY }
        dragged.current = false
        ignoreNodeClickUntil = Date.now() + 500
        const onMove = (ev: MouseEvent) => {
          if (!origin.current) return
          const dx = ev.clientX - origin.current.x
          const dy = ev.clientY - origin.current.y
          if (dx * dx + dy * dy > 36) dragged.current = true
        }
        const onUp = () => {
          window.removeEventListener('mousemove', onMove)
          window.removeEventListener('mouseup', onUp)
        }
        window.addEventListener('mousemove', onMove)
        window.addEventListener('mouseup', onUp)
      }}
      onClick={(e) => {
        e.stopPropagation()
        if (dragged.current) return
        onAdd()
      }}
    >
      <PlusOutlined className="rp-plus-handle-icon" />
    </Handle>
  )
}

/**
 * 右键菜单浮层：拦住指针事件，避免 mousedown 传到画布；
 * preventDefault 让浮层别在 mousedown 时先卸掉，click 才能打到菜单项。
 */
function renderCanvasMenu(menu: ReactNode) {
  return (
    <div
      className="nodrag nopan"
      onMouseDown={(e: ReactMouseEvent) => {
        e.stopPropagation()
        e.preventDefault()
      }}
      onClick={(e: ReactMouseEvent) => e.stopPropagation()}
    >
      {menu}
    </div>
  )
}

/** 菜单项回调：拦住冒泡，并忽略随后落到节点上的 click。 */
function menuAction(fn?: () => void): NonNullable<MenuProps['onClick']> {
  return ({ domEvent }) => {
    domEvent.stopPropagation()
    ignoreNodeClickUntil = Date.now() + 400
    fn?.()
  }
}

/** 阶段卡片：左侧起点，右键可加 Job / 加阶段 / 删除。 */
function EditorStageNode({ data }: { data: EditorNodeData }) {
  const t = useT()
  return (
    <Dropdown
      trigger={['contextMenu']}
      dropdownRender={renderCanvasMenu}
      menu={{
        items: [
          { key: 'job', label: t('pipe.addJob'), onClick: menuAction(() => data.onAddJob?.()) },
          { key: 'stage', label: t('pipe.addStageAfterMenu'), onClick: menuAction(() => data.onAddStage?.()) },
          { type: 'divider' },
          { key: 'del', danger: true, icon: <DeleteOutlined />, label: t('pipe.deleteStageShort'), onClick: menuAction(() => data.onDelete?.()) },
        ],
      }}
    >
      <div
        className="rp-editor-node"
        style={{
          width: STAGE_W - 8,
          padding: '10px 12px',
          borderRadius: 10,
          background: data.selected ? '#e6f4ff' : '#fff',
          border: data.selected ? '2px solid #1677ff' : '1px solid #e5e7eb',
          boxShadow: '0 4px 12px rgba(15,23,42,0.04)',
        }}
      >
        <div style={{ fontSize: 11, color: '#9ca3af', letterSpacing: 1 }}>{t('pipe.stageN', { n: data.order ?? '' })}</div>
        <div style={{ fontSize: 13, fontWeight: 700, color: '#111827', marginTop: 2 }}>{data.name}</div>
        <PlusSourceHandle title={t("pipe.clickAddStage")} onAdd={() => data.onAddStage?.()} />
        <HoverPlus title={t("pipe.addJob")} onClick={() => data.onAddJob?.()} style={{ left: '50%', bottom: -12, marginLeft: -11 }} />
      </div>
    </Dropdown>
  )
}

/** 深色小 Job 标签：独立节点，在步骤链左侧。 */
function EditorJobNode({ data }: { data: EditorNodeData }) {
  const t = useT()
  const job = data.job
  if (!job) return null
  return (
    <Dropdown
      trigger={['contextMenu']}
      dropdownRender={renderCanvasMenu}
      menu={{
        items: [
          { key: 'step', label: t('pipe.addStep'), onClick: menuAction(() => data.onAddStep?.()) },
          { key: 'job', label: t('pipe.addParallelJobMenu'), onClick: menuAction(() => data.onAddJob?.()) },
          { type: 'divider' },
          { key: 'del', danger: true, icon: <DeleteOutlined />, label: t('pipe.deleteJob'), onClick: menuAction(() => data.onDelete?.()) },
        ],
      }}
    >
      <div
        className="rp-editor-node"
        style={{
          width: 120,
          padding: '8px 10px',
          borderRadius: 10,
          background: data.selected ? '#111827' : '#1f2937',
          border: data.selected ? '2px solid #1677ff' : '1px solid #111827',
          color: '#fff',
        }}
      >
        <Handle type="target" position={Position.Left} className="rp-target-handle" />
        <div style={{ fontSize: 11, color: '#93c5fd' }}>{job.id}</div>
        <div
          style={{
            fontSize: 12,
            fontWeight: 600,
            marginTop: 2,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {job.agent_icon} {job.name}
        </div>
        <PlusSourceHandle title={t("pipe.clickAddStep")} onAdd={() => data.onAddStep?.()} />
        <HoverPlus title={t("pipe.addParallelJobMenu")} onClick={() => data.onAddJob?.()} style={{ left: '50%', bottom: -12, marginLeft: -11 }} />
      </div>
    </Dropdown>
  )
}

/** 步骤卡片：独立节点，右键复制 / 调序 / 拆并行 / 删除。 */
function EditorStepNode({ data }: { data: EditorNodeData }) {
  const t = useT()
  const step = data.step
  if (!step) return null
  return (
    <Dropdown
      trigger={['contextMenu']}
      dropdownRender={renderCanvasMenu}
      menu={{
        items: [
          { key: 'add', label: t('pipe.addStepAfter'), onClick: menuAction(() => data.onAddStep?.()) },
          {
            key: 'prev',
            label: t('pipe.movePrev'),
            icon: <LeftOutlined />,
            disabled: !data.onMovePrev,
            onClick: menuAction(() => data.onMovePrev?.()),
          },
          {
            key: 'next',
            label: t('pipe.moveNext'),
            icon: <RightOutlined />,
            disabled: !data.onMoveNext,
            onClick: menuAction(() => data.onMoveNext?.()),
          },
          { key: 'dup', label: t('pipe.copyStep'), icon: <CopyOutlined />, onClick: menuAction(() => data.onDuplicate?.()) },
          { key: 'split', label: t('pipe.splitParallel'), onClick: menuAction(() => data.onSplitParallel?.()) },
          { type: 'divider' },
          { key: 'del', danger: true, icon: <DeleteOutlined />, label: t('pipe.deleteStep'), onClick: menuAction(() => data.onDelete?.()) },
        ],
      }}
    >
      <div
        className="rp-editor-node"
        style={{
          width: NODE_W,
          background: '#fff',
          borderRadius: 12,
          border: data.selected ? '2px solid #1677ff' : '1px solid #e5e7eb',
          boxShadow: data.selected
            ? '0 0 0 3px rgba(22,119,255,0.18)'
            : '0 8px 24px rgba(15,23,42,0.06)',
          overflow: 'visible',
        }}
      >
        <Handle type="target" position={Position.Left} className="rp-target-handle" />
        <div style={{ height: 4, background: '#1677ff', borderRadius: '12px 12px 0 0' }} />
        <div style={{ padding: '10px 12px 12px', display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <span style={{ fontSize: 16 }}>{step.icon || '🔧'}</span>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: '#111827',
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {step.name || step.display_name || step.plugin}
            </div>
            <div style={{ fontSize: 11, color: '#9ca3af', marginTop: 2 }}>{step.plugin}</div>
          </div>
        </div>
        <PlusSourceHandle title={t("pipe.clickAddStep")} onAdd={() => data.onAddStep?.()} />
      </div>
    </Dropdown>
  )
}
