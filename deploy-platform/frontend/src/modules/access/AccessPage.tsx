import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Tabs,
  Tag,
  message,
} from 'antd'
import { AuditOutlined, CheckOutlined, CloseOutlined, StopOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '@/api/client'
import { envLabel, groupDisplayName, groupOptionLabel } from '@/env'
import DataTable from '@/components/DataTable'
import { useAuthStore } from '@/stores/auth'
import { t as translate, useT } from '@/i18n'

interface Catalog {
  projects: { id: number; name: string; code: string }[]
  groups: { id: number; name: string; type: string; project_id: number }[]
  pipelines: {
    id: number
    name: string
    project_id: number
    group_id: number
    project: string
    group: string
    env: string
  }[]
}

interface Application {
  id: number
  applicant_id: number
  applicant: string
  project_id: number
  group_id: number
  pipeline_id: number
  project_name: string
  group_name: string
  pipeline_name: string
  apply_type: string
  scope?: string
  scope_label?: string
  granted_actions: string
  reason: string
  status: string
  reviewer: string
  review_comment: string
  reviewed_at: string
  created_at: string
  source: string
}

/** 申请单状态码对应 Tag 颜色；句子按码现取，approved 没有公共 status 键。 */
const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  approved: 'success',
  rejected: 'error',
  cancelled: 'default',
}

/** 申请单状态码 → i18n 键。pending/rejected/cancelled 复用 status.*。 */
const STATUS_I18N: Record<string, string> = {
  pending: 'status.pending',
  approved: 'access.approved',
  rejected: 'status.rejected',
  cancelled: 'status.cancelled',
}

/** 申请来源码 → i18n 键。 */
const SOURCE_I18N: Record<string, string> = {
  web: 'access.sourceWeb',
  ai: 'access.sourceAi',
  api: 'access.sourceApi',
  cron: 'access.sourceCron',
}

const SOURCE_COLOR: Record<string, string> = {
  web: 'default',
  ai: 'blue',
  api: 'cyan',
  cron: 'purple',
}

/** 申请覆盖范围：整项目 / 环境分组 / 单条流水线。 */
type ApplyScope = 'project' | 'group' | 'pipeline'

/** 可申请的动作。默认勾查看+执行；标签走 acl.*。 */
function applyActionOptions() {
  return [
    { value: 'read', label: translate('acl.view') },
    { value: 'create', label: translate('acl.create') },
    { value: 'update', label: translate('acl.update') },
    { value: 'delete', label: translate('acl.delete') },
    { value: 'execute', label: translate('acl.execute') },
    { value: 'approve', label: translate('acl.approve') },
    { value: 'approval_exempt', label: translate('acl.exempt') },
  ]
}

/** 列表和审批弹窗用的范围名称。后端 scope_label 原样展示。 */
function scopeLabel(row: Application): string {
  if (row.scope_label) return row.scope_label
  if (row.apply_type === 'project_execute') return translate('access.scopeProject')
  if (row.apply_type === 'group_execute') return translate('access.scopeGroup')
  return translate('access.scopePipeline')
}

/** 通过后实际授予的范围说明。 */
function grantHint(row: Application | { apply_type?: string } | null, approved: boolean): string {
  if (!approved) return translate('access.grantRejectHint')
  const kind = row && 'apply_type' in row ? row.apply_type : ''
  const scope =
    kind === 'project_execute'
      ? translate('access.grantScopeProject')
      : kind === 'group_execute'
        ? translate('access.grantScopeGroup')
        : translate('access.grantScopePipeline')
  return translate('access.grantHint', { scope })
}

/** 申请单状态文案：有码就翻译，没有码回落到原值。 */
function applicationStatusText(code: string): string {
  return STATUS_I18N[code] ? translate(STATUS_I18N[code]) : code
}

/** 申请来源文案。空值按手动。 */
function applicationSourceText(code: string): string {
  if (!code) return translate('access.sourceWeb')
  return SOURCE_I18N[code] ? translate(SOURCE_I18N[code]) : code
}

export default function AccessPage() {
  const t = useT()
  const qc = useQueryClient()
  const user = useAuthStore((s) => s.user)
  const [projectId, setProjectId] = useState<number>()
  const [groupId, setGroupId] = useState<number>()
  const [pipelineId, setPipelineId] = useState<number>()
  const [applyScope, setApplyScope] = useState<ApplyScope>('pipeline')
  const [reason, setReason] = useState('')
  /** 拟授动作；默认查看+执行。 */
  const [applyActions, setApplyActions] = useState<string[]>(['read', 'execute'])
  const [reviewTarget, setReviewTarget] = useState<{
    id: number
    approved: boolean
    apply_type: string
  } | null>(null)
  const [comment, setComment] = useState('')

  const { data: catalog } = useQuery({
    queryKey: ['access-catalog'],
    queryFn: () => get<Catalog>('/access/catalog'),
  })
  const { data: mine = [] } = useQuery({
    queryKey: ['access-applications', 'mine'],
    queryFn: () => get<Application[]>('/access/applications', { scope: 'mine' }),
  })
  const { data: pending = [] } = useQuery({
    queryKey: ['access-applications', 'pending'],
    queryFn: () => get<Application[]>('/access/applications', { scope: 'pending' }),
  })
  const { data: allRows = [] } = useQuery({
    queryKey: ['access-applications', 'all'],
    queryFn: () => get<Application[]>('/access/applications', { scope: 'all' }),
    enabled: !!user?.is_admin,
  })

  const groups = useMemo(
    () => (catalog?.groups || []).filter((g) => !projectId || g.project_id === projectId),
    [catalog, projectId],
  )
  const pipelines = useMemo(
    () =>
      (catalog?.pipelines || []).filter(
        (p) => (!projectId || p.project_id === projectId) && (!groupId || p.group_id === groupId),
      ),
    [catalog, projectId, groupId],
  )

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['access-applications'] })
  }

  const applyMut = useMutation({
    mutationFn: () => {
      if (applyScope === 'project') {
        return post<Application>('/access/applications', { project_id: projectId, reason, actions: applyActions })
      }
      if (applyScope === 'group') {
        return post<Application>('/access/applications', { group_id: groupId, reason, actions: applyActions })
      }
      return post<Application>('/access/applications', { pipeline_id: pipelineId, reason, actions: applyActions })
    },
    onSuccess: (row) => {
      message.success(t('access.submitted', { n: row.id }))
      setReason('')
      invalidate()
    },
  })

  /** 当前范围是否已选齐必填项。 */
  const canSubmit =
    applyScope === 'project'
      ? !!projectId
      : applyScope === 'group'
        ? !!groupId
        : !!pipelineId

  const actMut = useMutation({
    mutationFn: ({ id, action, comment: c }: { id: number; action: 'approve' | 'reject' | 'cancel'; comment?: string }) =>
      post<Application>(`/access/applications/${id}/${action}`, action === 'cancel' ? undefined : { comment: c || '' }),
    onSuccess: () => {
      message.success(t('access.processed'))
      setReviewTarget(null)
      setComment('')
      invalidate()
    },
  })

  const columns = (opts: { review?: boolean; mine?: boolean }) => [
    { title: t('access.colId'), dataIndex: 'id', width: 70 },
    { title: t('access.colApplicant'), dataIndex: 'applicant', width: 120 },
    {
      title: t('access.colScope'),
      width: 110,
      render: (_: unknown, row: Application) => <Tag>{scopeLabel(row)}</Tag>,
    },
    { title: t('acl.project'), dataIndex: 'project_name' },
    {
      title: t('acl.group'),
      dataIndex: 'group_name',
      width: 140,
      render: (v: string) => groupDisplayName({ name: v }),
    },
    { title: t('acl.pipeline'), dataIndex: 'pipeline_name' },
    {
      title: t('access.colGranted'),
      dataIndex: 'granted_actions',
      width: 200,
      render: (v: string) => v || 'read,execute',
    },
    { title: t('access.colReason'), dataIndex: 'reason', ellipsis: true },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 100,
      render: (s: string, row: Application) => (
        <Space>
          <Tag color={STATUS_COLOR[s]}>{applicationStatusText(s)}</Tag>
          {s === 'approved' && row.granted_actions && (
            <Tag>{t('access.grantedTag', { actions: row.granted_actions })}</Tag>
          )}
        </Space>
      ),
    },
    { title: t('access.colCreated'), dataIndex: 'created_at', width: 180 },
    {
      title: t('access.colSource'),
      dataIndex: 'source',
      width: 90,
      render: (s: string) => <Tag color={SOURCE_COLOR[s] || 'default'}>{applicationSourceText(s)}</Tag>,
    },
    { title: t('access.colReviewer'), dataIndex: 'reviewer', width: 100 },
    { title: t('access.colComment'), dataIndex: 'review_comment', ellipsis: true },
    {
      title: t('common.action'),
      width: 180,
      render: (_: unknown, row: Application) => {
        if (row.status !== 'pending') return null
        if (opts.review) {
          return (
            <Space>
              <Button
                type="primary"
                size="small"
                icon={<CheckOutlined />}
                onClick={() => setReviewTarget({ id: row.id, approved: true, apply_type: row.apply_type })}
              >
                {t('access.pass')}
              </Button>
              <Button
                danger
                size="small"
                icon={<CloseOutlined />}
                onClick={() => setReviewTarget({ id: row.id, approved: false, apply_type: row.apply_type })}
              >
                {t('access.reject')}
              </Button>
            </Space>
          )
        }
        if (opts.mine && row.applicant_id === user?.id) {
          return (
            <Button
              size="small"
              icon={<StopOutlined />}
              onClick={() => actMut.mutate({ id: row.id, action: 'cancel' })}
            >
              {t('access.revoke')}
            </Button>
          )
        }
        return null
      },
    },
  ]

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('access.alertTitle')}
        description={t('access.alertDesc')}
      />

      <Card title={<><AuditOutlined /> {t('access.submitTitle')}</>} style={{ marginBottom: 16 }}>
        <Form layout="vertical">
          <Form.Item label={t('access.applyScope')} style={{ marginBottom: 16 }}>
            <Radio.Group
              value={applyScope}
              onChange={(e) => setApplyScope(e.target.value)}
              optionType="button"
              options={[
                { value: 'pipeline', label: t('access.scopePipelineBtn') },
                { value: 'group', label: t('access.scopeGroupBtn') },
                { value: 'project', label: t('access.scopeProjectBtn') },
              ]}
            />
          </Form.Item>
          <Space wrap size="middle" style={{ width: '100%' }} align="start">
            <Form.Item label={t('acl.project')} style={{ marginBottom: 0, minWidth: 220 }}>
              <Select
                allowClear
                placeholder={t('access.selectProject')}
                value={projectId}
                onChange={(v) => {
                  setProjectId(v)
                  setGroupId(undefined)
                  setPipelineId(undefined)
                }}
                options={(catalog?.projects || []).map((p) => ({ value: p.id, label: p.name }))}
              />
            </Form.Item>
            {applyScope !== 'project' ? (
              <Form.Item label={t('acl.group')} style={{ marginBottom: 0, minWidth: 220 }}>
                <Select
                  allowClear
                  placeholder={t('access.selectGroup')}
                  value={groupId}
                  onChange={(v) => {
                    setGroupId(v)
                    setPipelineId(undefined)
                  }}
                  options={groups.map((g) => ({
                    value: g.id,
                    label: groupOptionLabel(g),
                  }))}
                />
              </Form.Item>
            ) : null}
            {applyScope === 'pipeline' ? (
              <Form.Item label={t('acl.pipeline')} style={{ marginBottom: 0, minWidth: 260 }}>
                <Select
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder={t('access.selectPipeline')}
                  value={pipelineId}
                  onChange={setPipelineId}
                  options={pipelines.map((p) => ({
                    value: p.id,
                    label: `${p.name}${p.env ? ` · ${envLabel(p.env)}` : ''}`,
                  }))}
                />
              </Form.Item>
            ) : null}
          </Space>
          <Form.Item label={t('access.applyPerms')} style={{ marginTop: 16 }}>
            <Checkbox.Group
              options={applyActionOptions()}
              value={applyActions}
              onChange={(v) => {
                const next = v as string[]
                if (!next.length) {
                  setApplyActions(['read', 'execute'])
                  return
                }
                setApplyActions(next.includes('read') ? next : ['read', ...next])
              }}
            />
          </Form.Item>
          <Form.Item label={t('access.colReason')} style={{ marginTop: 16, maxWidth: 720 }}>
            <Input.TextArea
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t('access.reasonPlaceholder')}
            />
          </Form.Item>
          <Button
            type="primary"
            disabled={!canSubmit}
            loading={applyMut.isPending}
            onClick={() => applyMut.mutate()}
          >
            {t('access.submitApply')}
          </Button>
        </Form>
      </Card>

      <Card>
        <Tabs
          items={[
            {
              key: 'mine',
              label: t('access.tabMine', { n: mine.length }),
              children: (
                <DataTable
                  chromeKey="access-mine"
                  rowKey="id"
                  size="small"
                  columns={columns({ mine: true })}
                  dataSource={mine}
                />
              ),
            },
            {
              key: 'pending',
              label: t('access.tabPending', { n: pending.length }),
              children: (
                <DataTable
                  chromeKey="access-pending"
                  rowKey="id"
                  size="small"
                  columns={columns({ review: true })}
                  dataSource={pending}
                />
              ),
            },
            ...(user?.is_admin
              ? [
                  {
                    key: 'all',
                    label: t('access.tabAll', { n: allRows.length }),
                    children: (
                      <DataTable
                        chromeKey="access-all"
                        rowKey="id"
                        size="small"
                        columns={columns({ review: true })}
                        dataSource={allRows}
                      />
                    ),
                  },
                ]
              : []),
          ]}
        />
      </Card>

      <Modal
        title={reviewTarget?.approved ? t('access.modalPass') : t('access.modalReject')}
        open={!!reviewTarget}
        onCancel={() => setReviewTarget(null)}
        onOk={() => {
          if (!reviewTarget) return
          actMut.mutate({
            id: reviewTarget.id,
            action: reviewTarget.approved ? 'approve' : 'reject',
            comment,
          })
        }}
        confirmLoading={actMut.isPending}
        okButtonProps={{ danger: reviewTarget?.approved === false }}
        okText={reviewTarget?.approved ? t('access.okPass') : t('access.okReject')}
      >
        <p>{grantHint(reviewTarget, !!reviewTarget?.approved)}</p>
        <Input.TextArea
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={t('access.commentPh')}
        />
      </Modal>
    </div>
  )
}
