import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Tag,
  message,
} from 'antd'
import { AuditOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post } from '@/api/client'
import { envLabel, groupOptionLabel } from '@/env'
import { useAuthStore } from '@/stores/auth'
import { t as translate, useT } from '@/i18n'

/** 申请覆盖范围：整项目 / 环境分组 / 单条流水线。 */
type ApplyScope = 'project' | 'group' | 'pipeline'
/** 一张单只申请一种东西：资源直授或项目角色。 */
type ApplyKind = 'resource' | 'role'

interface CatalogRole {
  id: number
  project_id: number
  name: string
  description: string
  permissions: Record<string, string[]>
}

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
  roles: CatalogRole[]
}

interface DirectGrant {
  resource_type: string
  resource_id: number
  resource_name: string
  actions: string[]
}

interface RoleGrant {
  role_id: number
  name: string
  description: string
  permissions: Record<string, string[]>
}

interface Entitlements {
  is_admin: boolean
  projects: {
    project_id: number
    project_name: string
    roles: RoleGrant[]
    direct: DirectGrant[]
  }[]
  nodes: DirectGrant[]
}

interface Application {
  id: number
}

/** 可申请的动作。标签走 acl.*，切语言时现取。 */
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

/** 资源类型码 → 界面名称。未知码原样返回。 */
function typeLabel(rtype: string): string {
  const keys: Record<string, string> = {
    project: 'acl.project',
    group: 'acl.group',
    pipeline: 'acl.pipeline',
    node: 'acl.node',
    node_group: 'acl.nodeGroup',
    repository: 'acl.repo',
    credential: 'acl.credential',
  }
  return keys[rtype] ? translate(keys[rtype]) : rtype
}

/** 动作码 → 界面名称。未知码原样返回。 */
function actionLabel(code: string): string {
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
  return keys[code] ? translate(keys[code]) : code
}

/**
 * 角色权限包收成一排标签，方便申请人看这个角色实际能干什么。
 */
function permissionTags(permissions: Record<string, string[]>) {
  const entries = Object.entries(permissions || {})
  if (!entries.length) return <span style={{ color: '#999' }}>{translate('access.noPerms')}</span>
  return entries.flatMap(([rtype, actions]) =>
    (actions || []).map((a) => (
      <Tag key={`${rtype}-${a}`}>
        {typeLabel(rtype)} · {actionLabel(a)}
      </Tag>
    )),
  )
}

/**
 * 权限申请 Tab：上半提交（资源权或项目角色），下半看自己已经有的授权。
 */
export default function AccessApplyPanel() {
  const t = useT()
  const qc = useQueryClient()
  const user = useAuthStore((s) => s.user)
  const isAdmin = !!user?.is_admin
  const [applyKind, setApplyKind] = useState<ApplyKind>('resource')
  const [applyScope, setApplyScope] = useState<ApplyScope>('pipeline')
  const [projectId, setProjectId] = useState<number>()
  const [groupId, setGroupId] = useState<number>()
  const [pipelineId, setPipelineId] = useState<number>()
  const [roleId, setRoleId] = useState<number>()
  const [applyActions, setApplyActions] = useState<string[]>(['read', 'execute'])
  const [reason, setReason] = useState('')

  const { data: catalog } = useQuery({
    queryKey: ['access-catalog'],
    queryFn: () => get<Catalog>('/access/catalog'),
    enabled: !isAdmin,
  })
  const { data: entitlements } = useQuery({
    queryKey: ['access-me'],
    queryFn: () => get<Entitlements>('/access/me'),
  })

  const groups = useMemo(
    () => (catalog?.groups || []).filter((g) => !projectId || g.project_id === projectId),
    [catalog, projectId],
  )
  const pipelines = useMemo(
    () =>
      (catalog?.pipelines || []).filter((p) => {
        if (projectId && p.project_id !== projectId) return false
        if (groupId && p.group_id !== groupId) return false
        return true
      }),
    [catalog, projectId, groupId],
  )
  const projectRoles = useMemo(
    () => (catalog?.roles || []).filter((r) => r.project_id === projectId),
    [catalog, projectId],
  )
  const selectedRole = projectRoles.find((r) => r.id === roleId)

  const canSubmitResource =
    applyKind === 'resource' &&
    ((applyScope === 'project' && !!projectId) ||
      (applyScope === 'group' && !!groupId) ||
      (applyScope === 'pipeline' && !!pipelineId))
  const canSubmitRole = applyKind === 'role' && !!roleId
  const canSubmit = !isAdmin && (canSubmitResource || canSubmitRole)

  const applyMut = useMutation({
    mutationFn: () => {
      if (applyKind === 'role') {
        return post<Application>('/access/applications', { role_id: roleId, reason })
      }
      if (applyScope === 'project') {
        return post<Application>('/access/applications', {
          project_id: projectId,
          reason,
          actions: applyActions,
        })
      }
      if (applyScope === 'group') {
        return post<Application>('/access/applications', {
          group_id: groupId,
          reason,
          actions: applyActions,
        })
      }
      return post<Application>('/access/applications', {
        pipeline_id: pipelineId,
        reason,
        actions: applyActions,
      })
    },
    onSuccess: (row) => {
      message.success(t('access.submitted', { n: row.id }))
      setReason('')
      qc.invalidateQueries({ queryKey: ['access-applications'] })
      qc.invalidateQueries({ queryKey: ['access-me'] })
      qc.invalidateQueries({ queryKey: ['notices'] })
    },
  })

  const mineEmpty =
    !entitlements?.is_admin &&
    !(entitlements?.projects || []).length &&
    !(entitlements?.nodes || []).length

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('access.alertTitle')}
        description={t('access.applyDesc')}
      />

      <Card title={<><AuditOutlined /> {t('access.startApply')}</>} style={{ marginBottom: 16 }}>
        {isAdmin ? (
          <Alert type="success" showIcon message={t('access.adminAll')} />
        ) : (
          <Form layout="vertical">
            <Form.Item label={t('access.applyKind')} style={{ marginBottom: 16 }}>
              <Radio.Group
                value={applyKind}
                onChange={(e) => {
                  setApplyKind(e.target.value)
                  setRoleId(undefined)
                }}
                optionType="button"
                options={[
                  { value: 'resource', label: t('access.kindResource') },
                  { value: 'role', label: t('access.kindRole') },
                ]}
              />
            </Form.Item>
            {applyKind === 'resource' ? (
              <>
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
                      showSearch
                      optionFilterProp="label"
                      placeholder={t('access.selectProject')}
                      value={projectId}
                      onChange={(v) => {
                        setProjectId(v)
                        setGroupId(undefined)
                        setPipelineId(undefined)
                        setRoleId(undefined)
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
              </>
            ) : (
              <>
                <Space wrap size="middle" style={{ width: '100%' }} align="start">
                  <Form.Item label={t('acl.project')} style={{ marginBottom: 0, minWidth: 220 }}>
                    <Select
                      allowClear
                      showSearch
                      optionFilterProp="label"
                      placeholder={t('access.selectProject')}
                      value={projectId}
                      onChange={(v) => {
                        setProjectId(v)
                        setRoleId(undefined)
                      }}
                      options={(catalog?.projects || []).map((p) => ({ value: p.id, label: p.name }))}
                    />
                  </Form.Item>
                  <Form.Item label={t('access.role')} style={{ marginBottom: 0, minWidth: 260 }}>
                    <Select
                      allowClear
                      showSearch
                      optionFilterProp="label"
                      placeholder={projectId ? t('access.selectRole') : t('access.selectProjectFirst')}
                      disabled={!projectId}
                      value={roleId}
                      onChange={setRoleId}
                      options={projectRoles.map((r) => ({
                        value: r.id,
                        label: r.description ? t('access.roleWithDesc', { name: r.name, desc: r.description }) : r.name,
                      }))}
                    />
                  </Form.Item>
                </Space>
                {projectId && !projectRoles.length ? (
                  <Alert
                    style={{ marginTop: 16 }}
                    type="warning"
                    showIcon
                    message={t('access.noRoles')}
                  />
                ) : null}
                {selectedRole ? (
                  <div style={{ marginTop: 16 }}>
                    <div style={{ marginBottom: 8, color: '#666' }}>
                      {t('access.roleHint')}
                    </div>
                    {permissionTags(selectedRole.permissions)}
                  </div>
                ) : null}
              </>
            )}
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
        )}
      </Card>

      <Card title={t('access.myPerms')}>
        {entitlements?.is_admin ? (
          <Alert type="success" showIcon message={t('access.adminAllListed')} />
        ) : mineEmpty ? (
          <Empty description={t('access.emptyMine')} />
        ) : (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            {(entitlements?.projects || []).map((p) => (
              <div key={p.project_id}>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>{p.project_name}</div>
                {p.roles.map((r) => (
                  <div key={`role-${r.role_id}`} style={{ marginBottom: 8 }}>
                    <Tag color="purple">{t('access.roleTag')}</Tag>
                    <span style={{ marginRight: 8 }}>{r.name}</span>
                    {permissionTags(r.permissions)}
                  </div>
                ))}
                {p.direct.map((d) => (
                  <div key={`${d.resource_type}-${d.resource_id}`} style={{ marginBottom: 8 }}>
                    <Tag>{t('access.directGrant')}</Tag>
                    <span style={{ marginRight: 8 }}>
                      {typeLabel(d.resource_type)} · {d.resource_name}
                    </span>
                    {d.actions.map((a) => (
                      <Tag color="cyan" key={a}>
                        {actionLabel(a)}
                      </Tag>
                    ))}
                  </div>
                ))}
                {!p.roles.length && !p.direct.length ? (
                  <div style={{ color: '#999' }}>{t('access.noDetail')}</div>
                ) : null}
              </div>
            ))}
            {(entitlements?.nodes || []).length ? (
              <div>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>{t('access.nodePush')}</div>
                {entitlements!.nodes.map((d) => (
                  <div key={`${d.resource_type}-${d.resource_id}`} style={{ marginBottom: 8 }}>
                    <Tag>{t('access.directGrant')}</Tag>
                    <span style={{ marginRight: 8 }}>
                      {typeLabel(d.resource_type)} · {d.resource_name}
                    </span>
                    {d.actions.map((a) => (
                      <Tag color="cyan" key={a}>
                        {actionLabel(a)}
                      </Tag>
                    ))}
                  </div>
                ))}
              </div>
            ) : null}
          </Space>
        )}
      </Card>
    </div>
  )
}
