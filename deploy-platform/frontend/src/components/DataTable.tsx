import { useEffect, useState } from 'react'
import { Table } from 'antd'
import type { TablePaginationConfig, TableProps } from 'antd'
import ResizableTitle from '@/components/ResizableTitle'
import { LIST_PAGE_SIZES, useTableChrome } from '@/hooks/useTableChrome'
import type { TableSize } from '@/components/TableColumnSettings'
import { useIsMobile } from '@/hooks/useIsMobile'
import { MobileTableCards } from '@/components/MobileTableCards'
import { t } from '@/i18n'

type Props<T extends object> = TableProps<T> & {
  /** 本机记住列宽/显隐/行高/每页条数的键，同一页多张表不要重复 */
  chromeKey: string
}

function mergePagination(
  passed: TableProps['pagination'],
  storedPageSize: number,
  setStoredPageSize: (n: number) => void,
): TablePaginationConfig {
  const extra = typeof passed === 'object' && passed ? passed : {}
  const remember = (page: number, ps: number) => {
    const size = Number(ps)
    if (LIST_PAGE_SIZES.includes(size)) setStoredPageSize(size)
    extra.onChange?.(page, size)
  }
  return {
    ...extra,
    size: extra.size ?? 'small',
    position: extra.position ?? ['bottomLeft'],
    showSizeChanger: true,
    showQuickJumper: false,
    showLessItems: true,
    hideOnSinglePage: false,
    pageSizeOptions: extra.pageSizeOptions ?? LIST_PAGE_SIZES,
    showTotal: extra.showTotal ?? ((n) => t('common.total', { n })),
    locale: { ...extra.locale, items_per_page: '' },
    pageSize: extra.pageSize ?? storedPageSize,
    onChange: remember,
    onShowSizeChange: (page, ps) => {
      const size = Number(ps)
      if (LIST_PAGE_SIZES.includes(size)) setStoredPageSize(size)
      extra.onShowSizeChange?.(page, size)
    },
  }
}

/**
 * 业务列表统一入口。
 * 宽屏：列宽可拖、齿轮显隐字段。
 * 窄屏：同一套 columns 收成卡片，避免再为每个页面维护第二套列表。
 */
export default function DataTable<T extends object>({
  chromeKey,
  columns = [],
  components,
  scroll,
  size,
  pagination,
  rootClassName,
  dataSource,
  rowKey,
  ...rest
}: Props<T>) {
  const chrome = useTableChrome<T>(chromeKey, columns, size as TableSize | undefined)
  /** 窄屏把宽表收成卡片，各业务页共用这一套，不再各写一遍。 */
  const isMobile = useIsMobile()
  /** 卡片列表当前页；筛选或条数变了会回到第 1 页。 */
  const [page, setPage] = useState(1)

  useEffect(() => {
    setPage(1)
  }, [chromeKey, (dataSource as unknown[] | undefined)?.length, chrome.pageSize])

  if (isMobile) {
    const passedSize =
      typeof pagination === 'object' && pagination && pagination.pageSize
        ? Number(pagination.pageSize)
        : chrome.pageSize
    return (
      <div>
        {rest.loading ? (
          <div style={{ padding: 24, textAlign: 'center' }}>{t('common.loading')}</div>
        ) : (
          <MobileTableCards<T>
            columns={columns}
            dataSource={dataSource as T[] | undefined}
            rowKey={rowKey}
            pagination={pagination}
            pageSize={passedSize || chrome.pageSize}
            page={page}
            onPageChange={(p, ps) => {
              setPage(p)
              if (LIST_PAGE_SIZES.includes(Number(ps))) chrome.setPageSize(Number(ps))
            }}
          />
        )}
      </div>
    )
  }

  const prevX = typeof scroll?.x === 'number' ? scroll.x : 0
  const nextScroll = scroll
    ? { ...scroll, x: prevX > 0 && chrome.scrollX > 0 ? Math.max(prevX, chrome.scrollX) : scroll.x }
    : undefined
  return (
    <Table<T>
      {...rest}
      rowKey={rowKey}
      dataSource={dataSource}
      size={chrome.size}
      columns={chrome.columns}
      rootClassName={['rp-table', rootClassName].filter(Boolean).join(' ')}
      components={{
        ...components,
        header: { ...components?.header, cell: ResizableTitle },
      }}
      scroll={nextScroll}
      pagination={mergePagination(pagination, chrome.pageSize, chrome.setPageSize)}
    />
  )
}
