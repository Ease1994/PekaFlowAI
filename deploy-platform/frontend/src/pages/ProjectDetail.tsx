import { Alert, Card, Menu, Drawer, Table, Tag, Button, Space, Modal, Form, Input, Select, Empty, Radio, Popconfirm, Switch, Tooltip, Dropdown, message, Pagination } from 'antd'
import { PlusOutlined, DeploymentUnitOutlined, EditOutlined, RocketOutlined, AppstoreOutlined, CopyOutlined, DeleteOutlined, UndoOutlined, StarFilled, StarOutlined, FolderOpenOutlined, DatabaseOutlined, RestOutlined, EllipsisOutlined, CheckCircleFilled, CloseCircleFilled, SyncOutlined, UserOutlined, MinusCircleOutlined, ClockCircleOutlined, DownOutlined, TeamOutlined } from '@ant-design/icons'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, post, put } from '@/api/client'
import type { Group, Pipeline, Project } from '@/api/types'
import { defaultApprovalRequired, ENV_SLUG, envColor, envLabel, envOptions, groupDisplayName, groupOptionLabel, isFactoryGroupName } from '@/env'
import { useAuthStore } from '@/stores/auth'
import CatalogTransferButtons from '@/components/CatalogTransfer'
import { useIsMobile } from '@/hooks/useIsMobile'
import RepositoryPanel from '@/components/project/RepositoryPanel'
import DataTable from '@/components/DataTable'
import ResizableTitle from '@/components/ResizableTitle'
import TableColumnSettings, {
  type ColumnOption,
  type TableSize,
} from '@/components/TableColumnSettings'
import { formatDateTime, useT } from '@/i18n'

/** 回收站条目：流水线 + 后端算好的保留期信息 */
type RecycledPipeline = Pipeline & { deleted_at?: string; expire_at?: string; days_left?: number }

const ts = (v?: string | null) => (v ? new Date(v).getTime() : 0)
const fmtTime = (v?: string | null) => (v ? formatDateTime(v) : '—')

const RUN_BUSY = new Set(['queued', 'assigned', 'running', 'rolling_back'])

const PIPELINE_COL_DEFS: { key: string; labelKey: string; locked?: boolean }[] = [
  { key: 'name', labelKey: 'project.colName', locked: true },
  { key: 'group', labelKey: 'project.colGroup' },
  { key: 'version', labelKey: 'project.colVersion' },
  { key: 'exec', labelKey: 'project.colExec' },
  { key: 'last_run', labelKey: 'project.colLastRun' },
  { key: 'updated', labelKey: 'project.colUpdated' },
  { key: 'creator', labelKey: 'project.colCreator' },
  { key: 'created', labelKey: 'project.colCreated' },
  { key: 'actions', labelKey: 'common.action', locked: true },
]
const DEFAULT_VISIBLE = ['name', 'group', 'version', 'exec', 'last_run', 'updated', 'created', 'actions']
const DEFAULT_WIDTHS: Record<string, number> = {
  starred: 40,
  name: 260,
  group: 160,
  version: 150,
  exec: 180,
  last_run: 180,
  updated: 180,
  creator: 110,
  created: 180,
  actions: 240,
}

function ExecStatusCell({
  pipeline,
  onOpen,
}: {
  pipeline: Pipeline
  onOpen: (p: Pipeline, releaseId: number) => void
}) {
  const t = useT()
  const rel = pipeline.last_release
  if (!rel) {
    return (
      <span style={{ color: '#bbb' }}>
        <span
          style={{
            display: 'inline-block',
            width: 12,
            height: 12,
            border: '1.5px solid #d9d9d9',
            borderRadius: '50%',
            marginRight: 6,
            verticalAlign: -1,
          }}
        />
        {t('project.neverRun')}
      </span>
    )
  }
  const link = (
    <a onClick={() => onOpen(pipeline, rel.id)}>
      #{rel.build_number} | {rel.trigger_label || rel.trigger_by}
    </a>
  )
  const operator = rel.operator_name ? (
    <div style={{ fontSize: 12, lineHeight: '18px' }}>
      <UserOutlined style={{ marginRight: 4 }} />
      <span style={{ color: '#52c41a' }}>{rel.operator_name}</span>
    </div>
  ) : null

  if (rel.status === 'success') {
    return (
      <div>
        <div>
          <CheckCircleFilled style={{ color: '#52c41a', marginRight: 6 }} />
          {link}
        </div>
        {operator}
      </div>
    )
  }
  if (rel.status === 'failed') {
    return (
      <div>
        <div style={{ color: '#ff4d4f' }}>
          <CloseCircleFilled style={{ marginRight: 6 }} />
          {t('project.runFailed')}
        </div>
        <div>{link}</div>
        {rel.error_summary ? (
          <Tooltip title={rel.error_summary}>
            <div
              style={{
                color: '#ff4d4f',
                fontSize: 12,
                maxWidth: 220,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {rel.error_summary}
            </div>
          </Tooltip>
        ) : null}
        {operator}
      </div>
    )
  }
  if (RUN_BUSY.has(rel.status)) {
    return (
      <div>
        <div>
          <SyncOutlined spin style={{ color: '#1677ff', marginRight: 6 }} />
          {t('project.running')}
        </div>
        <div>{link}</div>
        {operator}
      </div>
    )
  }
  const other: Record<string, { color: string; text: string; icon: ReactNode }> = {
    pending: { color: '#fa8c16', text: t('status.pending'), icon: <ClockCircleOutlined /> },
    rejected: { color: '#ff4d4f', text: t('status.rejected'), icon: <CloseCircleFilled /> },
    cancelled: { color: '#8c8c8c', text: t('status.cancelled'), icon: <MinusCircleOutlined /> },
    rolled_back: { color: '#8c8c8c', text: t('status.rolledBack'), icon: <UndoOutlined /> },
  }
  const meta = other[rel.status] || { color: '#8c8c8c', text: rel.status, icon: <MinusCircleOutlined /> }
  return (
    <div>
      <div style={{ color: meta.color }}>
        <span style={{ marginRight: 6 }}>{meta.icon}</span>
        {meta.text}
      </div>
      <div>{link}</div>
      {operator}
    </div>
  )
}

/** 往个人分组里加流水线：表格勾选 + 搜索分页，下拉框扛不住上千条。 */
function AddPipelinesModal({
  open,
  folder,
  pipelines,
  groups,
  selectedIds,
  saving,
  onChangeIds,
  onOk,
  onCancel,
}: {
  open: boolean
  folder: string
  pipelines: Pipeline[]
  groups: Group[]
  selectedIds: number[]
  saving: boolean
  onChangeIds: (ids: number[]) => void
  onOk: () => void
  onCancel: () => void
}) {
  const t = useT()
  const [kw, setKw] = useState('')
  const [groupId, setGroupId] = useState<number | undefined>()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const groupMap = useMemo(() => Object.fromEntries(groups.map((g) => [g.id, g])), [groups])

  useEffect(() => {
    if (!open) return
    setKw('')
    setGroupId(undefined)
    setPage(1)
  }, [open])

  const candidates = useMemo(() => {
    const k = kw.trim().toLowerCase()
    return pipelines.filter((p) => {
      if ((p.folder || '') === folder) return false
      if (groupId && p.group_id !== groupId) return false
      if (k && !p.name.toLowerCase().includes(k)) return false
      return true
    })
  }, [pipelines, folder, groupId, kw])

  useEffect(() => {
    setPage(1)
  }, [kw, groupId])

  return (
    <Modal
      title={t('project.addToFolderTitle', { name: folder })}
      open={open}
      width={760}
      onOk={onOk}
      onCancel={onCancel}
      okText={selectedIds.length ? t('project.joinFolderN', { n: selectedIds.length }) : t('project.joinFolder')}
      okButtonProps={{ disabled: selectedIds.length === 0 }}
      confirmLoading={saving}
      cancelText={t('common.cancel')}
      destroyOnClose
    >
      <div style={{ marginBottom: 12, color: '#666', fontSize: 13 }}>
        {t('project.addFolderHint')}
      </div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          placeholder={t("project.searchName")}
          style={{ width: 260 }}
          value={kw}
          onChange={(e) => setKw(e.target.value)}
        />
        <Select
          allowClear
          placeholder={t("project.filterGroup")}
          style={{ width: 200 }}
          value={groupId}
          onChange={setGroupId}
          options={groups.map((g) => ({ value: g.id, label: groupOptionLabel(g) }))}
        />
      </Space>
      <DataTable
        chromeKey="pipeline-folder-add"
        rowKey="id"
        size="small"
        dataSource={candidates}
        rowSelection={{
          selectedRowKeys: selectedIds,
          onChange: (keys) => onChangeIds(keys.map(Number)),
          preserveSelectedRowKeys: true,
          selections: [Table.SELECTION_ALL, Table.SELECTION_INVERT, Table.SELECTION_NONE],
        }}
        pagination={{
          current: page,
          pageSize,
          showSizeChanger: true,
          pageSizeOptions: [10, 20, 50, 100],
          showTotal: (total) => t('project.addableTotal', { total, n: selectedIds.length }),
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
        scroll={{ y: 360 }}
        locale={{ emptyText: kw || groupId ? t('project.emptyMatch') : t('project.emptyJoin') }}
        columns={[
          {
            title: t('project.colPipeline'),
            dataIndex: 'name',
            ellipsis: true,
            render: (v: string) => (
              <span style={{ whiteSpace: 'nowrap' }}>{v}</span>
            ),
          },
          {
            title: t('project.colEnv'),
            dataIndex: 'group_id',
            width: 140,
            render: (id: number) => {
              const g = groupMap[id]
              return g ? <Tag color={envColor(g.type)}>{groupDisplayName(g)}</Tag> : '—'
            },
          },
          {
            title: t('project.colFolder'),
            dataIndex: 'folder',
            width: 140,
            ellipsis: true,
            render: (v: string) => v || <span style={{ color: '#bbb' }}>{t('project.ungrouped')}</span>,
          },
        ]}
      />
    </Modal>
  )
}

/** 左侧导航一行：文字撑开 + 右侧数量，选中态由 Menu 负责高亮。 */
function navLabel(text: ReactNode, count: number, tail?: ReactNode) {
  return (
    <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{text}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
        {tail}
        <span style={{ color: '#999', fontSize: 12 }}>{count}</span>
      </span>
    </span>
  )
}

export default function ProjectDetail() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const user = useAuthStore((s) => s.user)
  /** 窄屏：左侧分组改下拉，流水线改卡片，避免 232px 导航占掉半屏。 */
  const isMobile = useIsMobile()
  const t = useT()
  const pipelineColOptions: ColumnOption[] = PIPELINE_COL_DEFS.map((o) => ({
    key: o.key,
    label: t(o.labelKey),
    locked: o.locked,
  }))
  const [open, setOpen] = useState(false)
  const [dupOpen, setDupOpen] = useState(false)
  const [dupSource, setDupSource] = useState<Pipeline | null>(null)
  const [policyGroup, setPolicyGroup] = useState<Group | null>(null)
  const [createGroupOpen, setCreateGroupOpen] = useState(false)
  const [repoOpen, setRepoOpen] = useState(false)
  const [recycleOpen, setRecycleOpen] = useState(false)
  const restoreNameRef = useRef('')
  const [dupForm] = Form.useForm()
  const [groupForm] = Form.useForm()
  const [createGroupForm] = Form.useForm()
  const [form] = Form.useForm()

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => get<Project>(`/projects/${projectId}`),
  })

  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
  })

  const projectOptions = useMemo(() => {
    const byId = new Map<number, Project>()
    for (const p of projects) byId.set(p.id, p)
    if (project && !byId.has(project.id)) byId.set(project.id, project)
    return [...byId.values()].map((p) => ({
      value: p.id,
      label: `${p.name}（${p.code}）`,
    }))
  }, [projects, project])

  const currentProjectId = Number(projectId)

  const { data: groups = [] } = useQuery({
    queryKey: ['groups', projectId],
    queryFn: () => get<Group[]>('/groups', { project_id: Number(projectId) }),
  })

  const { data: pipelines = [] } = useQuery({
    queryKey: ['pipelines', projectId],
    queryFn: () => get<Pipeline[]>('/pipelines', { project_id: Number(projectId) }),
    refetchInterval: (q) =>
      q.state.data?.some((p) =>
        ['queued', 'assigned', 'running', 'rolling_back'].includes(p.last_release?.status || ''),
      )
        ? 5000
        : false,
  })

  const { data: personalFolders = [] } = useQuery({
    queryKey: ['personal-folders', projectId],
    queryFn: () => get<string[]>('/personal-folders', { project_id: Number(projectId) }),
    enabled: !!projectId,
  })

  const { data: recycled = [] } = useQuery({
    queryKey: ['recycled-pipelines', projectId],
    queryFn: () =>
      get<RecycledPipeline[]>('/pipelines/recycle-bin', { project_id: Number(projectId) }),
  })

  const groupMap = Object.fromEntries(groups.map((g) => [g.id, g]))
  const canCreate = !!user?.is_admin || !!project?.can_create_pipeline
  const canManageGroup = canCreate
  const canDeleteGroup = !!user?.is_admin || !!project?.can_delete_group
  const canUseRecycleBin =
    !!user?.is_admin || recycled.length > 0 || pipelines.some((p) => p.can_delete)

  // ---- 列表个性化：左侧导航筛选 + 表头排序，全部按当前用户持久化 ----
  const [keyword, setKeyword] = useState('')
  // nav：'all' | 'fav' | 'folder:<名称>' | 'group:<id>'
  const [nav, setNav] = useState<string>('all')
  // 表头排序：field 为列的 dataIndex，order 为 antd 的 ascend/descend；空=按默认（收藏置顶）
  const [sorter, setSorter] = useState<{ field?: string; order?: 'ascend' | 'descend' }>({})
  const [folderInput, setFolderInput] = useState('')
  // create = 左侧新建空组；rename = 重命名；add = 从分组侧把流水线加进去
  const [folderModal, setFolderModal] = useState<'create' | 'rename' | 'add' | null>(null)
  const [renameFrom, setRenameFrom] = useState('')
  const [folderAddTo, setFolderAddTo] = useState('')
  const [folderAddIds, setFolderAddIds] = useState<number[]>([])
  const [folderAddSaving, setFolderAddSaving] = useState(false)
  /** 流水线列表当前页；筛选条件变了会重置到第 1 页。 */
  const [listPage, setListPage] = useState(1)
  /** 每页条数；桌面表格和手机卡片共用。 */
  const [listPageSize, setListPageSize] = useState(20)
  const [projectSwitchOpen, setProjectSwitchOpen] = useState(false)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [visibleCols, setVisibleCols] = useState<string[]>(DEFAULT_VISIBLE)
  const [colWidths, setColWidths] = useState<Record<string, number>>(DEFAULT_WIDTHS)
  const [tableSize, setTableSize] = useState<TableSize>('small')
  const viewApplied = useRef(false)
  const prevProjectId = useRef(projectId)

  useEffect(() => {
    if (prevProjectId.current === projectId) return
    prevProjectId.current = projectId
    setKeyword('')
    setListPage(1)
    setSelectedIds([])
    setNav('all')
    setRepoOpen(false)
    setRecycleOpen(false)
    setOpen(false)
    setProjectSwitchOpen(false)
  }, [projectId])

  // 记住的视图偏好：进页面先读回来应用一次，之后改动防抖写回
  const { data: savedView } = useQuery({
    queryKey: ['view-pref', 'pipeline_list'],
    queryFn: () => get<Record<string, any>>('/view-pref/pipeline_list'),
  })
  useEffect(() => {
    if (!savedView || viewApplied.current) return
    if (savedView.nav) setNav(savedView.nav)
    if (savedView.sortField) setSorter({ field: savedView.sortField, order: savedView.sortOrder })
    if (Array.isArray(savedView.columns) && savedView.columns.length) {
      const known = new Set(PIPELINE_COL_DEFS.map((o) => o.key))
      const next = (savedView.columns as string[]).filter((k) => known.has(k))
      for (const o of PIPELINE_COL_DEFS) {
        if (o.locked && !next.includes(o.key)) next.push(o.key)
      }
      if (next.includes('name')) setVisibleCols(next)
    }
    if (savedView.widths && typeof savedView.widths === 'object') {
      setColWidths({ ...DEFAULT_WIDTHS, ...(savedView.widths as Record<string, number>) })
    }
    if (savedView.tableSize === 'small' || savedView.tableSize === 'middle' || savedView.tableSize === 'large') {
      setTableSize(savedView.tableSize)
    }
    viewApplied.current = true
  }, [savedView])
  useEffect(() => {
    if (!viewApplied.current) return
    const t = setTimeout(() => {
      // 关键词是临时搜索，不持久化；导航选择和排序才是「习惯」
      put('/view-pref/pipeline_list', {
        nav,
        sortField: sorter.field ?? null,
        sortOrder: sorter.order ?? null,
        columns: visibleCols,
        widths: colWidths,
        tableSize,
      }).catch(() => {})
    }, 500)
    return () => clearTimeout(t)
  }, [nav, sorter.field, sorter.order, visibleCols, colWidths, tableSize])

  useEffect(() => {
    setListPage(1)
    setSelectedIds([])
  }, [nav, keyword])

  // 目录是一等公民：空组也在。接口失败时仍能从流水线偏好里看到已用过的组。
  const folders = useMemo(() => {
    const s = new Set(personalFolders)
    pipelines.forEach((p) => p.folder && s.add(p.folder))
    return Array.from(s)
  }, [personalFolders, pipelines])

  const favCount = useMemo(() => pipelines.filter((p) => p.starred).length, [pipelines])
  const groupCount = useMemo(() => {
    const m: Record<number, number> = {}
    pipelines.forEach((p) => { m[p.group_id] = (m[p.group_id] || 0) + 1 })
    return m
  }, [pipelines])
  const folderCount = useMemo(() => {
    const m: Record<string, number> = {}
    pipelines.forEach((p) => { if (p.folder) m[p.folder] = (m[p.folder] || 0) + 1 })
    return m
  }, [pipelines])

  const visiblePipelines = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    const arr = pipelines.filter((p) => {
      if (nav === 'fav') {
        if (!p.starred) return false
      } else if (nav.startsWith('folder:')) {
        if ((p.folder || '') !== nav.slice(7)) return false
      } else if (nav.startsWith('group:')) {
        if (p.group_id !== Number(nav.slice(6))) return false
      }
      if (kw && !p.name.toLowerCase().includes(kw)) return false
      return true
    })
    // 基础顺序：⭐收藏恒定置顶 + 创建倒序；点击表头排序时由列 sorter 覆盖
    return [...arr].sort((a, b) => {
      if (!!a.starred !== !!b.starred) return a.starred ? -1 : 1
      return ts(b.created_at) - ts(a.created_at) || b.id - a.id
    })
  }, [pipelines, keyword, nav])

  const toggleStar = async (p: Pipeline) => {
    try {
      await put(`/pipelines/${p.id}/pref`, { starred: !p.starred })
      queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    } catch {
      /* 拦截器已提示 */
    }
  }

  const saveFolder = async (p: Pipeline, folder: string, silent = false) => {
    try {
      await put(`/pipelines/${p.id}/pref`, { folder })
      if (!silent) {
        queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
        queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
        message.success(folder ? t('project.movedTo', { name: folder }) : t('project.movedOut'))
      }
    } catch {
      /* 拦截器已提示 */
    }
  }

  const addPipelinesToFolder = async (folder: string, ids: number[]) => {
    if (!ids.length) {
      message.warning(t('project.pickToAdd'))
      return false
    }
    await Promise.all(
      ids.map((id) => {
        const p = pipelines.find((x) => x.id === id)
        return p ? saveFolder(p, folder, true) : Promise.resolve()
      }),
    )
    queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
    message.success(t('project.addedN', { n: ids.length, name: folder }))
    return true
  }

  const removeSelectedFromFolder = async () => {
    if (!selectedIds.length) {
      message.warning(t('project.pickToRemove'))
      return
    }
    await Promise.all(
      selectedIds.map((id) => {
        const p = pipelines.find((x) => x.id === id)
        return p ? saveFolder(p, '', true) : Promise.resolve()
      }),
    )
    queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
    message.success(t('project.removedN', { n: selectedIds.length }))
    setSelectedIds([])
  }

  const openAddToFolder = (folder: string) => {
    setFolderAddTo(folder)
    setFolderAddIds([])
    setFolderModal('add')
  }

  const currentFolder = nav.startsWith('folder:') ? nav.slice(7) : ''
  const currentGroupId = nav.startsWith('group:') ? Number(nav.slice(6)) : undefined

  const createFolder = async (name: string) => {
    const names = await post<string[]>('/personal-folders', {
      project_id: Number(projectId),
      name,
    })
    queryClient.setQueryData(['personal-folders', projectId], names)
    queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
    return names
  }

  const handleCreatePipeline = async (values: any) => {
    const { mode, ...rest } = values
    const editor_view = mode === 'canvas' ? 'canvas' : 'form'
    const newPipeline = await post<Pipeline>('/pipelines', {
      ...rest,
      project_id: Number(projectId),
      folder: currentFolder || undefined,
      editor_view,
    })
    queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
    navigate(`/pipeline/${newPipeline.id}/edit?mode=${editor_view}`)
  }

  // 打开复制弹窗：预填名称（加 _copy 后缀）+ 描述 + 同分组
  const openDuplicate = (p: Pipeline) => {
    setDupSource(p)
    dupForm.resetFields()
    dupForm.setFieldsValue({
      name: `${p.name}_copy`,
      description: p.description,
      group_id: p.group_id,
    })
    setDupOpen(true)
  }

  // 提交复制
  const handleDuplicate = async () => {
    if (!dupSource) return
    const values = await dupForm.validateFields()
    const newP = await post<Pipeline>(`/pipelines/${dupSource.id}/duplicate`, {
      name: values.name,
      description: values.description,
      group_id: values.group_id,
      folder: currentFolder || undefined,
    })
    queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
    setDupOpen(false)
    message.success(t('project.copiedAs', { name: newP.name }))
    navigate(`/pipeline/${newP.id}/edit`)
  }

  const refreshPipelines = () => {
    queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
    queryClient.invalidateQueries({ queryKey: ['recycled-pipelines', projectId] })
  }

  const handleDeletePipeline = async (p: Pipeline) => {
    await del(`/pipelines/${p.id}`)
    refreshPipelines()
    message.success(t('project.trashed', { name: p.name }))
  }

  const handleRestore = async (p: RecycledPipeline) => {
    try {
      await post(`/pipelines/${p.id}/restore`, {})
      refreshPipelines()
      message.success(t('project.restored', { name: p.name }))
    } catch (e: any) {
      // 删除后又建了同名流水线，原名被占了：让用户换个名字再恢复
      restoreNameRef.current = `${p.name}_restored`
      Modal.confirm({
        title: t('project.restoreNameBusy'),
        content: (
          <div>
            <div style={{ marginBottom: 8, color: '#999' }}>{e?.message ?? t('project.nameConflict')}</div>
            <Input
              defaultValue={restoreNameRef.current}
              onChange={(ev) => (restoreNameRef.current = ev.target.value)}
            />
          </div>
        ),
        okText: t('project.restore'),
        cancelText: t('common.cancel'),
        onOk: async () => {
          const name = restoreNameRef.current.trim()
          if (!name) {
            message.error(t('project.nameRequired'))
            return Promise.reject()
          }
          await post(`/pipelines/${p.id}/restore`, { name })
          refreshPipelines()
          message.success(t('project.restoredAs', { name }))
        },
      })
    }
  }

  const handlePurge = async (p: RecycledPipeline) => {
    await del(`/pipelines/${p.id}/purge`)
    refreshPipelines()
    message.success(t('project.purged', { name: p.name }))
  }

  // 环境分组的审批策略：强制审批 / 允许自审 / 应急跳审
  const openGroupPolicy = (g: Group) => {
    setPolicyGroup(g)
    groupForm.setFieldsValue({
      approval_required: g.approval_required,
      allow_self_approval: g.allow_self_approval,
      allow_emergency_bypass: g.allow_emergency_bypass,
      pm_approval_required: g.pm_approval_required,
      change_window: g.change_window || '',
    })
  }

  const handleSaveGroupPolicy = async () => {
    if (!policyGroup) return
    const values = await groupForm.validateFields()
    await put(`/groups/${policyGroup.id}`, values)
    queryClient.invalidateQueries({ queryKey: ['groups', projectId] })
    setPolicyGroup(null)
    message.success(t('project.policySaved'))
  }

  const openCreateGroup = () => {
    createGroupForm.resetFields()
    createGroupForm.setFieldsValue({
      type: 'uat',
      name: envLabel('uat'),
      approval_required: true,
      allow_self_approval: true,
      allow_emergency_bypass: false,
      pm_approval_required: false,
    })
    setCreateGroupOpen(true)
  }

  const handleCreateGroup = async () => {
    const v = await createGroupForm.validateFields()
    const type = v.type === '__custom__' ? String(v.custom_type || '').trim().toLowerCase() : v.type
    if (!type || !ENV_SLUG.test(type)) {
      message.warning(t('project.envSlugBad'))
      return
    }
    await post('/groups', {
      project_id: Number(projectId),
      name: v.name.trim(),
      type,
      approval_required: v.approval_required,
      allow_self_approval: v.allow_self_approval,
      allow_emergency_bypass: v.allow_emergency_bypass,
      pm_approval_required: v.pm_approval_required,
    })
    queryClient.invalidateQueries({ queryKey: ['groups', projectId] })
    setCreateGroupOpen(false)
    message.success(t('project.groupCreated', { name: v.name.trim() }))
  }

  const handleDeleteGroup = (g: Group) => {
    const busy = (groupCount[g.id] || 0) > 0
    const inTrash = recycled.some((p) => p.group_id === g.id)
    if (busy || inTrash) {
      message.warning(
        inTrash && !busy
          ? t('project.groupHasRecycle', { name: groupDisplayName(g) })
          : t('project.groupHasPipelines', { name: groupDisplayName(g) }),
      )
      return
    }
    Modal.confirm({
      title: t('project.deleteGroupTitle', { name: groupDisplayName(g) }),
      content: t('project.deleteGroupBody'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await del(`/groups/${g.id}`)
        queryClient.invalidateQueries({ queryKey: ['groups', projectId] })
        if (nav === `group:${g.id}`) setNav('all')
        message.success(t('project.deleted'))
      },
    })
  }

  const folderMenu = (f: string) => ({
    items: [
      { key: 'add', label: t('project.addPipelines') },
      { key: 'rename', label: t('common.rename') },
      { key: 'delete', label: t('project.deleteGroup'), danger: true },
    ],
    onClick: (info: { key: string; domEvent: { stopPropagation: () => void } }) => {
      info.domEvent.stopPropagation()
      if (info.key === 'add') {
        openAddToFolder(f)
      } else if (info.key === 'rename') {
        setRenameFrom(f)
        setFolderInput(f)
        setFolderModal('rename')
      } else if (info.key === 'delete') {
        Modal.confirm({
          title: t('project.deleteFolderTitle', { name: f }),
          content: t('project.deleteFolderBody'),
          okText: t('common.delete'),
          okButtonProps: { danger: true },
          cancelText: t('common.cancel'),
          onOk: async () => {
            await del(
              `/personal-folders?project_id=${Number(projectId)}&name=${encodeURIComponent(f)}`,
            )
            queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
            queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
            if (nav === `folder:${f}`) setNav('all')
            message.success(t('project.folderDeleted'))
          },
        })
      }
    },
  })

  // 左侧导航：全部 / 我的收藏 + 个人分组 / 环境分组（分组带审批策略齿轮）
  const navItems = [
    { key: 'all', icon: <AppstoreOutlined />, label: navLabel(t('project.allPipelines'), pipelines.length) },
    {
      type: 'group' as const,
      label: (
        <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <span>{t('project.personalFolders')}</span>
          <Tooltip title={t("project.newPersonalFolder")}>
            <PlusOutlined
              style={{ fontSize: 12, color: '#1677ff' }}
              onClick={(e) => {
                e.stopPropagation()
                setFolderInput('')
                setFolderModal('create')
              }}
            />
          </Tooltip>
        </span>
      ),
      children: [
        {
          key: 'fav',
          icon: <StarFilled style={{ color: '#faad14' }} />,
          label: navLabel(t('project.favorites'), favCount),
        },
        ...folders.map((f) => ({
          key: `folder:${f}`,
          icon: <FolderOpenOutlined />,
          label: navLabel(
            f,
            folderCount[f] || 0,
            <Dropdown trigger={['click']} menu={folderMenu(f)}>
              <EllipsisOutlined
                style={{ color: '#999' }}
                onClick={(e) => e.stopPropagation()}
              />
            </Dropdown>,
          ),
        })),
      ],
    },
    {
      type: 'group' as const,
      label: (
        <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <span>{t('project.envGroup')}</span>
          {canManageGroup ? (
            <Tooltip title={t("project.newEnvGroup")}>
              <PlusOutlined
                style={{ fontSize: 12, color: '#1677ff' }}
                onClick={(e) => {
                  e.stopPropagation()
                  openCreateGroup()
                }}
              />
            </Tooltip>
          ) : null}
        </span>
      ),
      children: groups.map((g) => ({
        key: `group:${g.id}`,
        label: navLabel(
          <Tag color={envColor(g.type)} style={{ margin: 0 }}>{groupDisplayName(g)}</Tag>,
          groupCount[g.id] || 0,
          canManageGroup ? (
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'policy', label: t('project.policy') },
                  ...(canDeleteGroup
                    ? [
                        {
                          key: 'delete',
                          label: t('common.delete'),
                          danger: true,
                          disabled:
                            (groupCount[g.id] || 0) > 0 ||
                            recycled.some((p) => p.group_id === g.id),
                        },
                      ]
                    : []),
                ],
                onClick: (info) => {
                  info.domEvent.stopPropagation()
                  if (info.key === 'policy') openGroupPolicy(g)
                  else if (info.key === 'delete') handleDeleteGroup(g)
                },
              }}
            >
              <EllipsisOutlined
                style={{ color: '#999' }}
                onClick={(e) => e.stopPropagation()}
              />
            </Dropdown>
          ) : null,
        ),
      })),
    },
  ]

  /** 手机上左侧导航改成下拉，避免占掉半屏。 */
  const navSelectOptions = [
    { value: 'all', label: `${t('project.allPipelines')} (${pipelines.length})` },
    { value: 'fav', label: `${t('project.favorites')} (${favCount})` },
    ...folders.map((f) => ({ value: `folder:${f}`, label: `${f}（${folderCount[f] || 0}）` })),
    ...groups.map((g) => ({
      value: `group:${g.id}`,
      label: `${groupDisplayName(g)}（${groupCount[g.id] || 0}）`,
    })),
  ]

  const colW = (key: string) => colWidths[key] ?? DEFAULT_WIDTHS[key] ?? 120
  const headerResize = (key: string) => ({
    width: colW(key),
    onResize: (next: number) => setColWidths((prev) => ({ ...prev, [key]: next })),
  })
  const shownCol = useMemo(() => new Set(['starred', ...visibleCols]), [visibleCols])

  const pipelineColumns = [
    {
      key: 'starred',
      title: '',
      dataIndex: 'starred',
      width: colW('starred'),
      onHeaderCell: () => headerResize('starred'),
      align: 'center' as const,
      render: (_v: boolean, r: Pipeline) => (
        <Tooltip title={r.starred ? t('project.unstar') : t('project.star')}>
          <span onClick={() => toggleStar(r)} style={{ cursor: 'pointer', fontSize: 16 }}>
            {r.starred ? (
              <StarFilled style={{ color: '#faad14' }} />
            ) : (
              <StarOutlined style={{ color: '#c0c0c0' }} />
            )}
          </span>
        </Tooltip>
      ),
    },
    {
      key: 'name',
      title: t('project.colName'),
      dataIndex: 'name',
      width: colW('name'),
      onHeaderCell: () => headerResize('name'),
      ellipsis: true,
      sorter: (a: Pipeline, b: Pipeline) => a.name.localeCompare(b.name, 'zh'),
      sortOrder: sorter.field === 'name' ? sorter.order : null,
      render: (v: string, r: Pipeline) => (
        <Tooltip title={v}>
          <a
            onClick={() => navigate(`/executions/${r.id}`)}
            style={{
              display: 'inline-block',
              maxWidth: '100%',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              verticalAlign: 'bottom',
            }}
          >
            <DeploymentUnitOutlined /> {v}
          </a>
        </Tooltip>
      ),
    },
    {
      key: 'group',
      title: t('project.colGroup'),
      dataIndex: 'group_id',
      width: colW('group'),
      onHeaderCell: () => headerResize('group'),
      // 免审批的生产流水线如果和普通的长得一样，「哪些线跳过了审批」就没人查得出来。
      // 审批要求被单独覆盖过的，必须在列表上一眼可见
      render: (v: number, r: Pipeline) => {
        const g = groupMap[v]
        return (
          <Space size={4} wrap>
            {g ? <Tag color={envColor(g.type)}>{groupDisplayName(g)}</Tag> : '-'}
            {r.approval_mode === 'exempt' && (
              <Tooltip title={t("project.exemptHint")}>
                <Tag color="volcano">{t("project.exempt")}</Tag>
              </Tooltip>
            )}
            {r.approval_mode === 'force' && (
              <Tooltip title={t("project.forceHint")}>
                <Tag color="gold">{t("project.forceApprove")}</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      key: 'version',
      title: t('project.colVersion'),
      dataIndex: 'version_label',
      width: colW('version'),
      onHeaderCell: () => headerResize('version'),
      render: (v: string) => (
        <span style={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 12 }}>
          {v || '—'}
        </span>
      ),
    },
    {
      key: 'exec',
      title: t('project.colExec'),
      dataIndex: 'last_release',
      width: colW('exec'),
      onHeaderCell: () => headerResize('exec'),
      render: (_v: unknown, r: Pipeline) => (
        <ExecStatusCell
          pipeline={r}
          onOpen={(p, releaseId) => navigate(`/executions/${p.id}/${releaseId}`)}
        />
      ),
    },
    {
      key: 'last_run',
      title: t('project.colLastRun'),
      dataIndex: 'last_run_at',
      width: colW('last_run'),
      onHeaderCell: () => headerResize('last_run'),
      sorter: (a: Pipeline, b: Pipeline) => ts(a.last_run_at) - ts(b.last_run_at),
      sortOrder: sorter.field === 'last_run_at' ? sorter.order : null,
      render: (v?: string) => <span style={{ color: v ? undefined : '#bbb', whiteSpace: 'nowrap' }}>{fmtTime(v)}</span>,
    },
    {
      key: 'updated',
      title: t('project.colUpdated'),
      dataIndex: 'updated_at',
      width: colW('updated'),
      onHeaderCell: () => headerResize('updated'),
      sorter: (a: Pipeline, b: Pipeline) => ts(a.updated_at || a.created_at) - ts(b.updated_at || b.created_at),
      sortOrder: sorter.field === 'updated_at' ? sorter.order : null,
      render: (_v: unknown, r: Pipeline) => (
        <span style={{ whiteSpace: 'nowrap' }}>{fmtTime(r.updated_at || r.created_at)}</span>
      ),
    },
    {
      key: 'creator',
      title: t('project.colCreator'),
      dataIndex: 'creator_name',
      width: colW('creator'),
      onHeaderCell: () => headerResize('creator'),
      render: (v?: string) => (
        <Space size={4}>
          <UserOutlined style={{ color: '#999' }} />
          <span>{v || '—'}</span>
        </Space>
      ),
    },
    {
      key: 'created',
      title: t('project.colCreated'),
      dataIndex: 'created_at',
      width: colW('created'),
      onHeaderCell: () => headerResize('created'),
      sorter: (a: Pipeline, b: Pipeline) => ts(a.created_at) - ts(b.created_at),
      sortOrder: sorter.field === 'created_at' ? sorter.order : null,
      render: (v?: string) => <span style={{ whiteSpace: 'nowrap' }}>{fmtTime(v)}</span>,
    },
    {
      key: 'actions',
      title: (
        <span style={{ display: 'inline-flex', alignItems: 'center' }}>
          {t('common.action')}
          <TableColumnSettings
            options={pipelineColOptions}
            visible={visibleCols}
            size={tableSize}
            onApply={(cols, sz) => {
              setVisibleCols(cols)
              setTableSize(sz)
            }}
          />
        </span>
      ),
      width: currentFolder ? Math.max(colW('actions'), 280) : colW('actions'),
      onHeaderCell: () => headerResize('actions'),
      fixed: 'right' as const,
      render: (_: unknown, r: Pipeline) => (
        <Space size={4} wrap={false}>
          {currentFolder ? (
            <Button size="small" onClick={() => saveFolder(r, '')}>
              {t('project.leaveFolder')}
            </Button>
          ) : null}
          {(user?.is_admin || r.can_update) && (
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() => navigate(`/pipeline/${r.id}/edit`)}
            >
              {t('common.edit')}
            </Button>
          )}
          {canCreate && (
            <Button
              size="small"
              icon={<CopyOutlined />}
              onClick={() => openDuplicate(r)}
            >
              {t('common.copy')}
            </Button>
          )}
          {(user?.is_admin || r.can_execute !== false) && (
            <Button
              size="small"
              type="primary"
              icon={<RocketOutlined />}
              onClick={() => navigate(`/executions/${r.id}`)}
            >
              {t('project.publish')}
            </Button>
          )}
          {(user?.is_admin || r.can_delete) && (
            <Popconfirm
              title={t('project.deletePipeline', { name: r.name })}
              description={t('project.deletePipelineDesc')}
              okText={t('common.delete')}
              okButtonProps={{ danger: true }}
              cancelText={t('common.cancel')}
              onConfirm={() => handleDeletePipeline(r)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ].filter((c) => shownCol.has(String(c.key)))

  const tableScrollX = pipelineColumns.reduce((sum, c) => sum + (Number(c.width) || 120), 0)

  const recycleColumns = [
    { title: t('project.recycleName'), dataIndex: 'name' },
    {
      title: t('project.recycleGroup'),
      dataIndex: 'group_id',
      render: (v: number) => {
        const g = groupMap[v]
        return g ? <Tag color={envColor(g.type)}>{groupDisplayName(g)}</Tag> : '-'
      },
    },
    {
      title: t('project.deletedAt'),
      dataIndex: 'deleted_at',
      render: (v?: string) => (v ? new Date(v).toLocaleString() : '-'),
    },
    {
      title: t('project.daysLeft'),
      dataIndex: 'days_left',
      render: (v?: number) =>
        v == null ? '-' : v <= 3 ? <Tag color="red">{v} {t('common.days')}</Tag> : <Tag>{v} {t('common.days')}</Tag>,
    },
    {
      title: t('common.action'),
      width: 200,
      render: (_: unknown, r: RecycledPipeline) => (
        <Space>
          <Button size="small" icon={<UndoOutlined />} onClick={() => handleRestore(r)}>
            {t('project.restore')}
          </Button>
          <Popconfirm
            title={t('project.purgeTitle', { name: r.name })}
            description={t('project.purgeDesc')}
            okText={t('project.purge')}
            okButtonProps={{ danger: true }}
            cancelText={t('common.cancel')}
            onConfirm={() => handlePurge(r)}
          >
            <Button size="small" danger icon={<DeleteOutlined />}>
              {t('project.purge')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Card
        title={
          <Select
            value={Number.isFinite(currentProjectId) ? currentProjectId : undefined}
            onChange={(id) => {
              if (id != null && String(id) !== String(projectId)) navigate(`/projects/${id}`)
            }}
            options={projectOptions}
            showSearch
            optionFilterProp="label"
            popupMatchSelectWidth={false}
            variant="borderless"
            open={projectSwitchOpen}
            onOpenChange={setProjectSwitchOpen}
            suffixIcon={
              <span
                role="button"
                aria-label={t("project.expandList")}
                onMouseDown={(e) => {
                  // 可搜索 Select 的箭头默认不接点击，点名字才展开
                  e.preventDefault()
                  e.stopPropagation()
                  setProjectSwitchOpen((v) => !v)
                }}
              >
                <DownOutlined />
              </span>
            }
            placeholder={t("project.pickProject")}
            className="qxci-project-switch"
            style={{ minWidth: isMobile ? 0 : 280, width: isMobile ? '100%' : undefined, maxWidth: '100%', fontWeight: 600, fontSize: 16, cursor: 'pointer' }}
            dropdownStyle={{ minWidth: isMobile ? undefined : 320 }}
          />
        }
        style={{ marginBottom: 16 }}
        size="small"
      >
        {project?.description}
      </Card>

      {!isMobile && <PmProjectCard projectId={Number(projectId)} />}

      <Card
        title={t("project.pipelines")}
        extra={
          isMobile ? null : (
          <Space>
            <Button icon={<DatabaseOutlined />} onClick={() => setRepoOpen(true)}>
              {t('project.repos')}
            </Button>
            {canUseRecycleBin && (
              <Button icon={<RestOutlined />} onClick={() => setRecycleOpen(true)}>
                {t('project.recycle')}{recycled.length ? ` (${recycled.length})` : ''}
              </Button>
            )}
          </Space>
          )
        }
        styles={{ body: isMobile ? { padding: 12 } : { display: 'flex', gap: 0, padding: 0 } }}
      >
        {!isMobile && (
        <div style={{ width: 232, flexShrink: 0, borderRight: '1px solid #f0f0f0', padding: '8px 0' }}>
          <Menu
            mode="inline"
            selectedKeys={[nav]}
            onClick={({ key }) => setNav(key)}
            items={navItems}
            style={{ borderInlineEnd: 'none' }}
          />
        </div>
        )}

        <div style={{ flex: 1, minWidth: 0, padding: isMobile ? 0 : 16 }}>
          {isMobile && (
            <Select
              value={nav}
              onChange={setNav}
              options={navSelectOptions}
              style={{ width: '100%', marginBottom: 12 }}
            />
          )}
          <div style={{ marginBottom: 16, display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            {canCreate && (
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  form.setFieldsValue({
                    group_id: currentGroupId || groups[0]?.id,
                    mode: 'form',
                  })
                  setOpen(true)
                }}
              >
                {t('project.newPipeline')}
              </Button>
            )}
            {!isMobile && (
            <CatalogTransferButtons
              projectId={Number(projectId)}
              pipelineIds={selectedIds}
              onImported={() => {
                queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
                queryClient.invalidateQueries({ queryKey: ['projects'] })
              }}
            />
            )}
            {currentFolder ? (
              <Button icon={<FolderOpenOutlined />} onClick={() => openAddToFolder(currentFolder)}>
                {t('project.addPipeline')}
              </Button>
            ) : null}
            {currentFolder && selectedIds.length > 0 ? (
              <Button onClick={removeSelectedFromFolder}>{t('project.removeFromFolder', { n: selectedIds.length })}</Button>
            ) : null}
            <Input.Search
              allowClear
              placeholder={t("project.searchPipeline")}
              style={{ width: isMobile ? '100%' : 240 }}
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
            />
            {!isMobile && (
            <span style={{ color: '#bbb', fontSize: 12 }}>{t('project.sortHint')}</span>
            )}
          </div>
          {pipelines.length === 0 ? (
            <Empty description={canCreate ? t('project.emptyCreate') : t('project.emptyNeedAccess')} />
          ) : visiblePipelines.length === 0 ? (
            <Empty
              description={
                currentFolder
                  ? t('project.emptyFolder', { name: currentFolder })
                  : t('project.emptyFilter')
              }
            />
          ) : isMobile ? (
            <>
              <div className="mobile-entity-list">
                {visiblePipelines
                  .slice((listPage - 1) * listPageSize, listPage * listPageSize)
                  .map((r) => {
                    const g = groupMap[r.group_id]
                    return (
                      <div key={r.id} className="mobile-entity-card">
                        <a
                          className="mobile-entity-title"
                          onClick={() => navigate(`/executions/${r.id}`)}
                        >
                          {r.name}
                        </a>
                        <div className="mobile-entity-meta">
                          {g ? <Tag color={envColor(g.type)}>{groupDisplayName(g)}</Tag> : null}
                        </div>
                        <div className="mobile-entity-meta">
                          <ExecStatusCell
                            pipeline={r}
                            onOpen={(p, releaseId) => navigate(`/executions/${p.id}/${releaseId}`)}
                          />
                        </div>
                        <div className="mobile-entity-actions">
                          {(user?.is_admin || r.can_execute !== false) && (
                            <Button
                              type="primary"
                              icon={<RocketOutlined />}
                              onClick={() => navigate(`/executions/${r.id}`)}
                            >
                              {t('project.publish')}
                            </Button>
                          )}
                        </div>
                      </div>
                    )
                  })}
              </div>
              <Pagination
                current={listPage}
                pageSize={listPageSize}
                total={visiblePipelines.length}
                size="small"
                simple
                style={{ marginTop: 12, textAlign: 'right' }}
                onChange={(p, ps) => {
                  setListPage(p)
                  setListPageSize(ps)
                }}
              />
            </>
          ) : (
            <Table
              rowKey="id"
              size={tableSize}
              columns={pipelineColumns}
              dataSource={visiblePipelines}
              rootClassName="qxci-table"
              components={{ header: { cell: ResizableTitle } }}
              rowSelection={{
                selectedRowKeys: selectedIds,
                onChange: (keys) => setSelectedIds(keys.map(Number)),
                preserveSelectedRowKeys: true,
              }}
              pagination={{
                current: listPage,
                pageSize: listPageSize,
                size: 'small',
                position: ['bottomLeft'],
                showSizeChanger: true,
                showLessItems: true,
                pageSizeOptions: [10, 20, 50, 100],
                showTotal: (n) => t('project.total', { n }),
                locale: { items_per_page: '' },
                onChange: (p, ps) => {
                  setListPage(p)
                  setListPageSize(ps)
                },
              }}
              scroll={{ x: tableScrollX }}
              onChange={(_p, _f, s) => {
                const ss = Array.isArray(s) ? s[0] : s
                setSorter(ss?.order ? { field: ss.field as string, order: ss.order } : {})
              }}
            />
          )}
        </div>
      </Card>

      {/* 代码库：低频，抽屉打开 */}
      <Drawer title={t("project.repos")} width={isMobile ? '100%' : 900} open={repoOpen} onClose={() => setRepoOpen(false)} destroyOnClose>
        <RepositoryPanel projectId={Number(projectId)} />
      </Drawer>

      {/* 回收站：低频，抽屉打开。没有删除权的人看不到入口 */}
      <Drawer
        title={recycled.length ? `${t('project.recycle')} (${recycled.length})` : t('project.recycle')}
        width={isMobile ? '100%' : 780}
        open={recycleOpen}
        onClose={() => setRecycleOpen(false)}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("project.recycleAlert")}
        />
        {recycled.length === 0 ? (
          <Empty description={t("project.recycleEmpty")} />
        ) : (
          <DataTable chromeKey="pipeline-recycle" rowKey="id" columns={recycleColumns} dataSource={recycled} pagination={false} />
        )}
      </Drawer>

      {/* 新建流水线弹窗（蓝盾风格：左侧模板 + 右侧表单）*/}
      <Modal
        title={t("project.createPipeline")}
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={isMobile ? '100%' : 920}
        styles={{ content: { maxWidth: 'calc(100vw - 16px)' } }}
        destroyOnClose
      >
        <div style={{ display: 'flex', flexDirection: isMobile ? 'column' : 'row', gap: 24, minHeight: isMobile ? 0 : 360 }}>
          {/* 左侧模板列表 */}
          <div style={{ flex: 1, borderRight: isMobile ? 'none' : '1px solid #f0f0f0', paddingRight: isMobile ? 0 : 24 }}>
            <div style={{ fontSize: 13, color: '#666', marginBottom: 12 }}>
              {t('project.templates', { n: 1 })}
            </div>
            <div
              style={{
                border: '2px solid #1677ff',
                borderRadius: 6,
                padding: 16,
                cursor: 'pointer',
                position: 'relative',
                background: '#f0f5ff',
              }}
            >
              <div
                style={{
                  position: 'absolute',
                  top: 8,
                  right: 8,
                  width: 18,
                  height: 18,
                  borderRadius: '50%',
                  background: '#1677ff',
                  color: '#fff',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 12,
                }}
              >
                ✓
              </div>
              <div
                style={{
                  width: 64,
                  height: 64,
                  margin: '0 auto 8px',
                  background: '#fff',
                  borderRadius: 4,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 32,
                  border: '1px solid #d9d9d9',
                }}
              >
                <AppstoreOutlined />
              </div>
              <div style={{ textAlign: 'center', color: '#1677ff', fontWeight: 600 }}>
                Blank
              </div>
            </div>
          </div>

          {/* 右侧表单 */}
          <div style={{ flex: 1.2 }}>
            <Form
              form={form}
              layout="vertical"
              onFinish={(v) =>
                handleCreatePipeline(v).then(() => {
                  // 关闭弹窗
                  setOpen(false)
                  form.resetFields()
                })
              }
              initialValues={{
                group_id: groups[0]?.id,
                mode: 'form',
                type: 'free',
              }}
            >
              <Form.Item
                name="name"
                label={t("project.colName")}
                rules={[
                  { required: true, message: t('project.pipelineNameRequired') },
                  { max: 40, message: t('project.nameMax') },
                ]}
              >
                <Input placeholder={t("project.namePlaceholder")} maxLength={40} />
              </Form.Item>

              <Form.Item label={t("project.type")} name="type">
                <Radio.Group>
                  <Radio value="free">{t("project.freeMode")}</Radio>
                </Radio.Group>
              </Form.Item>

              <Form.Item label={t("project.arrangeMode")} name="mode">
                <Radio.Group>
                  <Radio value="form">{t("project.listMode")}</Radio>
                  <Radio value="canvas">{t("project.canvasMode")}</Radio>
                </Radio.Group>
              </Form.Item>

              <Form.Item label={t("project.envGroup")} name="group_id" rules={[{ required: true, message: t("project.pickEnv") }]}>
                <Select
                  placeholder={t("project.pickEnvGroup")}
                  options={groups.map((g) => ({
                    label: groupOptionLabel(g),
                    value: g.id,
                  }))}
                />
              </Form.Item>
              {currentFolder ? (
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message={t('project.willJoinFolder', { name: currentFolder })}
                  description={t("project.folderNoAcl")}
                />
              ) : null}

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 24 }}>
                <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
                <Button type="primary" htmlType="submit">
                  {t('project.create')}
                </Button>
              </div>
            </Form>
          </div>
        </div>
      </Modal>

      {/* 复制流水线弹窗：整份 yaml 复用，A/B 互相独立 */}
      <Modal
        title={t('project.dupTitle', { name: dupSource?.name ?? '' })}
        open={dupOpen}
        onOk={handleDuplicate}
        onCancel={() => setDupOpen(false)}
        okText={t("common.ok")}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <div style={{ marginBottom: 12, color: '#999', fontSize: 13 }}>
          {t('project.dupHint')}
        </div>
        <Form form={dupForm} layout="vertical">
          <Form.Item
            name="name"
            label={t("project.dupName")}
            rules={[{ required: true, message: t('project.nameRequired') }]}
            extra={t("project.dupNameExtra")}
          >
            <Input placeholder={t("project.dupNamePlaceholder")} />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input.TextArea rows={2} placeholder={t("project.optional")} />
          </Form.Item>
          <Form.Item name="group_id" label={t("project.envGroup")} rules={[{ required: true }]}>
            <Select
              placeholder={t("project.pickGroupDefault")}
              options={groups.map((g) => ({
                label: groupOptionLabel(g),
                value: g.id,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* 环境分组审批策略：小团队可开自审与应急跳审，避免无人可审时卡住上线 */}
      <Modal
        title={t('project.policyTitle', { name: policyGroup?.name ?? '' })}
        open={!!policyGroup}
        onOk={handleSaveGroupPolicy}
        onCancel={() => setPolicyGroup(null)}
        okText={t("common.save")}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("project.policyAlert")}
          description={t("project.policyAlertDesc")}
        />
        <Form form={groupForm} layout="horizontal" labelCol={{ span: 10 }}>
          <Form.Item name="approval_required" label={t("project.requireApproval")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            name="allow_self_approval"
            label={t("project.allowSelf")}
            valuePropName="checked"
            extra={t("project.allowSelfExtra")}
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="allow_emergency_bypass"
            label={t("project.allowBypass")}
            valuePropName="checked"
            extra={t("project.allowBypassExtra")}
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="pm_approval_required"
            label={t("project.pmRequired")}
            valuePropName="checked"
            extra={t("project.pmRequiredExtra")}
          >
            <Switch />
          </Form.Item>
          <Form.Item name="change_window" label={t("project.changeWindow")} extra={t("project.changeWindowExtra")}>
            <Input placeholder={t("project.changeWindowPlaceholder")} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 新建环境分组：内置码 + 自定义 slug，有流水线的组不能删 */}
      <Modal
        title={t("project.newGroup")}
        open={createGroupOpen}
        onOk={handleCreateGroup}
        onCancel={() => setCreateGroupOpen(false)}
        okText={t("common.create")}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("project.envCodeMustMatch")}
          description={t("project.envCodeMustMatchDesc")}
        />
        <Form
          form={createGroupForm}
          layout="vertical"
          onValuesChange={(changed) => {
            if ('type' in changed && changed.type && changed.type !== '__custom__') {
              const need = defaultApprovalRequired(changed.type)
              const prevName = String(createGroupForm.getFieldValue('name') || '')
              const patch: Record<string, unknown> = {
                approval_required: need,
                allow_self_approval: need,
              }
              if (isFactoryGroupName(prevName)) {
                patch.name = envLabel(changed.type)
              }
              createGroupForm.setFieldsValue(patch)
            }
          }}
        >
          <Form.Item name="name" label={t("project.groupName")} rules={[{ required: true, message: t("project.fillGroupName") }]}>
            <Input placeholder={t("project.groupNamePlaceholder")} maxLength={40} />
          </Form.Item>
          <Form.Item name="type" label={t("project.envCode")} rules={[{ required: true, message: t("project.pickEnv") }]}>
            <Select
              options={[...envOptions(), { value: '__custom__', label: t('project.customCode') }]}
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.type !== c.type}>
            {({ getFieldValue }) =>
              getFieldValue('type') === '__custom__' ? (
                <Form.Item
                  name="custom_type"
                  label={t("project.customCode")}
                  rules={[
                    { required: true, message: t('project.fillGroupName') },
                    { pattern: ENV_SLUG, message: t('project.envSlugRule') },
                  ]}
                >
                  <Input placeholder={t("project.customCodePlaceholder")} />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          <Form.Item name="approval_required" label={t("project.requireApproval")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="allow_self_approval" label={t("project.allowSelf")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="allow_emergency_bypass" label={t("project.allowBypass")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="pm_approval_required" label={t("project.pmRequired")} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* 个人分组：新建空组 / 重命名。加流水线用下面的勾选表格。 */}
      <Modal
        title={folderModal === 'rename' ? t('project.renameFolder', { name: renameFrom }) : t('project.newFolder')}
        open={folderModal === 'create' || folderModal === 'rename'}
        onOk={async () => {
          const name = folderInput.trim()
          if (folderModal === 'create') {
            if (!name) {
              message.warning(t('project.fillGroupName'))
              return
            }
            await createFolder(name)
            setNav(`folder:${name}`)
            setFolderModal(null)
            message.success(t('project.folderCreated', { name }))
            return
          }
          if (folderModal === 'rename') {
            if (!name) {
              message.warning(t('project.fillGroupName'))
              return
            }
            const names = await put<string[]>('/personal-folders', {
              project_id: Number(projectId),
              from: renameFrom,
              to: name,
            })
            queryClient.setQueryData(['personal-folders', projectId], names)
            queryClient.invalidateQueries({ queryKey: ['personal-folders', projectId] })
            queryClient.invalidateQueries({ queryKey: ['pipelines', projectId] })
            if (nav === `folder:${renameFrom}`) setNav(`folder:${name}`)
            setFolderModal(null)
            message.success(t('project.folderRenamed'))
          }
        }}
        onCancel={() => setFolderModal(null)}
        okText={t("common.ok")}
        cancelText={t('common.cancel')}
        destroyOnClose
      >
        <div style={{ marginBottom: 8, color: '#999', fontSize: 13 }}>
          {t('project.folderHint')}
        </div>
        <Input
          placeholder={t("project.folderPlaceholder")}
          value={folderInput}
          maxLength={64}
          autoFocus
          onChange={(e) => setFolderInput(e.target.value)}
          onPressEnter={() => {
            const ok = document.querySelector('.ant-modal-footer .ant-btn-primary') as HTMLButtonElement | null
            ok?.click()
          }}
        />
      </Modal>

      <AddPipelinesModal
        open={folderModal === 'add'}
        folder={folderAddTo}
        pipelines={pipelines}
        groups={groups}
        selectedIds={folderAddIds}
        saving={folderAddSaving}
        onChangeIds={setFolderAddIds}
        onOk={async () => {
          setFolderAddSaving(true)
          try {
            const ok = await addPipelinesToFolder(folderAddTo, folderAddIds)
            if (ok) {
              setFolderModal(null)
              setFolderAddIds([])
              setNav(`folder:${folderAddTo}`)
            }
          } finally {
            setFolderAddSaving(false)
          }
        }}
        onCancel={() => {
          setFolderModal(null)
          setFolderAddIds([])
        }}
      />
    </div>
  )
}

function PmProjectCard({ projectId }: { projectId: number }) {
  const t = useT()
  const qc = useQueryClient()
  const { data } = useQuery({
    queryKey: ['pm-members', projectId],
    queryFn: () =>
      get<{ pm_enabled: boolean; members: { user_id: number; display_name: string; username: string }[]; can_manage: boolean }>(
        `/pm/projects/${projectId}/members`,
      ),
    enabled: Number.isFinite(projectId),
  })
  const manage = !!data?.can_manage
  const [kw, setKw] = useState('')
  const { data: dir = [] } = useQuery({
    queryKey: ['pm-dir', kw],
    queryFn: () => get<{ id: number; display_name: string; username: string }[]>('/pm/directory', { q: kw }),
    enabled: manage,
  })
  const enabled = !!data?.pm_enabled
  const ids = (data?.members || []).map((m) => m.user_id)
  const saveEnabled = async (on: boolean) => {
    await put(`/pm/projects/${projectId}/settings`, { pm_enabled: on })
    qc.invalidateQueries({ queryKey: ['pm-members', projectId] })
    message.success(on ? t('project.pmOn') : t('project.pmOff'))
  }
  const saveMembers = async (userIds: number[]) => {
    await put(`/pm/projects/${projectId}/members`, { user_ids: userIds })
    qc.invalidateQueries({ queryKey: ['pm-members', projectId] })
    message.success(t('project.pmSaved'))
  }
  return (
    <Card
      size="small"
      style={{ marginBottom: 16 }}
      title={
        <Space>
          <TeamOutlined />
          {t('project.pmTitle')}
        </Space>
      }
      extra={
        <Switch
          checked={enabled}
          disabled={!manage}
          checkedChildren={t("project.participate")}
          unCheckedChildren={t("project.closed")}
          onChange={saveEnabled}
        />
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("project.pmHint")}
      />
      <Select
        mode="multiple"
        style={{ width: '100%' }}
        placeholder={t("project.pmPlaceholder")}
        value={ids}
        disabled={!manage}
        filterOption={false}
        onSearch={setKw}
        onChange={saveMembers}
        options={[
          ...(data?.members || []).map((m) => ({ value: m.user_id, label: m.display_name || m.username })),
          ...dir
            .filter((u) => !ids.includes(u.id))
            .map((u) => ({ value: u.id, label: u.display_name || u.username })),
        ]}
      />
    </Card>
  )
}
