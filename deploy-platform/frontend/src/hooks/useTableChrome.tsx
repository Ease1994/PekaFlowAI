import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { ColumnsType, ColumnType } from 'antd/es/table'
import TableColumnSettings, {
  type ColumnOption,
  type TableSize,
} from '@/components/TableColumnSettings'
import { t } from '@/i18n'

const STORAGE_PREFIX = 'release.table.v2.'
export const LIST_PAGE_SIZES = [10, 20, 50, 100]
const DEFAULT_PAGE_SIZE = 20

function readPageSize(raw?: number): number {
  return LIST_PAGE_SIZES.includes(raw as number) ? (raw as number) : DEFAULT_PAGE_SIZE
}

interface Pref {
  visible?: string[]
  /** 上次见到的全部列。用来认出代码新加的列，刷新后仍默认显示。 */
  known?: string[]
  widths?: Record<string, number>
  size?: TableSize
  pageSize?: number
}

function colKey(c: ColumnType<unknown>, i: number): string {
  if (c.key != null && String(c.key).length) return String(c.key)
  const di = c.dataIndex
  if (Array.isArray(di) && di.length) return di.join('.')
  if (di != null && String(di).length) return String(di)
  return `col_${i}`
}

function colLabel(c: ColumnType<unknown>, key: string): string {
  if (typeof c.title === 'string' && c.title.trim()) return c.title.trim()
  if (key === 'actions') return t('common.action')
  return ''
}

function isLocked(key: string, _label: string, index: number): boolean {
  if (key === 'actions') return true
  return index === 0
}

function loadPref(scope: string): Pref {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + scope)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Pref
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function savePref(scope: string, pref: Pref) {
  try {
    localStorage.setItem(STORAGE_PREFIX + scope, JSON.stringify(pref))
  } catch {
    /* 隐私模式等写不进去就当没记住 */
  }
}

function declaredWidth(c: ColumnType<unknown>): number | undefined {
  const w = Number(c.width)
  return Number.isFinite(w) && w > 0 ? w : undefined
}

/**
 * 本机记住的显示列，补上代码后来才加、用户还没在齿轮里见过的列。
 * 旧偏好没有 known 时，只能拿当时亮着的列当「见过」；曾经藏过的列会跟着新列亮一次。
 */
function visibleKeysOf(scope: string, allKeys: string[]): string[] {
  const pref = loadPref(scope)
  const saved = pref.visible
  if (!Array.isArray(saved) || !saved.length) return allKeys
  const known = new Set(
    Array.isArray(pref.known) && pref.known.length ? pref.known : saved,
  )
  const next = saved.filter((k) => allKeys.includes(k))
  for (const key of allKeys) {
    if (!known.has(key) && !next.includes(key)) next.push(key)
  }
  return next.length ? next : allKeys
}

/** 给普通 antd Table 补上：列宽拖拽、字段显隐、行高，并记住到本机。 */
export function useTableChrome<T>(
  scope: string,
  columns: ColumnsType<T>,
  defaultSize?: TableSize,
) {
  const meta = useMemo(() => {
    return (columns as ColumnType<unknown>[]).map((c, i) => {
      const key = colKey(c, i)
      const label = colLabel(c, key)
      return {
        key,
        label: label || key,
        locked: isLocked(key, label, i) || !label,
        hideable: Boolean(label),
        width: declaredWidth(c),
      }
    })
  }, [columns])

  const options: ColumnOption[] = useMemo(
    () => meta.filter((m) => m.hideable).map((m) => ({ key: m.key, label: m.label, locked: m.locked })),
    [meta],
  )

  const allKeys = useMemo(() => meta.map((m) => m.key), [meta])

  const [visible, setVisible] = useState<string[]>(() => visibleKeysOf(scope, allKeys))
  const [widths, setWidths] = useState<Record<string, number>>(() => loadPref(scope).widths || {})
  const [size, setSize] = useState<TableSize | undefined>(() => {
    const s = loadPref(scope).size
    return s === 'small' || s === 'middle' || s === 'large' ? s : defaultSize
  })
  const [pageSize, setPageSize] = useState(() => readPageSize(loadPref(scope).pageSize))

  const seenKeys = useRef<string[] | null>(null)
  useEffect(() => {
    const known = new Set(allKeys)
    setVisible((prev) => {
      const seen = seenKeys.current
      const next = (prev.length ? prev : allKeys).filter((k) => known.has(k))
      for (const m of meta) {
        if ((m.locked || !m.hideable) && !next.includes(m.key)) next.push(m.key)
        if (seen && !seen.includes(m.key) && !next.includes(m.key)) next.push(m.key)
      }
      return next.length ? next : allKeys
    })
    seenKeys.current = allKeys
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allKeys.join('|')])

  useEffect(() => {
    savePref(scope, { visible, widths, size, pageSize, known: allKeys })
  }, [scope, visible, widths, size, pageSize, allKeys])

  const shown = useMemo(() => new Set(visible), [visible])

  const enhanced = useMemo(() => {
    const lastHideableIdx = (() => {
      let idx = -1
      ;(columns as ColumnType<T>[]).forEach((c, i) => {
        if (meta[i]?.hideable && (meta[i].locked || shown.has(meta[i].key))) idx = i
      })
      return idx
    })()

    return (columns as ColumnType<T>[])
      .map((c, i) => {
        const m = meta[i]
        if (!m) return c
        if (m.hideable && !m.locked && !shown.has(m.key)) return null
        const w = widths[m.key] ?? m.width
        const originalTitle = c.title
        const showGear = i === lastHideableIdx && options.length > 0
        const gear = (
          <TableColumnSettings
            options={options}
            visible={visible}
            size={size ?? 'middle'}
            onApply={(cols, sz) => {
              setVisible(cols)
              setSize(sz)
            }}
          />
        )
        const title = showGear
          ? (props: object) => (
              <span style={{ display: 'inline-flex', alignItems: 'center' }}>
                {typeof originalTitle === 'function'
                  ? (originalTitle as (p: object) => ReactNode)(props)
                  : (originalTitle as ReactNode)}
                {gear}
              </span>
            )
          : originalTitle
        return {
          ...c,
          key: m.key,
          title,
          ...(w != null ? { width: w } : {}),
          onHeaderCell: () => ({
            width: w,
            onResize: (next: number) => setWidths((prev) => ({ ...prev, [m.key]: next })),
          }),
        }
      })
      .filter(Boolean) as ColumnsType<T>
  }, [columns, meta, widths, shown, options, visible, size])

  const scrollX = useMemo(
    () => enhanced.reduce((sum, c) => sum + (Number((c as ColumnType<T>).width) || 0), 0),
    [enhanced],
  )

  return { columns: enhanced, size, scrollX, pageSize, setPageSize }
}
