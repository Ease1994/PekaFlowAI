/**
 * 构建日志解析：一个 Job 的日志是一条流，这里把它切回每个步骤各自的段落，并判断每行的级别。
 *
 * 新版 Agent 会打结构化边界 `##[step]begin:{index}:{plugin}` / `##[step]end:{index}:{status}:{ms}`；
 * 老版本只有 `  step: {plugin}` 这种给人看的行，所以两种都得认，否则历史构建的日志会全糊在一起。
 */

const BEGIN = '##[step]begin:'
const END = '##[step]end:'
const LEGACY = /^\s{2}step:\s*(.+)$/

export interface StepLogSegment {
  index: number
  plugin: string
  lines: string[]
}

/** 按步骤切分日志。返回 null 表示这段日志没有任何步骤边界（例如平台步骤或极早期的日志）。 */
export function splitLogByStep(lines: string[]): StepLogSegment[] | null {
  const segments: StepLogSegment[] = []
  // 第一个步骤之前的行（Workspace 路径等 Job 级信息），并到第一步里，别丢掉
  const preamble: string[] = []
  let current: StepLogSegment | null = null
  let sawMarker = false

  const open = (index: number, plugin: string): StepLogSegment => {
    const seg: StepLogSegment = { index, plugin, lines: segments.length ? [] : preamble.slice() }
    segments.push(seg)
    return seg
  }

  for (const line of lines) {
    const text = line ?? ''

    if (text.startsWith(BEGIN)) {
      sawMarker = true
      const rest = text.slice(BEGIN.length)
      const sep = rest.indexOf(':')
      const index = Number(sep >= 0 ? rest.slice(0, sep) : rest)
      current = open(
        Number.isFinite(index) ? index : segments.length,
        sep >= 0 ? rest.slice(sep + 1) : '',
      )
      continue
    }
    if (text.startsWith(END)) {
      current = null
      continue
    }

    // 老 Agent 没有结构化标记，退回按 "  step: xxx" 分段
    const legacy = !sawMarker ? LEGACY.exec(text) : null
    if (legacy) {
      current = open(segments.length, legacy[1].trim())
      continue
    }

    if (!current) {
      if (!segments.length) preamble.push(text)
      else segments[segments.length - 1].lines.push(text)
      continue
    }
    current.lines.push(text)
  }

  return segments.length ? segments : null
}

/** 取某一步的日志；没有边界信息时退回整段（总比什么都不显示强）。 */
export function stepLogLines(lines: string[], stepOrder: number): string[] {
  const segments = splitLogByStep(lines)
  if (!segments) return lines
  const hit = segments.find((s) => s.index === stepOrder)
  return hit ? hit.lines : []
}

export type LogTone = 'error' | 'warn' | 'success' | 'command' | 'meta' | 'plain'

// MSBuild/csc：`path(line,col): error CS0246: ...`（常被插件包成 [INFO]: 前缀）
// 不用裸 \berror: —— 会漏掉「error CSxxxx」，又容易误伤「0 Error(s)」这类摘要
const COMPILER_CODE = String.raw`(?:CS|MSB|NETSDK|NU|FS|BC|IDE)\d+`

const ERROR_PATTERNS = [
  /\[ERROR\]/i,
  /\[CRITICAL\]/i,
  new RegExp(String.raw`\berror\s+${COMPILER_CODE}\b`, 'i'),
  /:\s*error\s+[A-Z]{0,10}\d+/i,
  /\berror:/i,
  /\bfatal:/i,
  /\bTraceback \(most recent call last\)/,
  /^\s*(step failed|job failed)/i,
  /\bbuild failed\b/i,
  /Build FAILED\b/i,
  /生成失败/,
  /失败，退出码/,
  /\bcommand not found\b/i,
  /\bpermission denied\b/i,
  /\bno such file or directory\b/i,
]
const WARN_PATTERNS = [
  /\[WARN(ING)?\]/i,
  new RegExp(String.raw`\bwarning\s+${COMPILER_CODE}\b`, 'i'),
  /:\s*warning\s+[A-Z]{0,10}\d+/i,
  /\bwarning:/i,
  /\bdeprecated\b/i,
]

/** 判断一行日志的级别，用来上色。 */
export function logTone(line: string): LogTone {
  const text = line ?? ''
  const trimmed = text.trim()
  if (!trimmed) return 'plain'

  // 退出码单独判：exit 0 是正常收尾，非 0 才是错误
  const exit = /^\(exit (-?\d+)\)$/.exec(trimmed)
  if (exit) return exit[1] === '0' ? 'success' : 'error'

  if (ERROR_PATTERNS.some((re) => re.test(text))) return 'error'
  if (WARN_PATTERNS.some((re) => re.test(text))) return 'warn'
  if (trimmed.startsWith('$ ')) return 'command'
  if (trimmed.startsWith('##[') || trimmed.startsWith('step:') || trimmed.startsWith('Workspace:')) {
    return 'meta'
  }
  return 'plain'
}

export const TONE_COLOR: Record<LogTone, string> = {
  error: '#f87171',
  warn: '#fbbf24',
  success: '#4ade80',
  command: '#7dd3fc',
  meta: '#9ca3af',
  plain: '#d4d4d4',
}
