/**
 * 执行画布何时可以 fitView。
 * React Flow 11 的 fitView 只会框「已经量到 width/height」的节点；
 * 没量到的节点直接排除。执行中途若在部分节点还没写回尺寸时 fit，
 * 镜头会缩到当前量到的那几步，其余步骤看起来像消失。
 */

export type MeasuredLike = {
  id?: string
  position: { x: number; y: number }
  width?: number | null
  height?: number | null
}

/** 和 React Flow 11 fitView 一样：没量到宽高的节点不进包围盒。 */
export function nodesIncludedInFitView<T extends { width?: number | null; height?: number | null }>(
  nodes: T[],
): T[] {
  return nodes.filter((n) => !!n.width && !!n.height)
}

/** 全部节点都量过尺寸，这时 fitView 才是整图。 */
export function allNodesMeasured(nodes: { width?: number | null; height?: number | null }[]): boolean {
  return nodes.length > 0 && nodes.every((n) => !!n.width && !!n.height)
}

/**
 * 只有画布宽度明显变了才重新 fit（右侧日志开关）。
 * 顶栏折行、健康条导致的高度变化不能动镜头，那是执行中偶发推飞的触发源。
 */
export function canvasWidthChanged(prev: number, next: number, threshold = 48): boolean {
  if (next < 16) return false
  if (!prev) return true
  return Math.abs(next - prev) >= threshold
}

/** 已量节点的轴对齐包围盒，用来判断没量到的节点会不会被镜头裁掉。 */
export function measuredBounds(nodes: MeasuredLike[]): {
  minX: number
  minY: number
  maxX: number
  maxY: number
} | null {
  const measured = nodesIncludedInFitView(nodes)
  if (!measured.length) return null
  return {
    minX: Math.min(...measured.map((n) => n.position.x)),
    minY: Math.min(...measured.map((n) => n.position.y)),
    maxX: Math.max(...measured.map((n) => n.position.x + (n.width || 0))),
    maxY: Math.max(...measured.map((n) => n.position.y + (n.height || 0))),
  }
}

/** 点是否落在包围盒内（含边）。 */
export function pointInBounds(
  x: number,
  y: number,
  box: { minX: number; minY: number; maxX: number; maxY: number },
): boolean {
  return x >= box.minX && x <= box.maxX && y >= box.minY && y <= box.maxY
}
