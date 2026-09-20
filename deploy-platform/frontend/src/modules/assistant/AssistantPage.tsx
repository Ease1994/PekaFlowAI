import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Card,
  Drawer,
  Dropdown,
  Empty,
  Input,
  List,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd'
import {
  ApartmentOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CheckOutlined,
  ClearOutlined,
  CloseCircleOutlined,
  CloseOutlined,
  DeleteOutlined,
  DownOutlined,
  LoadingOutlined,
  PlusOutlined,
  PaperClipOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  SearchOutlined,
  SendOutlined,
  ToolOutlined,
  UnorderedListOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { del, get, patch, post, postForm } from '@/api/client'
import type {
  AssistantCatalogItem,
  AssistantEvent,
  AssistantMode,
  AssistantSession,
  HarnessComponent,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useIsMobile } from '@/hooks/useIsMobile'
import { t, useT } from '@/i18n'

interface Attachment {
  id: number
  name: string
  rel_path: string
  size_bytes: number
}

/** 拖进来的可能是文件夹，递归摊平成文件，同时留住相对路径 */
async function collectFiles(items: DataTransferItemList): Promise<{ file: File; path: string }[]> {
  const out: { file: File; path: string }[] = []

  const walk = async (entry: any, prefix: string): Promise<void> => {
    if (!entry) return
    if (entry.isFile) {
      const file: File = await new Promise((resolve, reject) => entry.file(resolve, reject))
      out.push({ file, path: prefix ? `${prefix}/${file.name}` : file.name })
      return
    }
    if (entry.isDirectory) {
      const reader = entry.createReader()
      // readEntries 一次最多给 100 条，得反复读到空为止
      for (;;) {
        const batch: any[] = await new Promise((resolve, reject) =>
          reader.readEntries(resolve, reject)
        )
        if (!batch.length) break
        for (const child of batch) {
          await walk(child, prefix ? `${prefix}/${entry.name}` : entry.name)
        }
      }
    }
  }

  const entries = Array.from(items)
    .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean)
  for (const entry of entries) {
    await walk(entry, '')
  }
  return out
}

function sizeText(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

interface ChatMessage {
  id?: number
  role: 'user' | 'assistant'
  content: string
  /** 用户发出去的图。本地回显是 data URL，落库后是会话短链。 */
  images?: string[]
  actions?: ActionCard[]
  traces?: SkillTrace[]
  kind?: string
  events?: AssistantEvent[]
  reasoning?: string
  model?: ModelInfo
  /** 本轮流失败时落在对话框里，避免只 toast 一下就没了 */
  failed?: boolean
}

interface ActionCard {
  type: string
  label: string
  payload: Record<string, unknown>
  // 服务端签发的一次性确认票据，确认时必须原样回传
  token?: string
  session_id?: string | number
}

interface SkillTrace {
  id?: string
  name: string
  arguments?: Record<string, unknown>
  result?: unknown
}

interface ModelInfo {
  name?: string
  model_id?: string
  provider_name?: string
}

interface StreamEvent {
  type: 'status' | 'reasoning' | 'text' | 'tool' | 'approval' | 'done' | 'error'
  text?: string
  name?: string
  phase?: 'start' | 'end'
  message?: string
  model?: ModelInfo
  adapter?: string
  content?: string
  arguments?: Record<string, unknown>
  result?: unknown
  reply?: string
  actions?: ActionCard[]
  traces?: SkillTrace[]
  watching?: boolean
}

interface AssistantModel {
  id: number
  name: string
  model_id: string
  provider_name: string
  description?: string
  max_tokens?: number
  context_window?: number
  capabilities?: string[]
  supports_vision?: boolean
  is_default?: boolean
}

const MODEL_STORE_KEY = 'assistant-model-id'
const SESSION_STORE_KEY = 'assistant-session-id'

function readStoredSessionId(): string | null {
  try {
    return localStorage.getItem(SESSION_STORE_KEY)
  } catch {
    return null
  }
}

function writeStoredSessionId(id: string | number) {
  try {
    localStorage.setItem(SESSION_STORE_KEY, String(id))
  } catch {
    /* 隐私模式写不进去就算了 */
  }
}

function readStoredModelId(): number | null {
  try {
    const raw = Number(localStorage.getItem(MODEL_STORE_KEY) || 0)
    return raw > 0 ? raw : null
  } catch {
    return null
  }
}

function writeStoredModelId(id: number) {
  try {
    localStorage.setItem(MODEL_STORE_KEY, String(id))
  } catch {
    /* 隐私模式写不进去就算了，会话里还有一份 */
  }
}

function windowLabel(n?: number): string {
  const v = Number(n || 0)
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(v % 1_000_000 ? 1 : 0)}M`
  if (v >= 1000) return `${Math.round(v / 1000)}K`
  return v ? String(v) : ''
}

/** 模型下拉的副标题：厂商、模型 id、上下文窗口。 */
function modelHint(m: AssistantModel): string {
  const bits = [m.provider_name, m.model_id]
  const ctx = windowLabel(m.context_window)
  if (ctx) bits.push(t('assistant.contextWindow', { n: ctx }))
  return bits.filter(Boolean).join(' · ')
}

interface PendingImage {
  name: string
  url: string
}

interface Conversation {
  conversation_id: number
  messages: ChatMessage[]
  watching: boolean
  /** 服务端还在跑的一轮。刷新后靠它重挂绿气泡，不是浏览器里那根已断的 SSE。 */
  live?: LiveSnapshot | null
}

/** GET 会话投影里的进行中一轮。 */
interface LiveSnapshot {
  running: boolean
  status?: string
  turn?: number
}

interface Skill {
  name: string
  description: string
  category: string
  examples: string[]
  confirm: boolean
}

/** 技能分类码 → 界面句子。渲染时取，避免语言切换后还停在导入时的文案。 */
function categoryLabel(cat: string): string {
  const keys: Record<string, string> = {
    catalog: 'assistant.catCatalog',
    observe: 'assistant.catObserve',
    diagnose: 'assistant.catDiagnose',
    delivery: 'assistant.catDelivery',
    access: 'notify.access',
    authoring: 'assistant.catAuthoring',
    meta: 'assistant.catCatalog',
  }
  return keys[cat] ? t(keys[cat]) : cat
}

/** 回显和落库比对时去掉首尾空白，避免后端 strip 后对不上画出两条。 */
function userTextKey(content: string): string {
  return (content || '').trim()
}

/**
 * 两份附图收成一份。已经能直接画的 data URL 优先，
 * 免得落库短链顶掉回显时气泡先闪成空图再去拉文件。
 */
function pickImages(a?: string[], b?: string[]): string[] | undefined {
  const left = (a || []).filter(Boolean)
  const right = (b || []).filter(Boolean)
  const ready = (urls: string[]) => urls.some((url) => url.startsWith('data:'))
  if (ready(left)) return left
  if (ready(right)) return right
  if (left.length) return left
  if (right.length) return right
  return undefined
}

function messageKey(msg: ChatMessage): string {
  return `${msg.role}\u0000${userTextKey(msg.content)}`
}

function mergeById(prev: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const ids = new Set(prev.map((m) => m.id).filter(Boolean) as number[])
  const add = incoming.filter((m) => m.id && !ids.has(m.id))
  if (!add.length) return collapseUserDupes(prev)
  // 本地回显没有 id，只按 id 去重的话，后台轮询把落库副本拉回来时同一句话会并排出现
  // 两条。这里一并把「已被落库副本取代」的回显去掉。
  const persistedKeys = new Set(add.map(messageKey))
  const localByKey = new Map(
    prev.filter((m) => !m.id).map((m) => [messageKey(m), m] as const),
  )
  const kept = prev.filter((m) => {
    if (isThinking(m) && add.some((item) => item.role === 'assistant')) return false
    return m.id || !persistedKeys.has(messageKey(m))
  })
  const mergedAdd = add.map((m) => {
    const local = localByKey.get(messageKey(m))
    if (!local) return m
    return {
      ...m,
      traces: m.traces?.length ? m.traces : local.traces,
      actions: m.actions?.length ? m.actions : local.actions,
      images: pickImages(local.images, m.images),
    }
  })
  return collapseUserDupes([...kept, ...mergedAdd])
}

function isThinking(msg: ChatMessage): boolean {
  return msg.kind === 'thinking'
}

/**
 * 同一句用户话的回显和落库副本收成一条。
 * 中间夹着思考占位时也算紧挨着，否则会画出两个蓝气泡。
 */
function collapseUserDupes(messages: ChatMessage[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const msg of messages) {
    if (isThinking(msg)) {
      out.push(msg)
      continue
    }
    let i = out.length - 1
    while (i >= 0 && isThinking(out[i])) i -= 1
    const prev = i >= 0 ? out[i] : undefined
    if (
      prev
      && prev.role === 'user'
      && msg.role === 'user'
      && userTextKey(prev.content) === userTextKey(msg.content)
    ) {
      const persisted = prev.id ? prev : msg
      const local = prev.id ? msg : prev
      out[i] = {
        ...local,
        ...persisted,
        id: persisted.id,
        images: pickImages(local.images, persisted.images),
      }
      continue
    }
    out.push(msg)
  }
  return out
}

/** 欢迎语没有轨迹，不能当成这一轮助手回复。
 *  msg: 待判断的气泡。
 *  welcome: 当前页生成的开场白，用正文比对而不是对象引用。
 */
function isWelcomeMessage(msg: ChatMessage, welcome: ChatMessage): boolean {
  return (
    msg.role === 'assistant'
    && !msg.id
    && !msg.failed
    && !(msg.traces?.length)
    && !(msg.actions?.length)
    && userTextKey(msg.content) === userTextKey(welcome.content)
  )
}

/** 这句话在页面上会画出气泡（用户句，或带正文/轨迹/失败标记的助手句）。
 *  msg: 待判断的气泡。
 *  welcome: 开场白，用来排除欢迎语。
 */
function hasVisibleBubble(msg: ChatMessage, welcome: ChatMessage): boolean {
  if (isThinking(msg) || isWelcomeMessage(msg, welcome)) return false
  if (msg.role === 'user') return Boolean(userTextKey(msg.content) || msg.images?.length)
  return Boolean(msg.content || msg.traces?.length || msg.failed || msg.actions?.length)
}

/**
 * 整页重拉时用服务端投影做底，把尚未落库的本地气泡按原对话顺序补回去。
 *
 * 切走 AI Agent 页会卸载组件，SSE 画出来的助手句没有 id。
 * 若只保留用户回显，失败回复会在回来时消失。
 *
 * prev: 卸载前或缓存里的气泡。
 * incoming: GET 会话投影。
 * welcome: 投影为空且本地也没有对话时的开场白。
 */
function withLocalEchoes(prev: ChatMessage[], incoming: ChatMessage[], welcome: ChatMessage): ChatMessage[] {
  const incomingChat = incoming.filter((m) => !isThinking(m))
  // 投影为空时不要清成欢迎语：多半是拉记录失败，本地气泡还在。
  if (!incomingChat.length) {
    const local = prev.filter((m) => hasVisibleBubble(m, welcome))
    return local.length ? collapseUserDupes(local) : [welcome]
  }

  const out: ChatMessage[] = collapseUserDupes([...incomingChat])
  const taken = new Set(out.map(messageKey))
  const prevClean = prev.filter((m) => !isThinking(m))

  for (let i = 0; i < prevClean.length; i += 1) {
    const msg = prevClean[i]
    if (isWelcomeMessage(msg, welcome)) continue
    // 投影里已有同一句用户话时，不能把本地回显整段跳过：落库记录常常不带图，
    // 回显上的 data URL 丢了，气泡里就只剩文字。
    if (taken.has(messageKey(msg))) {
      if (msg.role === 'user' && msg.images?.length) {
        const idx = out.findIndex((row) => messageKey(row) === messageKey(msg))
        if (idx >= 0) {
          out[idx] = { ...out[idx], images: pickImages(msg.images, out[idx].images) }
        }
      }
      continue
    }
    if (msg.id || !hasVisibleBubble(msg, welcome)) continue

    // 用户回显还没落库，接到投影末尾，去重函数会收成一条。
    if (msg.role === 'user') {
      out.push(msg)
      taken.add(messageKey(msg))
      continue
    }

    // 助手气泡插回对应那句用户话后面；投影里若是空壳则用本地正文填上。
    let userKey = ''
    for (let j = i - 1; j >= 0; j -= 1) {
      if (prevClean[j].role === 'user') {
        userKey = userTextKey(prevClean[j].content)
        break
      }
    }
    let placed = false
    if (userKey) {
      for (let j = 0; j < out.length; j += 1) {
        if (out[j].role !== 'user' || userTextKey(out[j].content) !== userKey) continue
        const next = out[j + 1]
        if (next?.role === 'assistant') {
          const nextEmpty = !next.content && !(next.traces?.length)
          const localHasBody = Boolean(msg.content || msg.traces?.length || msg.failed)
          if (nextEmpty && localHasBody) {
            out[j + 1] = {
              ...next,
              content: msg.content || next.content,
              traces: next.traces?.length ? next.traces : msg.traces,
              actions: next.actions?.length ? next.actions : msg.actions,
              kind: msg.kind && msg.kind !== 'chat' ? msg.kind : next.kind,
              failed: Boolean(next.failed || msg.failed),
            }
          }
          placed = true
          break
        }
        out.splice(j + 1, 0, msg)
        placed = true
        break
      }
    }
    if (!placed) out.push(msg)
    taken.add(messageKey(msg))
  }
  return collapseUserDupes(out)
}

/**
 * 对话页卸载后 React 状态就没了。模块级缓存让切走再回来先画出上一屏，
 * 再跟服务端投影对账，避免失败气泡闪一下消失。
 */
const transcriptCache = new Map<string, ChatMessage[]>()

function rememberTranscript(sessionId: string | number | null, messages: ChatMessage[]) {
  /** sessionId: 当前会话。messages: 去掉思考占位后的对话。 */
  if (sessionId == null) return
  transcriptCache.set(String(sessionId), messages)
}

function restoreTranscript(sessionId: string | number | null, welcome: ChatMessage): ChatMessage[] {
  /** sessionId: 要恢复的会话。welcome: 没有缓存时的开场白。 */
  if (sessionId == null) return [welcome]
  const cached = transcriptCache.get(String(sessionId))
  return cached && cached.length ? cached : [welcome]
}

function dropTranscript(sessionId: string | number) {
  /** sessionId: 已删除的会话，缓存必须一起丢掉。 */
  transcriptCache.delete(String(sessionId))
}

/**
 * 正在进行的一轮对话。思考气泡是请求态，完成后才写入聊天记录。
 *
 * 切侧栏：请求继续跑，状态挂在这个模块上，卸载界面不掐流。
 * 刷新整页：浏览器连接没了，改从会话事件重连（和 ChatGPT / Claude 一样，
 * 生成跟连接脱钩；进度在服务端，不靠 Redis 保存这根 SSE）。
 */
type LiveSource = 'local' | 'server'

type LiveTurn = {
  sessionId: string | number | null
  loading: boolean
  status: string
  source: LiveSource
}

const LIVE_STORE_KEY = 'assistant-live-turn'

const liveTurn: LiveTurn = {
  sessionId: null,
  loading: false,
  status: '',
  source: 'local',
}

const assistantUiListeners = new Set<() => void>()

function emitAssistantUi() {
  assistantUiListeners.forEach((fn) => fn())
}

function persistLiveHint() {
  /** 刷新后第一帧就能画出思考气泡，等 GET 会话投影对账。 */
  try {
    if (!liveTurn.loading || liveTurn.sessionId == null) {
      sessionStorage.removeItem(LIVE_STORE_KEY)
      return
    }
    sessionStorage.setItem(
      LIVE_STORE_KEY,
      JSON.stringify({ sessionId: String(liveTurn.sessionId), status: liveTurn.status }),
    )
  } catch {
    /* 隐私模式写不进去 */
  }
}

function readLiveHint(): { sessionId: string; status: string } | null {
  try {
    const raw = sessionStorage.getItem(LIVE_STORE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { sessionId?: string; status?: string }
    if (!parsed?.sessionId) return null
    return { sessionId: String(parsed.sessionId), status: parsed.status || t('assistant.thinking') }
  } catch {
    return null
  }
}

function restoreLiveTurnFromHint() {
  const stored = readStoredSessionId()
  const hint = readLiveHint()
  if (!hint || stored == null || hint.sessionId !== String(stored)) return
  liveTurn.sessionId = stored
  liveTurn.loading = true
  liveTurn.status = hint.status
  liveTurn.source = 'server'
}

restoreLiveTurnFromHint()

function startLiveTurn(sessionId: string | number | null, status = t('assistant.thinking'), source: LiveSource = 'local') {
  liveTurn.sessionId = sessionId
  liveTurn.loading = true
  liveTurn.status = status
  liveTurn.source = source
  persistLiveHint()
  emitAssistantUi()
}

function setLiveStatus(status: string) {
  if (!liveTurn.loading) return
  liveTurn.status = status
  persistLiveHint()
  emitAssistantUi()
}

function endLiveTurn() {
  liveTurn.loading = false
  liveTurn.status = ''
  liveTurn.source = 'local'
  persistLiveHint()
  emitAssistantUi()
}

function liveTurnOwns(sessionId: string | number | null): boolean {
  return liveTurn.loading && sessionId != null && String(liveTurn.sessionId) === String(sessionId)
}

function adoptServerLive(sessionId: string | number | null, live: LiveSnapshot | null | undefined) {
  /** 本页 SSE 还在跑时以本地流为准；刷新或断线后改跟会话投影。 */
  if (sessionId == null) return
  if (liveTurn.loading && liveTurn.source === 'local' && liveTurnOwns(sessionId)) return
  if (live?.running) {
    const text = live.status || t('assistant.thinking')
    if (liveTurnOwns(sessionId) && liveTurn.source === 'server') {
      setLiveStatus(text)
      return
    }
    startLiveTurn(sessionId, text, 'server')
    return
  }
  if (liveTurn.source === 'server' && liveTurnOwns(sessionId)) {
    endLiveTurn()
  }
}

/**
 * 先写模块缓存再通知界面。组件卸载时 React setState 可能不跑 updater，
 * 缓存必须在 setState 之外落定，回来才能看到这一轮的用户句和回复。
 */
function applyTranscript(
  sessionId: string | number | null,
  updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[]),
  welcome: ChatMessage,
): ChatMessage[] {
  const prev = restoreTranscript(sessionId, welcome)
  const next = typeof updater === 'function' ? updater(prev) : updater
  rememberTranscript(sessionId, next.filter((item) => !isThinking(item)))
  emitAssistantUi()
  return next
}

/** 对话框左下角的模型切换。清单只来自模型管理里启用且配好凭证的条目。 */
function ModelPicker({
  models,
  value,
  disabled,
  onChange,
  empty,
}: {
  models: AssistantModel[]
  value?: number
  disabled?: boolean
  onChange: (id: number) => void
  empty: string
}) {
  const t = useT()
  const navigate = useNavigate()
  const isAdmin = useAuthStore((s) => s.user?.is_admin)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const current = models.find((m) => m.id === value)
  const needle = query.trim().toLowerCase()
  const filtered = needle
    ? models.filter((m) => `${m.name} ${m.model_id} ${m.provider_name}`.toLowerCase().includes(needle))
    : models

  if (!models.length) {
    return (
      <Tooltip title={t('assistant.enableModelsHint')}>
        <button type="button" className="model-picker-trigger" disabled>
          <RobotOutlined />
          <span>{empty}</span>
        </button>
      </Tooltip>
    )
  }

  return (
    <Dropdown
      trigger={['click']}
      open={open}
      disabled={disabled}
      placement="topLeft"
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery('')
      }}
      dropdownRender={() => (
        <div className="model-picker-panel">
          <Input
            allowClear
            autoFocus
            prefix={<SearchOutlined />}
            placeholder={t('assistant.searchModel')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="model-picker-list">
            {filtered.length ? (
              filtered.map((m) => {
                const active = m.id === value
                return (
                  <button
                    type="button"
                    key={m.id}
                    className={`model-picker-item${active ? ' is-active' : ''}`}
                    onClick={() => {
                      onChange(m.id)
                      setOpen(false)
                    }}
                  >
                    <div className="model-picker-item-main">
                      <div className="model-picker-item-name">
                        <span>{m.name || m.model_id}</span>
                        {m.is_default && <Tag color="blue">{t('assistant.default')}</Tag>}
                        {m.supports_vision && <Tag color="purple">{t('assistant.vision')}</Tag>}
                      </div>
                      <div className="model-picker-item-meta">{modelHint(m)}</div>
                    </div>
                    {active && <CheckOutlined className="model-picker-check" />}
                  </button>
                )
              })
            ) : (
              <div className="model-picker-empty">{t('assistant.noMatchModel')}</div>
            )}
          </div>
          {isAdmin && (
            <button
              type="button"
              className="model-picker-footer"
              onClick={() => {
                setOpen(false)
                navigate('/models')
              }}
            >
              {t('assistant.manageModels')}
            </button>
          )}
        </div>
      )}
    >
      <button type="button" className="model-picker-trigger" disabled={disabled}>
        <RobotOutlined />
        <span>{current?.name || current?.model_id || t('assistant.pickModel')}</span>
        {current?.supports_vision && <span className="model-picker-cap">{t('assistant.vision')}</span>}
        <DownOutlined className="model-picker-caret" />
      </button>
    </Dropdown>
  )
}

function summarize(value: unknown, limit = 160): string {
  if (value === undefined || value === null) return ''
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > limit ? `${text.slice(0, limit)}…` : text
}

/** 完整轨迹抽屉：原样展开 JSON，不再截成摘要。 */
function formatFullJson(value: unknown): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/** 从技能轨迹里取出失败原因。折叠详情时也要能在气泡里直接看到。 */
function traceError(result: unknown): string {
  if (!result || typeof result !== 'object') return ''
  const row = result as { error?: unknown; ok?: boolean; message?: unknown }
  if (typeof row.error === 'string' && row.error.trim()) return row.error.trim()
  if (row.ok === false && typeof row.message === 'string' && row.message.trim()) {
    return row.message.trim()
  }
  return ''
}

/** 展示助手这一轮读了什么、做了什么。失败必须露在折叠外面。 */
function TracePanel({ traces }: { traces: SkillTrace[] }) {
  const t = useT()
  const failures = traces
    .map((row) => ({ name: row.name, error: traceError(row.result) }))
    .filter((row) => row.error)
  const [open, setOpen] = useState(failures.length > 0)

  return (
    <div style={{ marginTop: 10, borderTop: '1px dashed #d9d9d9', paddingTop: 8 }}>
      {failures.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={failures.length === 1 ? t('assistant.skillFailed', { name: failures[0].name }) : t('assistant.skillsFailed', { n: failures.length })}
          description={
            <div>
              {failures.map((f) => (
                <div key={f.name}>
                  <code>{f.name}</code>{t('assistant.colon')}{f.error}
                </div>
              ))}
            </div>
          }
        />
      )}
      <a style={{ fontSize: 12, color: failures.length ? '#cf1322' : '#888' }} onClick={() => setOpen(!open)}>
        {open ? <DownOutlined /> : <RightOutlined />} {t('assistant.calledSkills', { n: traces.length })}
        {failures.length > 0 ? t('assistant.failedCount', { n: failures.length }) : ''}
      </a>
      {open && (
        <div style={{ marginTop: 8 }}>
          {traces.map((row, i) => {
            const error = traceError(row.result)
            return (
              <div key={row.id || i} style={{ fontSize: 12, marginBottom: 6, lineHeight: 1.6 }}>
                <Space size={4}>
                  {error ? <CloseCircleOutlined style={{ color: '#ff4d4f' }} /> : <CheckCircleOutlined style={{ color: '#52c41a' }} />}
                  <code>{row.name}</code>
                </Space>
                {Object.keys(row.arguments || {}).length > 0 && (
                  <div style={{ color: '#888', paddingLeft: 20 }}>{t('assistant.argsLabel', { text: summarize(row.arguments) })}</div>
                )}
                <div style={{ color: error ? '#ff4d4f' : '#888', paddingLeft: 20 }}>
                  {error ? t('assistant.failLabel', { text: error }) : t('assistant.resultLabel', { text: summarize(row.result) })}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** 抽屉里的技能轨迹：参数和结果全文展开，气泡里那份 160 字摘要只给扫一眼。 */
function TraceDetailList({ traces }: { traces: SkillTrace[] }) {
  const t = useT()
  if (!traces.length) return null
  return (
    <>
      {traces.map((row, i) => {
        const error = traceError(row.result)
        const args = row.arguments && Object.keys(row.arguments).length > 0 ? formatFullJson(row.arguments) : ''
        const body = error || formatFullJson(row.result)
        return (
          <Card
            key={row.id || i}
            size="small"
            title={
              <Space>
                {error ? (
                  <CloseCircleOutlined style={{ color: '#ff4d4f' }} />
                ) : (
                  <CheckCircleOutlined style={{ color: '#52c41a' }} />
                )}
                <code>{row.name}</code>
              </Space>
            }
            style={{ marginBottom: 10 }}
          >
            {args ? (
              <div style={{ marginBottom: 8 }}>
                <div style={{ color: '#888', marginBottom: 4 }}>{t('assistant.args')}</div>
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0 }}>{args}</pre>
              </div>
            ) : null}
            <div style={{ color: error ? '#ff4d4f' : '#888', marginBottom: 4 }}>{error ? t('status.failed') : t('assistant.result')}</div>
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0, maxHeight: 480, overflow: 'auto' }}>
              {body || t('assistant.none')}
            </pre>
          </Card>
        )
      })}
    </>
  )
}

/** 会话附图短链要带登录态。不走 axios：拦截器按 JSON 信封读 blob，图会加载失败。 */
function loadSessionImage(url: string): Promise<string> {
  const path = url.startsWith('/api/v1/') ? url : `/api/v1${url.startsWith('/') ? url : `/${url}`}`
  const token = useAuthStore.getState().token
  return fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).then(async (resp) => {
    if (!resp.ok) throw new Error(`image ${resp.status}`)
    const blob = await resp.blob()
    const kind = (blob.type || '').toLowerCase()
    if (kind && !kind.startsWith('image/') && kind !== 'application/octet-stream') {
      throw new Error('not an image')
    }
    return URL.createObjectURL(blob)
  })
}

/**
 * 气泡里的一张图。
 * data URL 和 http(s) 第一帧就能画；会话短链拉成 blob 再画。
 */
function ChatBubbleImage({ url, alt }: { url: string; alt: string }) {
  const direct = url.startsWith('data:') || url.startsWith('http://') || url.startsWith('https://')
  const [src, setSrc] = useState(direct ? url : '')

  useEffect(() => {
    if (url.startsWith('data:') || url.startsWith('http://') || url.startsWith('https://')) {
      setSrc(url)
      return
    }
    let objectUrl = ''
    let cancelled = false
    loadSessionImage(url)
      .then((created) => {
        if (cancelled) {
          URL.revokeObjectURL(created)
          return
        }
        objectUrl = created
        setSrc(created)
      })
      .catch(() => {
        if (!cancelled) setSrc('')
      })
    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [url])

  if (!src) return null
  return <img src={src} alt={alt} className="assistant-bubble-image" />
}

export default function AssistantPage() {
  const t = useT()
  const user = useAuthStore((s) => s.user)
  const welcome: ChatMessage = {
    role: 'assistant',
    content: user?.display_name
      ? t('assistant.welcomeNamed', { name: user.display_name })
      : t('assistant.welcome'),
  }
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    const name = useAuthStore.getState().user?.display_name
    const boot: ChatMessage = {
      role: 'assistant',
      content: name ? t('assistant.welcomeNamed', { name }) : t('assistant.welcome'),
    }
    return restoreTranscript(readStoredSessionId(), boot)
  })
  const [input, setInput] = useState('')
  const storedId = readStoredSessionId()
  const [loading, setLoading] = useState(() => liveTurnOwns(storedId))
  const [status, setStatus] = useState(() => (liveTurnOwns(storedId) ? liveTurn.status : ''))
  const [watching, setWatching] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [sessionId, setSessionId] = useState<string | number | null>(readStoredSessionId)
  const bootstrapped = useRef(false)
  const [mode, setMode] = useState<AssistantMode>('standard')
  const [catalogOpen, setCatalogOpen] = useState(false)
  /** 窄屏会话列表改抽屉，避免 240px 侧栏把聊天区挤没。 */
  const isMobile = useIsMobile()
  const [sessionDrawer, setSessionDrawer] = useState(false)
  /** 窄屏快捷指令默认收起；点开才铺芯片，避免挡住对话。 */
  const [chipOpen, setChipOpen] = useState(false)
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceMessage, setTraceMessage] = useState<ChatMessage | null>(null)
  const [sessionSearch, setSessionSearch] = useState('')
  const [searchedSessions, setSearchedSessions] = useState<AssistantSession[] | null>(null)
  const [selectedSkills, setSelectedSkills] = useState<string[]>([])
  const [selectedTools, setSelectedTools] = useState<string[]>([])
  const [compatibilityMode, setCompatibilityMode] = useState(false)
  const [modelId, setModelId] = useState<number | null>(readStoredModelId)
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([])
  const [sessionLoading, setSessionLoading] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)
  const lastIdRef = useRef(0)
  const lastLiveRef = useRef(false)
  const actingRef = useRef(false)
  const sessionIdRef = useRef<string | number | null>(sessionId)
  const welcomeRef = useRef(welcome)
  const loadGen = useRef(0)
  sessionIdRef.current = sessionId
  welcomeRef.current = welcome

  /** 改对话列表时先写入模块缓存。组件卸载后 SSE 仍能把回复写进去。
   *  updater: 新列表或基于上一屏的更新函数。
   *  forSession: 写入哪一个会话的缓存，切会话时必须显式传入，避免写到上一会话。
   */
  const setTranscript = (
    updater: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[]),
    forSession: string | number | null = sessionIdRef.current,
  ) => {
    const next = applyTranscript(forSession, updater, welcomeRef.current)
    if (String(sessionIdRef.current) === String(forSession)) setMessages(next)
  }

  useEffect(() => {
    const sync = () => {
      const sid = sessionIdRef.current
      setMessages(restoreTranscript(sid, welcomeRef.current))
      const mine = liveTurnOwns(sid)
      setLoading(mine)
      setStatus(mine ? liveTurn.status : '')
    }
    assistantUiListeners.add(sync)
    sync()
    return () => {
      assistantUiListeners.delete(sync)
    }
  }, [])

  const sessionsQuery = useQuery({
    queryKey: ['ai-sessions'],
    queryFn: async () => {
      try {
        const data = await get<AssistantSession[] | { items: AssistantSession[] }>('/ai/sessions')
        const items = Array.isArray(data) ? data : data.items || []
        setCompatibilityMode(false)
        return items
      } catch {
        setCompatibilityMode(true)
        return [{ id: 'legacy', title: t('assistant.defaultSession'), mode: 'standard', status: 'active' }] as AssistantSession[]
      }
    },
    retry: false,
  })

  // 只列模型管理里启用且配好凭证的模型；这个接口不返回任何 Key
  const modelsQuery = useQuery({
    queryKey: ['assistant-models'],
    queryFn: () => get<AssistantModel[]>('/ai/models'),
    retry: false,
  })
  const models = modelsQuery.data || []
  const activeModel = models.find((m) => m.id === modelId) || models.find((m) => m.is_default) || models[0]
  const supportsVision = !!activeModel?.supports_vision

  useEffect(() => {
    if (!models.length || modelId == null) return
    if (!models.some((m) => m.id === modelId)) setModelId(null)
  }, [models, modelId])

  const catalogQuery = useQuery({
    queryKey: ['assistant-catalog'],
    queryFn: async () => {
      try {
        const data = await get<HarnessComponent[]>('/harness/components')
        return data
          .filter((item) => item.kind === 'agent-skill' || item.kind === 'agent-tool')
          .map((item) => ({
            name: item.name,
            display_name: item.manifest.display_name,
            description: item.manifest.description,
            kind: item.kind,
            enabled: item.enabled,
            schema: item.manifest.config_schema,
          } satisfies AssistantCatalogItem))
      } catch {
        const legacySkills = await get<Skill[]>('/ai/skills')
        return legacySkills.map((item) => ({
          name: item.name,
          display_name: item.name,
          description: item.description,
          kind: 'agent-skill',
          enabled: true,
        } satisfies AssistantCatalogItem))
      }
    },
    retry: false,
  })

  const { data: skills = [] } = useQuery({
    queryKey: ['ai-skills'],
    queryFn: () => get<Skill[]>('/ai/skills'),
  })

  const { data: attachments = [], refetch: refetchAttachments } = useQuery({
    queryKey: ['ai-attachments'],
    queryFn: () => get<Attachment[]>('/ai/attachments'),
  })

  const uploadFiles = async (files: { file: File; path: string }[]) => {
    if (!files.length) return
    setUploading(true)
    try {
      const form = new FormData()
      for (const f of files) {
        form.append('files', f.file)
      }
      // 相对路径单独用换行传：FormData 里文件名不带目录，层级会在这一步丢掉
      form.append('rel_paths', files.map((f) => f.path).join('\n'))
      await postForm('/ai/attachments', form)
      await refetchAttachments()
      message.success(t('assistant.uploadedN', { n: files.length }))
    } finally {
      setUploading(false)
    }
  }

  const removeAttachment = async (id: number) => {
    await del(`/ai/attachments/${id}`)
    refetchAttachments()
  }

  /** 图片读成 data url 随消息发给模型看，不落盘、不下发到节点。 */
  const attachImages = async (files: File[]) => {
    const loaded = await Promise.all(
      files.map(
        (file) =>
          new Promise<PendingImage>((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve({ name: file.name, url: String(reader.result || '') })
            reader.onerror = () => reject(reader.error)
            reader.readAsDataURL(file)
          })
      )
    )
    setPendingImages((prev) => [...prev, ...loaded].slice(0, 8))
  }

  /** 图片给模型看，其余文件走节点下发。当前模型不认图就全部按文件处理。 */
  const routeFiles = async (picked: { file: File; path: string }[]) => {
    const images = supportsVision ? picked.filter((f) => f.file.type.startsWith('image/')) : []
    const rest = picked.filter((f) => !images.includes(f))
    if (images.length) await attachImages(images.map((f) => f.file))
    if (rest.length) await uploadFiles(rest)
  }

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    if (uploading) return
    const picked = await collectFiles(e.dataTransfer.items)
    await routeFiles(picked)
  }

  const onPaste = async (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files || [])
    if (!files.length) return
    e.preventDefault()
    await routeFiles(files.map((f) => ({ file: f, path: f.name })))
  }

  const chooseModel = async (id: number) => {
    const next = models.find((m) => m.id === id)
    if (!next) return
    const leavingVision = supportsVision && !next.supports_vision && pendingImages.length > 0
    setModelId(id)
    writeStoredModelId(id)
    if (leavingVision) {
      const files = await Promise.all(
        pendingImages.map(async (img) => {
          const res = await fetch(img.url)
          const blob = await res.blob()
          return new File([blob], img.name || 'image.png', { type: blob.type || 'image/png' })
        })
      )
      setPendingImages([])
      await uploadFiles(files.map((file) => ({ file, path: file.name })))
      message.info(t('assistant.noVisionFallback'))
    }
    if (!compatibilityMode && sessionId != null && sessionId !== 'legacy') {
      try {
        await patch(`/ai/sessions/${sessionId}`, { model_id: id })
        sessionsQuery.refetch()
      } catch {
        /* 下次发消息还会带上 model_id */
      }
    }
  }

  /**
   * 推倒重来。
   *
   * 脏上下文会自我延续：模型把上一轮的错误结论当既成事实接着往下推，
   * 越聊越偏，光靠再解释一遍常常拉不回来。
   */
  const clearConversation = async () => {
    setClearing(true)
    try {
      if (compatibilityMode || sessionId == null || sessionId === 'legacy') {
        await del('/ai/conversation')
      } else {
        await post(`/ai/sessions/${sessionId}/clear`, {})
      }
      lastIdRef.current = 0
      setTranscript([welcome])
      endLiveTurn()
      setWatching(false)
      sessionsQuery.refetch()
      message.success(t('assistant.cleared'))
    } catch {
      message.error(t('assistant.clearFail'))
    } finally {
      setClearing(false)
    }
  }

  const pull = async (afterId: number, opts?: { replace?: boolean }): Promise<ChatMessage[]> => {
    const currentSession = sessionIdRef.current
    if (currentSession == null) return []
    const data = compatibilityMode || currentSession === 'legacy'
      ? await get<Conversation>('/ai/conversation', { after_id: afterId }, {
          timeout: opts?.replace ? 30000 : 15000,
          skipErrorToast: true,
        })
      : await get<Conversation>(`/ai/sessions/${currentSession}`, { after_id: afterId }, {
          timeout: opts?.replace ? 30000 : 15000,
          skipErrorToast: true,
        })
    if (sessionIdRef.current !== currentSession) return []
    setWatching(!!data.watching)
    lastLiveRef.current = !!data.live?.running
    adoptServerLive(currentSession, data.live)
    const incoming = data.messages || []
    if (opts?.replace || afterId === 0) {
      lastIdRef.current = incoming.reduce((max, item) => Math.max(max, item.id || 0), 0)
      setTranscript((prev) => withLocalEchoes(prev, incoming, welcome), currentSession)
      return incoming
    }
    if (incoming.length) {
      lastIdRef.current = Math.max(lastIdRef.current, ...incoming.map((m) => m.id || 0))
      setTranscript((prev) => mergeById(prev, incoming), currentSession)
    }
    return incoming
  }

  useEffect(() => {
    if (compatibilityMode) {
      if (sessionId !== 'legacy') setSessionId('legacy')
      return
    }
    const items = sessionsQuery.data
    if (items == null) return
    if (sessionId != null && sessionId !== 'legacy' && items.some((row) => String(row.id) === String(sessionId))) {
      return
    }
    const stored = readStoredSessionId()
    const hit = stored ? items.find((row) => String(row.id) === stored) : undefined
    const next = hit || items[0]
    if (next) {
      setSessionId(next.id)
      writeStoredSessionId(next.id)
      setMode(next.mode || 'standard')
      if (typeof next.model_id === 'number') {
        setModelId(next.model_id)
        writeStoredModelId(next.model_id)
      }
    }
  }, [sessionsQuery.data, compatibilityMode])

  useEffect(() => {
    const gen = ++loadGen.current
    if (sessionId == null) {
      setSessionLoading(false)
      return
    }
    setTranscript(restoreTranscript(sessionId, welcome), sessionId)
    const timer = window.setTimeout(() => {
      if (loadGen.current === gen) setSessionLoading(true)
    }, 160)
    pull(0, { replace: true })
      .catch(() => undefined)
      .finally(() => {
        window.clearTimeout(timer)
        if (loadGen.current === gen) setSessionLoading(false)
      })
  }, [sessionId, compatibilityMode])

  useEffect(() => {
    if (sessionId == null) return
    const pollTimer = window.setInterval(() => {
      pull(lastIdRef.current).catch(() => undefined)
    }, loading || watching ? 1500 : 8000)
    return () => window.clearInterval(pollTimer)
  }, [watching, loading, sessionId, compatibilityMode])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages, loading])

  /**
   * 发一条用户话：本地立刻留下用户气泡，思考态画在列表外，避免夹在回显和落库中间画出两条。
   * HTTP/SSE 失败写进对话框；连接中断先补救轮询，确认没有助手回复再报失败。
   */
  const sendText = async (raw: string) => {
    const text = raw.trim()
    if (!text || liveTurn.loading) return
    setChipOpen(false)
    const turnSession = sessionIdRef.current
    const images = pendingImages
    setPendingImages([])
    setInput('')
    startLiveTurn(turnSession)
    const imageUrls = images.map((img) => img.url).filter(Boolean)
    const echo: ChatMessage = {
      role: 'user',
      content: text,
      ...(imageUrls.length ? { images: imageUrls } : {}),
    }
    setTranscript((prev) => collapseUserDupes([...prev, echo]), turnSession)
    let answered = false

    const keepUser = (prev: ChatMessage[]) => {
      const rest = prev.filter((m) => m !== echo && !isThinking(m))
      const hasUser = rest.some((m) => m.role === 'user' && userTextKey(m.content) === text)
      return hasUser ? rest : [...rest, echo]
    }

    /** 落库副本回来之后再丢掉没有 id 的回显，避免列表被掏空。 */
    const dropEchoIfPersisted = () => {
      setTranscript((prev) => {
        const persistedIdx = prev.findIndex(
          (m) => m !== echo && m.role === 'user' && userTextKey(m.content) === text && m.id,
        )
        if (persistedIdx < 0) return prev
        const next = prev.map((m, i) =>
          i === persistedIdx ? { ...m, images: pickImages(echo.images, m.images) } : m,
        )
        return collapseUserDupes(next.filter((m) => m !== echo))
      }, turnSession)
    }

    /** toast + 对话框红气泡。已画过答案或失败时不再覆盖。 */
    const failInChat = (reason: string) => {
      if (answered) return
      answered = true
      message.error(reason)
      setTranscript(
        (prev) =>
          collapseUserDupes([
            ...keepUser(prev),
            { role: 'assistant', content: reason, kind: 'error', failed: true },
          ]),
        turnSession,
      )
    }

    try {
      const auth = useAuthStore.getState().token
      const resp = await fetch('/api/v1/ai/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(auth ? { Authorization: `Bearer ${auth}` } : {}),
        },
        body: JSON.stringify({
          message: text,
          mode,
          session_id: turnSession == null || turnSession === 'legacy' ? undefined : turnSession,
          skills: selectedSkills,
          tools: selectedTools,
          model_id: activeModel?.id,
          images: images.map((img) => img.url),
        }),
      })
      if (!resp.ok || !resp.body) {
        let detail = resp.status ? t('assistant.noResponseHttp', { n: resp.status }) : t('assistant.noResponse')
        try {
          const body = (await resp.json()) as { message?: string; detail?: string }
          if (body?.message?.trim()) detail = body.message
          else if (typeof body?.detail === 'string' && body.detail.trim()) detail = body.detail
        } catch {
          /* 网关超时常常是 HTML */
        }
        failInChat(detail)
      } else {
        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop() || ''
          for (const part of parts) {
            const line = part.trim()
            if (!line.startsWith('data:')) continue
            const rawEvt = line.slice(5).trim()
            if (!rawEvt || rawEvt === '[DONE]') continue
            let evt: StreamEvent
            try {
              evt = JSON.parse(rawEvt) as StreamEvent
            } catch {
              continue
            }
            if (evt.type === 'status') setLiveStatus(evt.text || t('assistant.thinking'))
            else if (evt.type === 'reasoning') setLiveStatus(evt.text || evt.content || t('assistant.reasoning'))
            else if (evt.type === 'text') setLiveStatus(t('assistant.generating'))
            else if (evt.type === 'tool') {
              setLiveStatus(evt.phase === 'start' ? t('assistant.calling', { name: evt.name || '' }) : t('assistant.called', { name: evt.name || '' }))
            } else if (evt.type === 'done') {
              if (evt.reply || evt.actions?.length || evt.traces?.length) {
                answered = true
                if (evt.watching) setWatching(true)
                setTranscript(
                  (prev) =>
                    collapseUserDupes([
                      ...keepUser(prev),
                      {
                        role: 'assistant',
                        content: evt.reply || '',
                        actions: evt.actions || [],
                        traces: evt.traces || [],
                      },
                    ]),
                  turnSession,
                )
              }
            } else if (evt.type === 'error') {
              failInChat(evt.message || t('assistant.processFail'))
            }
          }
        }
      }
    } catch (err) {
      const reason =
        err instanceof Error && err.message && !/failed to fetch/i.test(err.message)
          ? err.message
          : t('assistant.connLost')
      message.warning(t('assistant.retrying', { reason }))
      setLiveStatus(t('assistant.reconnecting'))
    } finally {
      const before = lastIdRef.current
      let recovered = false
      try {
        const first = await pull(before)
        recovered = first.some((m) => m.role === 'assistant')
        if (!answered && !recovered && !lastLiveRef.current) {
          recovered = await retryPull(before)
        }
      } catch {
        /* 下面统一报没有回答 */
      }
      const stillLive = !answered && !recovered && lastLiveRef.current
      if (!answered && !recovered && !stillLive) {
        failInChat(t('assistant.noAnswer'))
      }
      setTranscript((prev) => {
        let next = prev.filter((m) => !isThinking(m))
        if (recovered) next = next.filter((m) => !(m.failed && !m.id))
        return collapseUserDupes(next)
      }, turnSession)
      dropEchoIfPersisted()
      if (stillLive) {
        startLiveTurn(turnSession, liveTurn.status || t('assistant.thinking'), 'server')
      } else {
        endLiveTurn()
      }
    }
  }

  /** 流中断后的补救轮询：短间隔追几次，拿到助手回复就停。 */
  const retryPull = async (before: number): Promise<boolean> => {
    for (const wait of [1500, 2500, 4000, 6000, 8000, 10000]) {
      await new Promise((resolve) => window.setTimeout(resolve, wait))
      const incoming = await pull(lastIdRef.current).catch(() => [] as ChatMessage[])
      if (incoming.some((m) => m.role === 'assistant')) return true
    }
    return false
  }

  const selectSession = (next: AssistantSession) => {
    if (String(next.id) === String(sessionId)) return
    lastIdRef.current = 0
    sessionIdRef.current = next.id
    setSessionId(next.id)
    writeStoredSessionId(next.id)
    setTranscript(restoreTranscript(next.id, welcome), next.id)
    setMode(next.mode || 'standard')
    // 会话记着自己上次用的模型，切回来不用再挑一遍
    setModelId(typeof next.model_id === 'number' ? next.model_id : null)
    if (typeof next.model_id === 'number') writeStoredModelId(next.model_id)
    setPendingImages([])
  }

  const deleteSession = async (item: AssistantSession) => {
    if (compatibilityMode) {
      message.info(t('assistant.singleNoDelete'))
      return
    }
    try {
      await del(`/ai/sessions/${item.id}`)
      lastIdRef.current = 0
      dropTranscript(item.id)
      if (liveTurnOwns(item.id)) endLiveTurn()
      if (String(item.id) === String(sessionId)) {
        setMessages([welcome])
        setSessionId(null)
        try {
          localStorage.removeItem(SESSION_STORE_KEY)
        } catch {
          /* ignore */
        }
      }
      const remaining = (sessionsQuery.data || []).filter((row) => String(row.id) !== String(item.id))
      await sessionsQuery.refetch()
      if (String(item.id) === String(sessionId)) {
        if (remaining.length) {
          selectSession(remaining[0])
        } else {
          await createSession()
        }
      }
      message.success(t('assistant.sessionDeleted'))
    } catch {
      message.error(t('assistant.deleteSessionFail'))
    }
  }

  const createSession = async () => {
    if (compatibilityMode) {
      message.info(t('assistant.singleNoCreate'))
      return
    }
    try {
      const created = await post<AssistantSession>('/ai/sessions', {
        mode,
        title: t('assistant.newSession'),
        model_id: activeModel?.id,
      })
      await sessionsQuery.refetch()
      selectSession(created)
    } catch {
      message.error(t('assistant.createSessionFail'))
    }
  }

  useEffect(() => {
    if (compatibilityMode || sessionsQuery.data == null) return
    if (sessionsQuery.data.length === 0 && !bootstrapped.current) {
      bootstrapped.current = true
      createSession()
    }
  }, [sessionsQuery.data, compatibilityMode])

  const searchSessions = async () => {
    const query = sessionSearch.trim()
    if (!query || compatibilityMode) {
      setSearchedSessions(null)
      return
    }
    try {
      const result = await get<AssistantSession[] | { items: AssistantSession[] }>('/ai/sessions/search', { q: query })
      setSearchedSessions(Array.isArray(result) ? result : result.items || [])
    } catch {
      setSearchedSessions(null)
      message.info(t('assistant.searchLocal'))
    }
  }

  const runSessionAction = async (action: 'inspect' | 'resume' | 'fork' | 'replay') => {
    if (compatibilityMode || sessionId == null || sessionId === 'legacy') {
      message.info(t('assistant.needMultiApi'))
      return
    }
    try {
      if (action === 'inspect') {
        const detail = await get<Record<string, unknown>>(`/ai/sessions/${sessionId}/inspect`)
        setTraceMessage({
          role: 'assistant',
          content: t('assistant.inspectResult'),
          events: [{ type: 'status', content: JSON.stringify(detail, null, 2) }],
        })
        setTraceOpen(true)
      } else {
        const result = await post<AssistantSession | { session_id?: string | number }>(
          `/ai/sessions/${sessionId}/${action}`,
          { mode },
        )
        message.success({ resume: t('assistant.resumed'), fork: t('assistant.forked'), replay: t('assistant.replayed') }[action])
        await sessionsQuery.refetch()
        if (action === 'fork') {
          const id = 'id' in result ? result.id : result.session_id
          if (id) selectSession({ id, title: t('assistant.forkSession'), mode })
        }
        pull(0).catch(() => undefined)
      }
    } catch {
      message.error(t('assistant.sessionActionFail'))
    }
  }

  const handleAction = async (action: ActionCard) => {
    if (!action.token) {
      message.error(t('assistant.invalidCard'))
      return
    }
    if (actingRef.current) return
    actingRef.current = true
    try {
      const cardSession = action.session_id
      const out = await post<{ watching: boolean; messages: ChatMessage[] }>('/ai/act', {
        type: action.type,
        payload: action.payload,
        token: action.token,
        session_id: cardSession ?? (sessionId == null || sessionId === 'legacy' ? undefined : sessionId),
      })
      setWatching(!!out.watching)
      refetchAttachments()
      message.success(out.watching ? t('assistant.submittedWatch') : t('assistant.handled'))
      // act 返回的是 AiMessage.id，会话列表用 event_seq。用错 id 会让轮询再也拉不到跟进。
      await pull(0, { replace: true })
    } catch {
      // 拦截器已提示
    } finally {
      actingRef.current = false
    }
  }

  const chips = skills.filter((s) => (s.examples || []).length > 0).slice(0, 12)

  /** 会话列表：宽屏放左侧，窄屏放抽屉，避免 240px 侧栏把聊天挤没。 */
  const sessionPane = (
        <div className="assistant-sessions">
          <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
            <Input
              allowClear
              prefix={<SearchOutlined />}
              placeholder={t('assistant.searchSession')}
              value={sessionSearch}
              onChange={(event) => {
                setSessionSearch(event.target.value)
                if (!event.target.value) setSearchedSessions(null)
              }}
              onPressEnter={searchSessions}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={createSession} />
          </Space.Compact>
          {compatibilityMode && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 8 }}
              message={t('assistant.legacyApi')}
              description={t('assistant.legacyApiHint')}
            />
          )}
          <List
            size="small"
            style={{ flex: 1, overflow: 'auto' }}
            loading={sessionsQuery.isLoading}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('assistant.noSession')} /> }}
            dataSource={(searchedSessions || sessionsQuery.data || []).filter((item) =>
              item.title.toLowerCase().includes(sessionSearch.trim().toLowerCase())
            )}
            renderItem={(item) => (
              <List.Item
                onClick={() => {
                  selectSession(item)
                  setSessionDrawer(false)
                }}
                style={{
                  cursor: 'pointer',
                  padding: '10px 8px',
                  borderRadius: 6,
                  background: String(item.id) === String(sessionId) ? '#e6f4ff' : undefined,
                }}
                extra={
                  <span onClick={(event) => event.stopPropagation()}>
                    <Popconfirm
                      title={t('assistant.deleteSession')}
                      description={t('assistant.deleteSessionDesc')}
                      okText={t('common.delete')}
                      cancelText={t('common.cancel')}
                      okButtonProps={{ danger: true }}
                      onConfirm={() => deleteSession(item)}
                    >
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                      />
                    </Popconfirm>
                  </span>
                }
              >
                <List.Item.Meta
                  title={<span style={{ fontSize: 13 }}>{item.title || t('assistant.untitled')}</span>}
                  description={
                    <Space size={4}>
                      <Tag bordered={false}>{item.mode || 'standard'}</Tag>
                      {item.status && <Badge status={item.status === 'running' ? 'processing' : 'default'} text={item.status} />}
                    </Space>
                  }
                />
              </List.Item>
            )}
          />
        </div>
  )

  return (
    <Card
      className="assistant-page"
      title={
        <Space>
          AI Agent
          {compatibilityMode && <Tag color="orange">{t('assistant.compatMode')}</Tag>}
        </Space>
      }
      extra={
        <Space wrap>
          {isMobile && (
            <Button size="small" icon={<UnorderedListOutlined />} onClick={() => setSessionDrawer(true)}>
              {t('assistant.sessions')}
            </Button>
          )}
          {watching && <Tag color="orange">{t('assistant.watching')}</Tag>}
          {!isMobile && (
          <Tag color="blue">
            {activeModel
              ? t('assistant.modelTag', { provider: activeModel.provider_name, name: activeModel.name })
              : t('assistant.skillMcpTag')}
          </Tag>
          )}
          {!isMobile && (
          <Segmented
            size="small"
            value={mode}
            onChange={(value) => setMode(value as AssistantMode)}
            options={[
              { label: 'Standard', value: 'standard' },
              { label: 'Minimal', value: 'minimal' },
              { label: 'Plan', value: 'plan' },
            ]}
          />
          )}
          <Button size="small" icon={<ToolOutlined />} onClick={() => setCatalogOpen(true)}>{t('assistant.catalog')}</Button>
          <Popconfirm
            title={t('assistant.clearTitle')}
            description={
              <div style={{ maxWidth: 300 }}>
                {t('assistant.clearDesc1')}
                <br />
                <b>{t('assistant.clearDescBold')}</b>
                {t('assistant.clearDesc2')}
                <br />
                {t('assistant.clearDesc3')}
              </div>
            }
            okText={t('assistant.clear')}
            cancelText={t('common.cancel')} 
            okButtonProps={{ danger: true }}
            onConfirm={clearConversation}
          >
            <Tooltip title={t('assistant.clearTitle')}>
              <Button size="small" icon={<ClearOutlined />} loading={clearing}>
                {t('assistant.clear')}
              </Button>
            </Tooltip>
          </Popconfirm>
          <Dropdown
            menu={{
              items: [
                { key: 'inspect', icon: <SearchOutlined />, label: t('assistant.inspect'), onClick: () => runSessionAction('inspect') },
                { key: 'resume', icon: <ReloadOutlined />, label: t('assistant.resume'), onClick: () => runSessionAction('resume') },
                { key: 'fork', icon: <BranchesOutlined />, label: t('assistant.fork'), onClick: () => runSessionAction('fork') },
                { key: 'replay', icon: <PlayCircleOutlined />, label: t('assistant.replay'), onClick: () => runSessionAction('replay') },
              ],
            }}
          >
            <Button size="small">{t('assistant.sessionActions')} <DownOutlined /></Button>
          </Dropdown>
        </Space>
      }
      style={{ height: '100%' }}
    >
      <div className="assistant-shell">
        {!isMobile && sessionPane}
      <div
        className="assistant-chat"
        onDragOver={(e) => {
          e.preventDefault()
          if (!dragging) setDragging(true)
        }}
        onDragLeave={(e) => {
          // 移到子元素上也会触发 leave，只有真正离开外框才收起提示
          if (e.currentTarget.contains(e.relatedTarget as Node)) return
          setDragging(false)
        }}
        onDrop={onDrop}
      >
        {dragging && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              zIndex: 10,
              background: 'rgba(22,119,255,0.06)',
              border: '2px dashed #1677ff',
              borderRadius: 8,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column',
              gap: 8,
              pointerEvents: 'none',
            }}
          >
            <PaperClipOutlined style={{ fontSize: 28, color: '#1677ff' }} />
            <span style={{ color: '#1677ff' }}>{t('assistant.dropHint')}</span>
          </div>
        )}
        {chips.length > 0 && (
          <div className="assistant-chips">
            {isMobile ? (
              <button
                type="button"
                className="assistant-chips-toggle"
                onClick={() => setChipOpen((v) => !v)}
              >
                {t('assistant.quickCmds')}
                <DownOutlined style={{ transform: chipOpen ? 'rotate(180deg)' : undefined, fontSize: 10 }} />
              </button>
            ) : null}
            {(!isMobile || chipOpen) && (
              <div className="assistant-chips-list">
                <Space size={[8, 8]} wrap>
                  {chips.map((s) => (
                    <Tag
                      key={s.name}
                      style={{ cursor: 'pointer' }}
                      onClick={() => sendText(s.examples[0])}
                    >
                      {categoryLabel(s.category)} · {s.examples[0]}
                    </Tag>
                  ))}
                </Space>
              </div>
            )}
          </div>
        )}
        {sessionLoading && (
          <div className="assistant-session-hint">
            <LoadingOutlined />
            {t('assistant.reading')}
          </div>
        )}
        <div ref={listRef} className="assistant-msg-list">
          {messages.map((msg, idx) => {
            const toolFailed = (msg.traces || []).some((row) => Boolean(traceError(row.result)))
            const failed = Boolean(msg.failed || msg.kind === 'error' || toolFailed)
            const thinking = isThinking(msg)
            return (
            <div
              key={msg.id || `local-${idx}`}
              style={{
                display: 'flex',
                gap: 12,
                marginBottom: 16,
                flexDirection: msg.role === 'user' ? 'row-reverse' : 'row',
              }}
            >
              <div
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: '50%',
                  background: msg.role === 'user'
                    ? '#1677ff'
                    : msg.kind === 'followup'
                      ? '#d48806'
                      : failed
                        ? '#cf1322'
                        : '#52c41a',
                  color: '#fff',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                {msg.role === 'user' ? <UserOutlined /> : thinking ? <LoadingOutlined /> : <RobotOutlined />}
              </div>
              <div
                className="assistant-bubble"
                style={{
                  background: msg.role === 'user'
                    ? '#1677ff'
                    : msg.kind === 'followup'
                      ? '#fff7e6'
                      : failed
                        ? '#fff2f0'
                        : thinking
                          ? '#f6ffed'
                          : '#f5f5f5',
                  color: msg.role === 'user' ? '#fff' : failed ? '#820014' : '#333',
                  border: failed ? '1px solid #ffccc7' : thinking ? '1px solid #b7eb8f' : undefined,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {msg.kind && msg.role === 'assistant' && !thinking && msg.kind !== 'error' && (
                  <Tag
                    bordered={false}
                    color={msg.kind === 'reasoning' ? 'gold' : msg.kind === 'tool' ? 'purple' : 'blue'}
                    style={{ marginBottom: 6 }}
                  >
                    {msg.kind === 'reasoning' ? 'Reasoning' : msg.kind === 'tool' ? 'Tool' : 'Text'}
                  </Tag>
                )}
                {msg.images && msg.images.length > 0 && (
                  <div className="assistant-bubble-images">
                    {msg.images.map((url, i) => (
                      <ChatBubbleImage key={`${i}-${url.slice(0, 48)}`} url={url} alt={t('assistant.imageAlt', { n: i + 1 })} />
                    ))}
                  </div>
                )}
                {msg.content}
                {msg.reasoning && (
                  <Card size="small" title="Reasoning" style={{ marginTop: 10, background: '#fffbe6' }}>
                    <span style={{ color: '#666' }}>{msg.reasoning}</span>
                  </Card>
                )}
                {msg.role === 'assistant' && msg.traces && msg.traces.length > 0 && (
                  <TracePanel traces={msg.traces} />
                )}
                {msg.role === 'assistant' && ((msg.events?.length || 0) > 0 || (msg.traces?.length || 0) > 0) && (
                  <Button
                    type="link"
                    size="small"
                    icon={<ApartmentOutlined />}
                    style={{ paddingLeft: 0, marginTop: 6 }}
                    onClick={() => {
                      setTraceMessage(msg)
                      setTraceOpen(true)
                    }}
                  >
                    {t('assistant.openTrace')}
                  </Button>
                )}
                {msg.actions && msg.actions.length > 0 && (
                  <Card
                    size="small"
                    title={<Space><Badge status="warning" />{t('assistant.needApprove')}</Space>}
                    style={{ marginTop: 12, background: '#fffbe6' }}
                  >
                    <Space wrap>
                      {msg.actions.map((a, i) => (
                        <Button
                          key={i}
                          type="primary"
                          size="small"
                          disabled={!a.token}
                          onClick={() => handleAction(a)}
                        >
                          {a.label}
                        </Button>
                      ))}
                    </Space>
                  </Card>
                )}
              </div>
            </div>
            )
          })}
          {loading && (
            <div
              style={{
                display: 'flex',
                gap: 12,
                marginBottom: 16,
              }}
            >
              <div
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: '50%',
                  background: '#52c41a',
                  color: '#fff',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <LoadingOutlined />
              </div>
              <div
                className="assistant-bubble"
                style={{
                  background: '#f6ffed',
                  color: '#135200',
                  border: '1px solid #b7eb8f',
                }}
              >
                {status || t('assistant.thinking')}
              </div>
            </div>
          )}
        </div>
        {attachments.length > 0 && (
          <div
            style={{
              padding: '8px 12px 0',
              display: 'flex',
              flexWrap: 'wrap',
              gap: 8,
              alignItems: 'center',
            }}
          >
            <span style={{ fontSize: 12, color: '#999' }}>{t('assistant.pendingPush')}</span>
            {attachments.map((a) => (
              <Tag
                key={a.id}
                icon={<PaperClipOutlined />}
                closable
                closeIcon={<CloseOutlined />}
                onClose={(e) => {
                  e.preventDefault()
                  removeAttachment(a.id)
                }}
                style={{ marginInlineEnd: 0 }}
              >
                {a.rel_path || a.name}
                <span style={{ color: '#999', marginLeft: 6 }}>{sizeText(a.size_bytes)}</span>
              </Tag>
            ))}
            <span style={{ fontSize: 12, color: '#999' }}>
              {t('assistant.pushHint')}
            </span>
          </div>
        )}
        <div style={{ borderTop: '1px solid #f0f0f0' }}>
          <div className="assistant-composer">
            {pendingImages.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
                {pendingImages.map((img, i) => (
                  <div key={`${img.name}-${i}`} style={{ position: 'relative' }}>
                    <img
                      src={img.url}
                      alt={img.name}
                      style={{ height: 56, borderRadius: 6, border: '1px solid #f0f0f0', display: 'block' }}
                    />
                    <Button
                      size="small"
                      type="text"
                      icon={<CloseOutlined />}
                      onClick={() => setPendingImages((prev) => prev.filter((_, idx) => idx !== i))}
                      style={{ position: 'absolute', top: -6, right: -6, background: '#fff', borderRadius: '50%' }}
                    />
                  </div>
                ))}
              </div>
            )}
            <textarea
              className="assistant-composer-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onPaste={onPaste}
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing || e.keyCode === 229) return
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  sendText(input)
                }
              }}
              placeholder={supportsVision ? t('assistant.inputVision') : t('assistant.inputPlain')}
            />
            <div className="assistant-composer-bar">
              <ModelPicker
                models={models}
                value={activeModel?.id}
                disabled={loading}
                onChange={chooseModel}
                empty={modelsQuery.isError ? t('assistant.modelListFail') : t('assistant.noModel')}
              />
              <div className="assistant-composer-bar-right">
                <Upload
                  multiple
                  showUploadList={false}
                  beforeUpload={(file, fileList) => {
                    if (file === fileList[fileList.length - 1]) {
                      routeFiles(
                        fileList.map((f) => ({
                          file: f as unknown as File,
                          path: (f as unknown as { webkitRelativePath?: string }).webkitRelativePath || f.name,
                        }))
                      )
                    }
                    return false
                  }}
                >
                  <Tooltip
                    title={
                      supportsVision
                        ? t('assistant.uploadVision')
                        : t('assistant.uploadPlain')
                    }
                  >
                    <Button type="text" icon={<PaperClipOutlined />} loading={uploading} />
                  </Tooltip>
                </Upload>
                <Button
                  type="primary"
                  shape="circle"
                  icon={<SendOutlined />}
                  onClick={() => sendText(input)}
                  loading={loading}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
      </div>
      <Drawer title={t('assistant.sessions')} open={sessionDrawer} onClose={() => setSessionDrawer(false)} width="100%">
        {sessionPane}
      </Drawer>
      <Drawer title={t('assistant.catalogTitle')} open={catalogOpen} width={560} onClose={() => setCatalogOpen(false)}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('assistant.catalogScope')}
          description={t('assistant.catalogHint')}
        />
        <Card size="small" title={<Space><ToolOutlined />{t('assistant.builtinSkills')}</Space>} style={{ marginBottom: 16 }}>
          <Space size={[4, 8]} wrap>
            {skills.length ? skills.map((item) => (
              <Tag key={item.name} color="blue">
                {categoryLabel(item.category)} · {item.name}
              </Tag>
            )) : <span style={{ color: '#999' }}>{t('assistant.noneShort')}</span>}
          </Space>
        </Card>
        <Card size="small" title={<Space><ApartmentOutlined />{t('assistant.skillPacks')}</Space>} style={{ marginBottom: 16 }}>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            placeholder={t('assistant.pickSkills')}
            value={selectedSkills}
            onChange={setSelectedSkills}
            options={(catalogQuery.data || []).filter((item) => item.kind === 'agent-skill').map((item) => ({
              value: item.name,
              label: item.display_name || item.name,
              disabled: item.enabled === false,
            }))}
          />
        </Card>
        <Card size="small" title={<Space><ToolOutlined />{t('assistant.thirdPartyTools')}</Space>}>
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            placeholder={t('assistant.pickTools')}
            value={selectedTools}
            onChange={setSelectedTools}
            options={(catalogQuery.data || []).filter((item) => item.kind === 'agent-tool').map((item) => ({
              value: item.name,
              label: item.display_name || item.name,
              disabled: item.enabled === false,
            }))}
          />
        </Card>
      </Drawer>
      <Drawer title={t('assistant.traceTitle')} open={traceOpen} width={680} onClose={() => setTraceOpen(false)}>
        {traceMessage ? (
          <>
            {traceMessage.reasoning && (
              <Card size="small" title="Reasoning" style={{ marginBottom: 12 }}>
                <pre style={{ whiteSpace: 'pre-wrap' }}>{traceMessage.reasoning}</pre>
              </Card>
            )}
            {(traceMessage.events || []).map((event, index) => (
              <Card
                key={String(event.id || index)}
                size="small"
                title={<Space><Tag color={event.type === 'tool' ? 'purple' : event.type === 'reasoning' ? 'gold' : 'blue'}>{event.type}</Tag>{event.name}</Space>}
                style={{ marginBottom: 10 }}
              >
                <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0 }}>
                  {event.content || JSON.stringify({ arguments: event.arguments, result: event.result }, null, 2)}
                </pre>
              </Card>
            ))}
            <TraceDetailList traces={traceMessage.traces || []} />
          </>
        ) : <Empty description={t('assistant.noTrace')} />}
      </Drawer>
    </Card>
  )
}
