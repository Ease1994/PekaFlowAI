import { useEffect, useMemo, useState } from 'react'
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Input,
  List,
  Popconfirm,
  Select,
  Space,
  Tabs,
  Tag,
  message,
} from 'antd'
import { CheckOutlined, CloseOutlined, DeleteOutlined, PlusOutlined, SearchOutlined, StopOutlined, UserOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { get, post } from '@/api/client'
import RolePanel from '@/components/project/RolePanel'
import DataTable from '@/components/DataTable'
import GrantPermissionModal, {
  type GrantKind,
  type GrantPayload,
} from '@/components/GrantPermissionModal'
import { useIsMobile } from '@/hooks/useIsMobile'
import MenuManagement from '@/pages/MenuManagement'
import AccessApplyPanel from '@/modules/access/AccessApplyPanel'
import { useAuthStore } from '@/stores/auth'
import { t, useT } from '@/i18n'
import { groupDisplayName } from '@/env'

interface UserItem { id: number; username: string; display_name: string; is_admin: boolean }
interface ProjectItem { id: number; name: string; code: string }
interface GroupItem { id: number; name: string; type: string; project_id: number }
interface NodeItem { id: number; name: string; env?: string; allow_paths?: string[] }
interface NodeGroupItem { id: number; name: string; description: string; member_count: number; agent_ids?: number[] }
interface PermissionItem {
  id: number
  user_id: number
  username: string
  display_name: string
  resource_type: string
  resource_id: number
  resource_name: string
  action: string
  action_label: string
  effect: string
}
interface Application {
  id: number
  applicant_id: number
  applicant: string
  project_name: string
  group_name: string
  pipeline_name: string
  pipeline_id: number
  apply_type: string
  role_id?: number
  role_name?: string
  scope_label?: string
  scope_text?: string
  reason: string
  status: string
  source: string
  granted_actions: string
  granted_action_list: string[]
  review_comment: string
  reviewer: string
  created_at: string
  reviewed_at: string
}

/** 资源类型码 → 界面句子。渲染时再取，语言切换才不会停在导入时的文案。 */
function typeLabel(v: string): string {
  const keys: Record<string, string> = {
    project: 'acl.project',
    group: 'acl.group',
    pipeline: 'acl.pipeline',
    node: 'acl.node',
    node_group: 'acl.nodeGroup',
  }
  return keys[v] ? t(keys[v]) : v
}

/** 动作码 → 界面句子。查看/创建等与 acl 共用。 */
function actionLabel(a: string): string {
  const keys: Record<string, string> = {
    read: 'acl.view',
    create: 'acl.create',
    update: 'acl.update',
    delete: 'acl.delete',
    execute: 'acl.execute',
    approve: 'acl.approve',
    approval_exempt: 'acl.exempt',
    deploy: 'acl.pushFile',
  }
  return keys[a] ? t(keys[a]) : a
}

/** 申请单状态：颜色固定，句子走 t()。 */
function statusMeta(s: string): { color: string; text: string } {
  const map: Record<string, { color: string; text: string }> = {
    pending: { color: 'orange', text: t('status.pending') },
    approved: { color: 'success', text: t('perm.approved') },
    rejected: { color: 'error', text: t('perm.rejected') },
    cancelled: { color: 'default', text: t('perm.revoked') },
  }
  return map[s] || { color: 'default', text: s }
}

/** 申请来源：AI / 页面 / API。 */
function sourceMeta(s: string): { color: string; text: string } {
  const map: Record<string, { color: string; text: string }> = {
    ai: { color: 'blue', text: t('perm.sourceAi') },
    web: { color: 'default', text: t('perm.sourceWeb') },
    api: { color: 'cyan', text: t('perm.sourceApi') },
  }
  return map[s] || { color: 'default', text: s }
}

type GroupedPerm = PermissionItem & { actions: string[]; ids: number[] }
/** 节点授权落在这两类资源上，和流水线的项目/分组/流水线分开列。 */
const NODE_TYPES = new Set(['node', 'node_group'])

/**
 * 写类动作会带上查看：没有查看就进不了流水线页。
 */

function withRead(actions: string[]) {
  if (actions.length && !actions.includes('read')) return ['read', ...actions]
  return actions
}

/** 审核抽屉里可勾的动作。label 在渲染时取。 */
function reviewActionOptions() {
  return [
    { value: 'read', label: t('acl.view') },
    { value: 'create', label: t('acl.create') },
    { value: 'update', label: t('acl.update') },
    { value: 'delete', label: t('acl.delete') },
    { value: 'execute', label: t('acl.execute') },
    { value: 'approve', label: t('acl.approve') },
    { value: 'approval_exempt', label: t('acl.exempt') },
  ]
}

/** 列表范围列：角色单走 scope_text，资源单仍是项目/分组/流水线。 */
function scopeText(row: Application) {
  if (row.scope_text) return row.scope_text
  if (row.apply_type === 'role') return t('perm.scopeRole', { project: row.project_name, role: row.role_name || row.pipeline_name })
  return `${row.project_name} / ${groupDisplayName({ name: row.group_name })} / ${row.pipeline_name}`
}

/** 角色申请 vs 资源权限。 */
function applyKind(row: Application) {
  return row.scope_label || (row.apply_type === 'role' ? t('perm.roleGrant') : t('perm.resourceGrant'))
}

export default function PermissionManagement() {
  const t = useT()
  const qc = useQueryClient()
  const isMobile = useIsMobile()
  const user = useAuthStore((s) => s.user)
  const isAdmin = !!user?.is_admin
  const [params, setParams] = useSearchParams()
  const tab = params.get('tab') || 'apply'
  const focusId = Number(params.get('id') || 0)
  const [userKw, setUserKw] = useState('')
  const [selectedUserId, setSelectedUserId] = useState<number>()
  /** 添加授权弹窗是否打开。 */
  const [grantOpen, setGrantOpen] = useState(false)
  /** 弹窗授流水线还是节点，跟当前页签一致。 */
  const [grantKind, setGrantKind] = useState<GrantKind>('pipeline')
  const [roleProjectId, setRoleProjectId] = useState<number>()
  const [review, setReview] = useState<Application | null>(null)
  const [reviewActions, setReviewActions] = useState<string[]>(['read', 'execute'])
  const [reviewComment, setReviewComment] = useState('')
  /** 管理员在申请记录里可只看自己的单。 */
  const [historyMineOnly, setHistoryMineOnly] = useState(false)

  const adminGrantTab = isAdmin && (tab === 'users' || tab === 'nodes' || tab === 'roles')
  const { data: users = [] } = useQuery({
    queryKey: ['users'],
    queryFn: () => get<UserItem[]>('/users'),
    enabled: adminGrantTab,
  })
  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<ProjectItem[]>('/projects'),
    enabled: isAdmin,
  })
  const { data: groups = [] } = useQuery({
    queryKey: ['groups'],
    queryFn: () => get<GroupItem[]>('/groups'),
    enabled: isAdmin,
  })
  const { data: nodes = [] } = useQuery({
    queryKey: ['agents', 'node'],
    queryFn: () => get<NodeItem[]>('/agents?role=node'),
    enabled: isAdmin && tab === 'nodes',
  })
  const { data: nodeGroups = [] } = useQuery({
    queryKey: ['node-groups'],
    queryFn: () => get<NodeGroupItem[]>('/node-groups'),
    enabled: isAdmin && tab === 'nodes',
  })
  // 只拉当前选中用户的授权。以前是把整张权限表拉回来再在前端 filter，
  // 用户和资源一多就是几十万行走网络
  const { data: userPerms = [] } = useQuery({
    queryKey: ['permissions', selectedUserId],
    queryFn: () => get<PermissionItem[]>('/permissions', { user_id: selectedUserId }),
    enabled: isAdmin && !!selectedUserId,
  })
  const { data: pending = [] } = useQuery({
    queryKey: ['access-applications', 'pending'],
    queryFn: () => get<Application[]>('/access/applications', { scope: 'pending' }),
  })
  const historyScope = isAdmin && !historyMineOnly ? 'all' : 'mine'
  const { data: historyApps = [] } = useQuery({
    queryKey: ['access-applications', historyScope],
    queryFn: () => get<Application[]>('/access/applications', { scope: historyScope }),
    enabled: tab === 'history' || (!!focusId && tab !== 'pending'),
  })

  const filteredUsers = useMemo(() => {
    const kw = userKw.trim().toLowerCase()
    return users.filter((u) => {
      if (u.is_admin) return false
      if (!kw) return true
      return `${u.display_name} ${u.username}`.toLowerCase().includes(kw)
    })
  }, [users, userKw])

  const selectedUser = users.find((u) => u.id === selectedUserId)
  const grouped = useMemo(() => {
    const map = new Map<string, GroupedPerm>()
    for (const p of userPerms) {
      if (p.resource_type === 'console') continue
      const key = `${p.resource_type}:${p.resource_id}`
      const cur = map.get(key)
      if (cur) {
        cur.actions.push(p.action)
        cur.ids.push(p.id)
      } else {
        map.set(key, { ...p, actions: [p.action], ids: [p.id] })
      }
    }
    return [...map.values()]
  }, [userPerms])
  /** 用户授权页签只看项目/分组/流水线。 */
  const pipelinePerms = grouped.filter((p) => !NODE_TYPES.has(p.resource_type))
  /** 节点授权页签只看节点和节点组。 */
  const nodePerms = grouped.filter((p) => NODE_TYPES.has(p.resource_type))

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['permissions'] })
    qc.invalidateQueries({ queryKey: ['access-applications'] })
    qc.invalidateQueries({ queryKey: ['access-me'] })
    qc.invalidateQueries({ queryKey: ['notices'] })
    qc.invalidateQueries({ queryKey: ['notices-brief'] })
    qc.invalidateQueries({ queryKey: ['notices-unread'] })
  }

  const grantMut = useMutation({
    mutationFn: (payload: GrantPayload) =>
      post<{ created: number }>('/permissions/batch', {
        user_ids: selectedUserId ? [selectedUserId] : [],
        resource_type: payload.resource_type,
        resource_ids: payload.resource_ids,
        actions: payload.actions,
      }),
    onSuccess: (r) => {
      message.success(t('perm.grantedN', { n: r.created }))
      setGrantOpen(false)
      invalidate()
    },
  })

  const revokeMut = useMutation({
    mutationFn: (ids: number[]) => post<{ deleted: number }>('/permissions/revoke', { permission_ids: ids }),
    onSuccess: (r) => {
      message.success(t('perm.removedN', { n: r.deleted }))
      invalidate()
    },
  })

  const reviewMut = useMutation({
    mutationFn: (body: { id: number; action: 'approve' | 'reject'; actions?: string[]; comment?: string }) =>
      post<Application>(`/access/applications/${body.id}/${body.action}`, {
        comment: body.comment || '',
        actions: body.actions,
      }),
    onSuccess: () => {
      message.success(t('perm.handled'))
      setReview(null)
      invalidate()
    },
  })

  const cancelMut = useMutation({
    mutationFn: (id: number) => post<Application>(`/access/applications/${id}/cancel`),
    onSuccess: () => {
      message.success(t('perm.cancelled'))
      invalidate()
    },
  })

  const openReview = (row: Application) => {
    setReview(row)
    const picked = (row.granted_action_list || []).filter(Boolean)
    setReviewActions(picked.length ? picked : ['read', 'execute'])
    setReviewComment('')
  }

  useEffect(() => {
    const adminOnly = new Set(['users', 'nodes', 'menus', 'roles'])
    if (!isAdmin && adminOnly.has(tab)) {
      setParams({ tab: 'apply' }, { replace: true })
    }
  }, [isAdmin, tab, setParams])

  useEffect(() => {
    if (!focusId) return
    const row = pending.find((p) => p.id === focusId) || historyApps.find((p) => p.id === focusId)
    if (!row) return
    if (row.status === 'pending') openReview(row)
    const next = new URLSearchParams(params)
    next.delete('id')
    setParams(next, { replace: true })
  }, [focusId, pending, historyApps])

  /**
   * 打开添加授权。kind 由当前页签决定：用户授权走流水线联动，节点授权走节点组联动。
   */
  const openGrant = (kind: GrantKind) => {
    setGrantKind(kind)
    setGrantOpen(true)
  }

  /** 授权列表列：类型、资源名、已授操作、整组移除。 */
  const permColumns = [
    {
      title: t('common.type'),
      dataIndex: 'resource_type',
      width: 90,
      render: (v: string) => <Tag>{typeLabel(v)}</Tag>,
    },
    { title: t('perm.resource'), dataIndex: 'resource_name' },
    {
      title: t('perm.permissions'),
      render: (_: unknown, r: GroupedPerm) =>
        r.actions.map((a) => (
          <Tag color="cyan" key={a}>
            {actionLabel(a)}
          </Tag>
        )),
    },
    {
      title: t('common.action'),
      width: 80,
      render: (_: unknown, r: GroupedPerm) => (
        <Popconfirm title={t('perm.revokeAll')} onConfirm={() => revokeMut.mutate(r.ids)}>
          <Button type="link" size="small" danger icon={<DeleteOutlined />}>
            {t('perm.remove')}
          </Button>
        </Popconfirm>
      ),
    },
  ]

  /**
   * 左侧选人、右侧看授权。用户授权和节点授权共用这一套，只是表格数据不同。
   */
  const userGrantPanel = (kind: GrantKind) => (
    <div className="perm-user-grant">
      <Card size="small" className="perm-user-list" title={t('perm.user')}>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder={t('perm.searchUser')}
          value={userKw}
          onChange={(e) => setUserKw(e.target.value)}
          style={{ marginBottom: 8 }}
        />
        <List
          size="small"
          dataSource={filteredUsers}
          locale={{ emptyText: t('perm.noMatchUser') }}
          renderItem={(u) => (
            <List.Item
              style={{
                cursor: 'pointer',
                background: u.id === selectedUserId ? '#e6f4ff' : undefined,
                padding: '8px 10px',
                borderRadius: 6,
              }}
              onClick={() => setSelectedUserId(u.id)}
            >
              <List.Item.Meta
                avatar={<UserOutlined />}
                title={u.display_name || u.username}
                description={u.username}
              />
            </List.Item>
          )}
        />
      </Card>
      <Card
        size="small"
        style={{ flex: 1 }}
        title={selectedUser ? t('perm.userPerms', { name: selectedUser.display_name || selectedUser.username }) : t('perm.pickLeftUser')}
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!selectedUserId}
            onClick={() => openGrant(kind)}
          >
            {t('perm.addGrant')}
          </Button>
        }
      >
        {!selectedUserId ? (
          <Empty description={t('perm.emptyPickUser')} />
        ) : (
          <DataTable<GroupedPerm>
            chromeKey={kind === 'node' ? 'perm-nodes' : 'perm-grouped'}
            rowKey={(r) => `${r.resource_type}-${r.resource_id}`}
            size="small"
            dataSource={kind === 'node' ? nodePerms : pipelinePerms}
            pagination={false}
            locale={{
              emptyText: kind === 'node' ? t('perm.emptyNodePerm') : t('perm.emptyPipePerm'),
            }}
            columns={permColumns}
          />
        )}
      </Card>
    </div>
  )

  return (
    <div>
      <Tabs
        activeKey={tab}
        onChange={(k) => setParams({ tab: k })}
        items={[
          {
            key: 'apply',
            label: t('perm.tabApply'),
            children: <AccessApplyPanel />,
          },
          {
            key: 'history',
            label: t('perm.tabHistory'),
            children: (
              <Card
                extra={
                  isAdmin ? (
                    <Checkbox checked={historyMineOnly} onChange={(e) => setHistoryMineOnly(e.target.checked)}>
                      {t('perm.mineOnly')}
                    </Checkbox>
                  ) : null
                }
              >
                <DataTable
                  chromeKey="perm-apps"
                  rowKey="id"
                  size="small"
                  dataSource={historyApps}
                  columns={[
                    { title: t('perm.colId'), dataIndex: 'id', width: 70 },
                    { title: t('perm.applicant'), dataIndex: 'applicant', width: 120 },
                    {
                      title: t('perm.scope'),
                      render: (_: unknown, r: Application) => scopeText(r),
                    },
                    {
                      title: t('common.type'),
                      width: 100,
                      render: (_: unknown, r: Application) => applyKind(r),
                    },
                    {
                      title: t('common.status'),
                      dataIndex: 'status',
                      width: 90,
                      render: (s: string) => {
                        const meta = statusMeta(s)
                        return <Tag color={meta.color}>{meta.text}</Tag>
                      },
                    },
                    {
                      title: t('perm.granted'),
                      width: 180,
                      render: (_: unknown, r: Application) => {
                        if (r.status !== 'approved') return '—'
                        if (r.apply_type === 'role') return <Tag color="purple">{r.role_name || r.pipeline_name}</Tag>
                        return (r.granted_action_list || []).map((a) => <Tag key={a}>{actionLabel(a)}</Tag>)
                      },
                    },
                    {
                      title: t('perm.source'),
                      dataIndex: 'source',
                      width: 90,
                      render: (s: string) => {
                        const meta = sourceMeta(s)
                        return <Tag color={meta.color}>{meta.text}</Tag>
                      },
                    },
                    { title: t('perm.reviewer'), dataIndex: 'reviewer', width: 100 },
                    { title: t('perm.comment'), dataIndex: 'review_comment', ellipsis: true },
                    {
                      title: t('common.action'),
                      width: 90,
                      render: (_: unknown, r: Application) =>
                        r.status === 'pending' && r.applicant_id === user?.id ? (
                          <Popconfirm title={t('perm.cancelApply')} onConfirm={() => cancelMut.mutate(r.id)}>
                            <Button type="link" size="small" icon={<StopOutlined />}>
                              {t('perm.cancel')}
                            </Button>
                          </Popconfirm>
                        ) : null,
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'pending',
            label: (
              <span>
                {t('perm.tabPending')} <Badge count={pending.length} size="small" offset={[6, -2]} />
              </span>
            ),
            children: (
              <Card>
                <DataTable
                  chromeKey="perm-pending"
                  rowKey="id"
                  size="small"
                  scroll={{ x: 1100 }}
                  dataSource={pending}
                  locale={{ emptyText: t('perm.emptyPending') }}
                  columns={[
                    { title: t('perm.colId'), dataIndex: 'id', width: 70 },
                    { title: t('perm.applicant'), dataIndex: 'applicant', width: 120 },
                    {
                      title: t('perm.applyScope'),
                      render: (_: unknown, r: Application) => scopeText(r),
                    },
                    {
                      title: t('perm.proposed'),
                      width: 180,
                      render: (_: unknown, r: Application) =>
                        r.apply_type === 'role' ? (
                          <Tag color="purple">{r.role_name || r.pipeline_name}</Tag>
                        ) : (
                          (r.granted_action_list || []).map((a) => (
                            <Tag key={a}>{actionLabel(a)}</Tag>
                          ))
                        ),
                    },
                    {
                      title: t('perm.source'),
                      dataIndex: 'source',
                      width: 90,
                      render: (s: string) => {
                        const meta = sourceMeta(s)
                        return <Tag color={meta.color}>{meta.text}</Tag>
                      },
                    },
                    { title: t('perm.reason'), dataIndex: 'reason', ellipsis: true },
                    { title: t('perm.submittedAt'), dataIndex: 'created_at', width: 170 },
                    {
                      title: t('common.action'),
                      width: 90,
                      render: (_: unknown, r: Application) => (
                        <Button type="link" size="small" onClick={() => openReview(r)}>
                          {t('perm.review')}
                        </Button>
                      ),
                    },
                  ]}
                />
              </Card>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: 'users',
                  label: t('perm.tabUsers'),
                  children: userGrantPanel('pipeline'),
                },
                {
                  key: 'nodes',
                  label: t('perm.tabNodes'),
                  children: userGrantPanel('node'),
                },
                {
                  key: 'menus',
                  label: t('perm.tabMenus'),
                  children: (
                    <Card>
                      <MenuManagement />
                    </Card>
                  ),
                },
                {
                  key: 'roles',
                  label: t('perm.tabRoles'),
                  children: (
                    <Card
                      title={
                        <Space>
                          <span>{t('perm.tabRoles')}</span>
                          <Select
                            style={{ width: 260 }}
                            showSearch
                            optionFilterProp="label"
                            placeholder={t('project.pickProject')}
                            value={roleProjectId}
                            onChange={setRoleProjectId}
                            options={projects.map((p) => ({ value: p.id, label: t('perm.projectWithCode', { name: p.name, code: p.code }) }))}
                          />
                        </Space>
                      }
                    >
                      <div style={{ marginBottom: 12, color: '#999', fontSize: 12 }}>
                        {t('perm.rolesHint')}
                      </div>
                      {roleProjectId ? (
                        <RolePanel projectId={roleProjectId} />
                      ) : (
                        <Empty description={t('perm.pickProjectFirst')} />
                      )}
                    </Card>
                  ),
                },
              ]
            : []),
        ]}
      />

      <GrantPermissionModal
        open={grantOpen}
        kind={grantKind}
        user={selectedUser}
        projects={projects}
        groups={groups}
        nodes={nodes}
        nodeGroups={nodeGroups}
        confirmLoading={grantMut.isPending}
        onCancel={() => setGrantOpen(false)}
        onSubmit={(payload) => grantMut.mutate(payload)}
      />

      <Drawer
        title={review ? t('perm.reviewTitle', { id: review.id }) : t('perm.review')}
        width={isMobile ? '100%' : 480}
        open={!!review}
        onClose={() => setReview(null)}
        extra={
          review ? (
            <Space>
              <Button
                danger
                icon={<CloseOutlined />}
                onClick={() => reviewMut.mutate({ id: review.id, action: 'reject', comment: reviewComment })}
                loading={reviewMut.isPending}
              >
                {t('perm.reject')}
              </Button>
              <Button
                type="primary"
                icon={<CheckOutlined />}
                onClick={() =>
                  reviewMut.mutate({
                    id: review.id,
                    action: 'approve',
                    comment: reviewComment,
                    actions: review.apply_type === 'role' ? undefined : withRead(reviewActions),
                  })
                }
                loading={reviewMut.isPending}
              >
                {t('perm.pass')}
              </Button>
            </Space>
          ) : null
        }
      >
        {review && (
          <>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('perm.applicant')}>{review.applicant}</Descriptions.Item>
              <Descriptions.Item label={t('perm.scope')}>{scopeText(review)}</Descriptions.Item>
              <Descriptions.Item label={t('common.type')}>{applyKind(review)}</Descriptions.Item>
              <Descriptions.Item label={t('perm.source')}>
                <Tag color={sourceMeta(review.source).color}>{sourceMeta(review.source).text}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('perm.reason')}>{review.reason || t('perm.none')}</Descriptions.Item>
              <Descriptions.Item label={t('perm.submittedAt')}>{review.created_at}</Descriptions.Item>
            </Descriptions>
            {review.apply_type === 'role' ? (
              <div style={{ marginBottom: 16, color: '#666' }}>{t('perm.roleApproveHint')}</div>
            ) : (
              <>
                <div style={{ marginBottom: 8, fontWeight: 500 }}>{t('perm.grantedEditable')}</div>
                <Checkbox.Group
                  value={reviewActions}
                  onChange={(v) => setReviewActions(withRead(v as string[]))}
                  options={reviewActionOptions()}
                  style={{ marginBottom: 16 }}
                />
              </>
            )}
            <div style={{ marginBottom: 8, fontWeight: 500 }}>{t('perm.reviewComment')}</div>
            <Input.TextArea rows={3} value={reviewComment} onChange={(e) => setReviewComment(e.target.value)} placeholder={t('perm.optional')} />
          </>
        )}
      </Drawer>
    </div>
  )
}
