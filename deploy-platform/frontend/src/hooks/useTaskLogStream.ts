import { useEffect, useState } from 'react'
import { post } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

/**
 * 订阅单个构建任务的日志流（SSE：先回放历史，再推增量）。
 *
 * EventSource 不能自定义 Header，不能把登录 JWT 放进 URL。
 * 先用会话换一张 60 秒 stream_token，断线再换一张继续。
 */
const MAX_LINES = 20000

function capped(prev: string[], incoming: string[]): string[] {
  const merged = prev.concat(incoming)
  if (merged.length <= MAX_LINES) return merged
  return [t('log.trimmed'), ...merged.slice(merged.length - MAX_LINES)]
}

/** 用登录会话换 SSE 短时票。失败返回空串，调用方不要去连。 */
async function fetchStreamToken(path: string): Promise<string> {
  const data = await post<{ stream_token: string }>(path)
  return data?.stream_token || ''
}

export function useTaskLogStream(taskId: number | undefined | null) {
  const token = useAuthStore((s) => s.token)
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!taskId) {
      setLines([])
      setLoading(false)
      return
    }
    setLines([])
    setLoading(true)
    let lastId = '0-0'
    let closed = false
    let es: EventSource | null = null

    let buffer: string[] = []
    let frame = 0
    const flush = () => {
      frame = 0
      if (!buffer.length) return
      const pending = buffer
      buffer = []
      setLines((prev) => capped(prev, pending))
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(flush)
    }

    const connect = async () => {
      if (closed) return
      let streamToken = ''
      try {
        streamToken = await fetchStreamToken(`/tasks/${taskId}/logs/stream-token`)
      } catch {
        setLoading(false)
        return
      }
      if (closed || !streamToken) {
        setLoading(false)
        return
      }
      const url =
        `/api/v1/tasks/${taskId}/logs/stream` +
        `?stream_token=${encodeURIComponent(streamToken)}&from=${encodeURIComponent(lastId)}`
      es = new EventSource(url)
      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data)
          if (Array.isArray(data.lines)) {
            buffer = buffer.concat(data.lines)
            schedule()
          } else if (data.line !== undefined) {
            buffer.push(data.line)
            schedule()
          }
          if (data.id) lastId = String(data.id)
        } catch {
          /* 忽略非 JSON */
        }
        setLoading(false)
      }
      es.addEventListener('done', () => {
        closed = true
        es?.close()
        flush()
        setLoading(false)
      })
      es.onerror = () => {
        es?.close()
        setLoading(false)
        if (!closed) {
          window.setTimeout(() => {
            void connect()
          }, 1000)
        }
      }
    }
    void connect()

    return () => {
      closed = true
      es?.close()
      if (frame) cancelAnimationFrame(frame)
    }
  }, [taskId, token])

  return { lines, loading }
}

/**
 * 订阅整次发布的日志流（所有任务按 stage/job 串起来）。
 */
export function useReleaseLogStream(releaseId: number | undefined | null) {
  const token = useAuthStore((s) => s.token)
  const [lines, setLines] = useState<string[]>([])
  const [done, setDone] = useState(false)

  useEffect(() => {
    setLines([])
    setDone(false)
    if (!releaseId) return

    let closed = false
    let es: EventSource | null = null
    let buffer: string[] = []
    let frame = 0
    const flush = () => {
      frame = 0
      if (!buffer.length) return
      const pending = buffer
      buffer = []
      setLines((prev) => capped(prev, pending))
    }
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(flush)
    }

    const connect = async () => {
      if (closed) return
      let streamToken = ''
      try {
        streamToken = await fetchStreamToken(`/releases/${releaseId}/logs/stream-token`)
      } catch {
        return
      }
      if (closed || !streamToken) return
      const url = `/api/v1/releases/${releaseId}/logs/stream?stream_token=${encodeURIComponent(streamToken)}`
      es = new EventSource(url)
      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data)
          if (Array.isArray(data.lines)) {
            buffer = buffer.concat(data.lines)
            schedule()
          } else if (data.line !== undefined) {
            buffer.push(data.line)
            schedule()
          }
        } catch {
          /* 忽略非 JSON 心跳 */
        }
      }
      es.addEventListener('done', () => {
        closed = true
        es?.close()
        flush()
        setDone(true)
      })
      es.onerror = () => {
        es?.close()
        if (!closed) {
          window.setTimeout(() => {
            void connect()
          }, 1000)
        }
      }
    }
    void connect()

    return () => {
      closed = true
      es?.close()
      if (frame) cancelAnimationFrame(frame)
    }
  }, [releaseId, token])

  return { lines, done }
}
