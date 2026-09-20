import { Empty, Pagination } from 'antd'
import type { ColumnsType, ColumnType } from 'antd/es/table'
import type { TableProps } from 'antd'
import type { Key, ReactNode } from 'react'
import { t } from '@/i18n'

/**
 * 把 antd 列定义收成窄屏卡片。
 * 第一列当标题（名称会按词换行，不再竖排成一字一行），其余列当说明，操作列贴在卡片底部。
 */

function flattenColumns<T>(columns: ColumnsType<T>): ColumnType<T>[] {
  const out: ColumnType<T>[] = []
  for (const c of columns) {
    const kids = (c as ColumnType<T> & { children?: ColumnsType<T> }).children
    if (kids?.length) out.push(...flattenColumns(kids))
    else out.push(c as ColumnType<T>)
  }
  return out
}

/** 列标题：只要纯文本，函数标题（齿轮）不当标签。 */
function colLabel<T>(c: ColumnType<T>): string {
  return typeof c.title === 'string' ? c.title.trim() : ''
}

/** 操作列：key/标题是操作，或最后一列带一排按钮。 */
function isActionCol<T>(c: ColumnType<T>): boolean {
  const key = c.key != null ? String(c.key) : ''
  const label = colLabel(c)
  if (key === 'actions' || label === t('common.action') || label === '操作') return true
  // 有的表操作列标题留空，只挂了 render
  return !label && c.dataIndex == null && typeof c.render === 'function'
}

/** 按 dataIndex 取值；对象不当字符串甩出来。 */
function rawValue<T>(c: ColumnType<T>, record: T): unknown {
  const di = c.dataIndex
  let raw: unknown = record
  if (Array.isArray(di)) {
    for (const k of di) raw = (raw as Record<string, unknown> | undefined)?.[String(k)]
  } else if (di != null) {
    raw = (record as Record<string, unknown>)[String(di)]
  }
  return raw
}

/** 单元格展示：优先走列自己的 render。 */
function cellNode<T>(c: ColumnType<T>, record: T, index: number): ReactNode {
  const raw = rawValue(c, record)
  if (c.render) return c.render(raw as never, record, index) as ReactNode
  if (raw == null || raw === '') return '—'
  if (typeof raw === 'object') return null
  return String(raw)
}

/** 行主键，和 Table 的 rowKey 同一套口径。 */
function rowKeyOf<T>(rowKey: TableProps<T>['rowKey'], record: T, index: number): Key {
  if (typeof rowKey === 'function') return rowKey(record)
  if (typeof rowKey === 'string' && rowKey) {
    const v = (record as Record<string, unknown>)[rowKey]
    if (v != null) return v as Key
  }
  const id = (record as Record<string, unknown>).id
  return (id as Key) ?? index
}

export function MobileTableCards<T extends object>({
  columns,
  dataSource,
  rowKey,
  pagination,
  pageSize,
  page,
  onPageChange,
}: {
  columns: ColumnsType<T>
  dataSource?: readonly T[]
  rowKey?: TableProps<T>['rowKey']
  pagination?: TableProps<T>['pagination']
  pageSize: number
  page: number
  onPageChange: (page: number, pageSize: number) => void
}) {
  const cols = flattenColumns(columns)
  const action = cols.find(isActionCol)
  const dataCols = cols.filter((c) => c !== action)
  const titleCol = dataCols[0]
  const metaCols = dataCols.slice(1, 6)
  const rows = (dataSource || []) as T[]
  const paged = pagination !== false
  const start = paged ? (page - 1) * pageSize : 0
  const view = paged ? rows.slice(start, start + pageSize) : rows

  if (rows.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />

  return (
    <>
      <div className="mobile-entity-list">
        {view.map((record, i) => {
          const index = start + i
          return (
            <div key={rowKeyOf(rowKey, record, index)} className="mobile-entity-card">
              {titleCol ? (
                <div className="mobile-entity-title">{cellNode(titleCol, record, index)}</div>
              ) : null}
              {metaCols.map((c, mi) => {
                const node = cellNode(c, record, index)
                if (node == null || node === false) return null
                const label = colLabel(c)
                return (
                  <div key={c.key != null ? String(c.key) : `m${mi}`} className="mobile-entity-meta">
                    {label ? <span className="mobile-entity-label">{label}：</span> : null}
                    {node}
                  </div>
                )
              })}
              {action ? <div className="mobile-entity-actions">{cellNode(action, record, index)}</div> : null}
            </div>
          )
        })}
      </div>
      {paged ? (
        <Pagination
          current={page}
          pageSize={pageSize}
          total={rows.length}
          size="small"
          simple
          style={{ marginTop: 12, textAlign: 'right' }}
          onChange={onPageChange}
        />
      ) : null}
    </>
  )
}
