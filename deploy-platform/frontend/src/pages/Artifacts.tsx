import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import DataTable from '@/components/DataTable'
import type { TablePaginationConfig } from 'antd'
import type { SorterResult } from 'antd/es/table/interface'
import {
  DeleteOutlined,
  DownloadOutlined,
  InboxOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { del, get, getBlob, post } from '@/api/client'
import type { Project } from '@/api/types'
import { envColor, envLabel, envOptions, groupDisplayName } from '@/env'
import { formatDateTime, useT } from '@/i18n'

const { Text } = Typography

interface ArtifactRow {
  id: number
  pipeline_id: number
  release_id: number | null
  name: string
  type: string
  version: string
  sha256: string
  size_bytes: number | null
  created_at: string | null
  /** 归属信息，流水线已删除时为空 */
  pipeline_name: string
  project_id: number | null
  project_name: string
  group_name: string
  env: string
  build_number: number | null
}

interface ArtifactPage {
  items: ArtifactRow[]
  total: number
  total_bytes: number
  page: number
  page_size: number
}

interface ArtifactSummary {
  count: number
  total_bytes: number
  policy: { prod_retention_days: number; test_retention_days: number }
}

function humanSize(bytes: number | null | undefined): string {
  if (!bytes) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/**
 * 制品库：构建产出的包都在这儿，按流水线权限可见。
 *
 * 磁盘是会满的，所以这个页面的重点是「看清楚哪些包在占地方」并能批量清掉，
 * 同时把自动清理规则明写出来——否则包不见了没人知道是被谁删的。
 */
export default function Artifacts() {
  const t = useT()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [projectFilter, setProjectFilter] = useState<number>()
  const [typeFilter, setTypeFilter] = useState<string>()
  const [envFilter, setEnvFilter] = useState<string>()
  const [keyword, setKeyword] = useState('')
  const [sort, setSort] = useState('created_at')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [selected, setSelected] = useState<ArtifactRow[]>([])

  const query = {
    project_id: projectFilter,
    type: typeFilter,
    env: envFilter,
    keyword: keyword || undefined,
    sort,
    order,
    page,
    page_size: pageSize,
  }

  const { data, isFetching } = useQuery({
    queryKey: ['artifacts', query],
    queryFn: () => get<ArtifactPage>('/artifacts', query),
  })
  const { data: summary } = useQuery({
    queryKey: ['artifacts-summary'],
    queryFn: () => get<ArtifactSummary>('/artifacts/summary'),
  })
  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
  })

  const refresh = () => {
    setSelected([])
    queryClient.invalidateQueries({ queryKey: ['artifacts'] })
    queryClient.invalidateQueries({ queryKey: ['artifacts-summary'] })
  }

  const deleteMutation = useMutation({
    mutationFn: (ids: number[]) =>
      ids.length === 1
        ? del(`/artifacts/${ids[0]}`)
        : post('/artifacts/batch-delete', { ids }),
    onSuccess: () => {
      message.success(t('project.deleted'))
      refresh()
    },
  })

  const download = async (row: ArtifactRow) => {
    try {
      const blob = await getBlob(`/artifacts/${row.id}/download`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = row.name
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      // 拦截器已经弹过错误信息了
    }
  }

  const onTableChange = (
    pagination: TablePaginationConfig,
    _filters: unknown,
    sorter: SorterResult<ArtifactRow> | SorterResult<ArtifactRow>[],
  ) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter
    if (s?.field && s.order) {
      setSort(s.field === 'size_bytes' ? 'size' : String(s.field))
      setOrder(s.order === 'ascend' ? 'asc' : 'desc')
    }
    setPage(pagination.current || 1)
    setPageSize(pagination.pageSize || 20)
  }

  const selectedBytes = useMemo(
    () => selected.reduce((sum, r) => sum + (r.size_bytes || 0), 0),
    [selected],
  )

  const typeOptions = [
    { value: 'iis-package', label: t('art.incPackage') },
    { value: 'zip', label: 'zip' },
    { value: 'jar', label: 'jar' },
    { value: 'war', label: 'war' },
    { value: 'docker', label: 'docker' },
  ]

  const columns = [
    {
      title: t('art.artifact'),
      dataIndex: 'name',
      render: (v: string, r: ArtifactRow) => (
        <Space direction="vertical" size={0}>
          <Text strong>{v}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {r.type}
            {r.version ? ` · ${r.version}` : ''}
            {r.sha256 ? ` · ${r.sha256.slice(0, 12)}` : ''}
          </Text>
        </Space>
      ),
    },
    {
      title: t('art.owner'),
      dataIndex: 'pipeline_name',
      width: 260,
      render: (v: string, r: ArtifactRow) =>
        v ? (
          <Space direction="vertical" size={0}>
            <Space size={4}>
              <Text>{r.project_name}</Text>
              <Text type="secondary">/</Text>
              <a onClick={() => navigate(`/executions/${r.pipeline_id}`)}>{v}</a>
            </Space>
            <Space size={4}>
              {r.env && (
                <Tag color={envColor(r.env)} style={{ marginRight: 0 }}>
                  {envLabel(r.env)}
                </Tag>
              )}
              {r.group_name &&
                groupDisplayName({ name: r.group_name, type: r.env }) !== envLabel(r.env) && (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {groupDisplayName({ name: r.group_name, type: r.env })}
                  </Text>
                )}
            </Space>
          </Space>
        ) : (
          <Tooltip title={t('art.orphanHint')}>
            <Tag>{t('art.orphan')}</Tag>
          </Tooltip>
        ),
    },
    {
      title: t('art.fromRelease'),
      dataIndex: 'build_number',
      width: 110,
      render: (v: number | null, r: ArtifactRow) =>
        v && r.pipeline_id ? (
          <a onClick={() => navigate(`/executions/${r.pipeline_id}/${r.release_id}`)}>#{v}</a>
        ) : (
          <Text type="secondary">{t('art.manual')}</Text>
        ),
    },
    {
      title: t('art.size'),
      dataIndex: 'size_bytes',
      width: 110,
      sorter: true,
      defaultSortOrder: undefined,
      render: (v: number | null) => humanSize(v),
    },
    {
      title: t('art.createdAt'),
      dataIndex: 'created_at',
      width: 170,
      sorter: true,
      render: (v: string | null) => (v ? formatDateTime(v) : '-'),
    },
    {
      title: t('common.action'),
      width: 150,
      render: (_: unknown, r: ArtifactRow) => (
        <Space>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => download(r)}>
            {t('art.download')}
          </Button>
          <Popconfirm
            title={t('art.deleteOne')}
            description={t('art.deleteOneDesc')}
            okText={t('common.delete')}
            okButtonProps={{ danger: true }}
            cancelText={t('common.cancel')}
            onConfirm={() => deleteMutation.mutateAsync([r.id])}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const keepDays = summary?.policy.prod_retention_days ?? 10
  const testDays = summary?.policy.test_retention_days ?? 0

  return (
    <Card
      title={
        <Space>
          <InboxOutlined />
          {t('menu.artifacts')}
        </Space>
      }
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={refresh} loading={isFetching}>
            {t('common.refresh')}
          </Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('art.autoClean')}
        description={
          <>
            {t('art.policyLead')}
            {testDays > 0 ? t('art.keepDays', { n: testDays }) : t('art.keepToday')}
            {t('art.policyMid')}
            <b>{keepDays}</b>
            {t('art.policyTail')}
          </>
        }
      />

      <Space size="large" style={{ marginBottom: 16 }}>
        <Statistic title={t('art.visible')} value={summary?.count ?? 0} suffix={t('art.unit')} />
        <Statistic title={t('art.space')} value={humanSize(summary?.total_bytes ?? 0)} />
        {selected.length > 0 && (
          <Statistic
            title={t('art.selected')}
            value={selected.length}
            suffix={t('art.selectedSuffix', { size: humanSize(selectedBytes) })}
            valueStyle={{ color: '#1677ff' }}
          />
        )}
      </Space>

      <Space wrap style={{ marginBottom: 12, width: '100%' }}>
        <Select
          allowClear
          placeholder={t('deploy.filterProject')}
          style={{ width: 200 }}
          value={projectFilter}
          onChange={(v) => {
            setProjectFilter(v)
            setPage(1)
          }}
          options={projects.map((p) => ({ value: p.id, label: p.name }))}
          showSearch
          optionFilterProp="label"
        />
        <Select
          allowClear
          placeholder={t('art.filterEnv')}
          style={{ width: 140 }}
          value={envFilter}
          onChange={(v) => {
            setEnvFilter(v)
            setPage(1)
          }}
          options={envOptions()}
        />
        <Select
          allowClear
          placeholder={t('art.filterType')}
          style={{ width: 160 }}
          value={typeFilter}
          onChange={(v) => {
            setTypeFilter(v)
            setPage(1)
          }}
          options={typeOptions}
        />
        <Input.Search
          allowClear
          placeholder={t('art.searchName')}
          style={{ width: 220 }}
          onSearch={(v) => {
            setKeyword(v)
            setPage(1)
          }}
        />
        <Popconfirm
          title={t('art.deleteN', { n: selected.length })}
          description={t('art.deleteNDesc', { size: humanSize(selectedBytes) })}
          okText={t('common.delete')}
          okButtonProps={{ danger: true }}
          cancelText={t('common.cancel')}
          disabled={selected.length === 0}
          onConfirm={() => deleteMutation.mutateAsync(selected.map((r) => r.id))}
        >
          <Button
            danger
            icon={<DeleteOutlined />}
            disabled={selected.length === 0}
            loading={deleteMutation.isPending}
          >
            {selected.length > 0 ? t('art.batchDeleteN', { n: selected.length }) : t('art.batchDelete')}
          </Button>
        </Popconfirm>
      </Space>

      <DataTable
        chromeKey="artifacts"
        rowKey="id"
        size="small"
        loading={isFetching}
        columns={columns}
        dataSource={data?.items || []}
        onChange={onTableChange}
        rowSelection={{
          selectedRowKeys: selected.map((r) => r.id),
          onChange: (_keys, rows) => setSelected(rows),
          // 「全选」只作用于当前页，跨页批量删太容易误操作
          selections: [Table.SELECTION_ALL, Table.SELECTION_INVERT, Table.SELECTION_NONE],
        }}
        pagination={{
          current: data?.page || page,
          pageSize: data?.page_size || pageSize,
          total: data?.total || 0,
          showSizeChanger: true,
          showTotal: (n) => t('art.total', { n, size: humanSize(data?.total_bytes ?? 0) }),
        }}
        locale={{
          emptyText: <Empty description={t('art.empty')} />,
        }}
      />
    </Card>
  )
}
