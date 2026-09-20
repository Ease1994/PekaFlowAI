import { Tag, Button, Modal, Form, Input, Select, Space, Popconfirm, Checkbox, Divider, Empty } from 'antd'
import DataTable from '@/components/DataTable'
import { PlusOutlined, EditOutlined, DeleteOutlined, UserOutlined } from '@ant-design/icons'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post, put, del } from '@/api/client'
import type { Role, RoleUser } from '@/api/types'
import { useT } from '@/i18n'

interface UserItem {
  id: number
  username: string
  display_name: string
  is_admin: boolean
}

/**
 * 资源类型界面名。分组用环境分组，避免 acl.group「分组」过短。
 */
function resourceLabel(res: string, t: (key: string) => string) {
  if (res === 'group') return t('role.groupEnv')
  if (res === 'repository') return t('acl.repo')
  const keys: Record<string, string> = {
    project: 'acl.project',
    pipeline: 'acl.pipeline',
    credential: 'acl.credential',
  }
  return keys[res] ? t(keys[res]) : res
}

/**
 * 操作界面名。权限表里的 read 对应查看。
 */
function actionLabel(action: string, t: (key: string) => string) {
  const keys: Record<string, string> = {
    read: 'acl.view',
    create: 'acl.create',
    update: 'acl.update',
    delete: 'acl.delete',
    execute: 'acl.execute',
    approve: 'acl.approve',
    approval_exempt: 'acl.exempt',
  }
  return keys[action] ? t(keys[action]) : action
}

/**
 * 成员展示：有姓名且不同于账号时带上账号，避免重名对不上人。
 */
function userLabel(
  u: { username: string; display_name?: string },
  t: (key: string, vars?: Record<string, string | number>) => string,
) {
  const name = u.display_name || u.username
  return name === u.username ? name : t('role.userWithAccount', { name, username: u.username })
}

/** 权限管理页的「项目角色」Tab：角色跟项目绑定，成员在编辑框里搜、加、点 × 去掉。 */
export default function RolePanel({ projectId }: { projectId: number }) {
  const t = useT()
  const queryClient = useQueryClient()
  const [roleModalOpen, setRoleModalOpen] = useState(false)
  const [editing, setEditing] = useState<Role | null>(null)
  const [members, setMembers] = useState<RoleUser[]>([])
  const [pickUser, setPickUser] = useState<number | undefined>()
  const [roleForm] = Form.useForm()

  const { data: roles = [] } = useQuery({
    queryKey: ['roles', projectId],
    queryFn: () => get<Role[]>('/roles', { project_id: projectId }),
  })

  const { data: resourceActions = {} } = useQuery({
    queryKey: ['roles-resource-actions'],
    queryFn: () => get<Record<string, string[]>>('/roles/resource-actions'),
  })

  const { data: users = [] } = useQuery({
    queryKey: ['users'],
    queryFn: () => get<UserItem[]>('/users'),
  })

  const openCreate = () => {
    setEditing(null)
    setMembers([])
    setPickUser(undefined)
    roleForm.resetFields()
    roleForm.setFieldsValue({ permissions: {} })
    setRoleModalOpen(true)
  }

  const openEdit = (r: Role) => {
    setEditing(r)
    setMembers(r.users || [])
    setPickUser(undefined)
    roleForm.setFieldsValue({ name: r.name, description: r.description, permissions: r.permissions })
    setRoleModalOpen(true)
  }

  const addMember = (userId: number) => {
    const u = users.find((item) => item.id === userId)
    if (!u || members.some((m) => m.id === u.id)) return
    setMembers((prev) => [
      ...prev,
      { id: u.id, username: u.username, display_name: u.display_name },
    ])
  }

  const removeMember = (userId: number) => {
    setMembers((prev) => prev.filter((m) => m.id !== userId))
  }

  const syncMembers = async (roleId: number, original: RoleUser[]) => {
    const prev = new Set(original.map((u) => u.id))
    const next = new Set(members.map((u) => u.id))
    const toAdd = members.filter((u) => !prev.has(u.id)).map((u) => u.id)
    const toRemove = original.filter((u) => !next.has(u.id)).map((u) => u.id)
    if (toAdd.length) {
      await post(`/roles/${roleId}/users`, { user_ids: toAdd })
    }
    await Promise.all(toRemove.map((uid) => del(`/roles/${roleId}/users/${uid}`)))
  }

  const handleSaveRole = async () => {
    const values = await roleForm.validateFields()
    const original = editing?.users || []
    if (editing) {
      await put(`/roles/${editing.id}`, values)
      await syncMembers(editing.id, original)
    } else {
      const created = await post<Role>('/roles', { ...values, project_id: projectId })
      if (created?.id) await syncMembers(created.id, [])
    }
    queryClient.invalidateQueries({ queryKey: ['roles', projectId] })
    setRoleModalOpen(false)
  }

  const handleDeleteRole = async (id: number) => {
    await del(`/roles/${id}`)
    queryClient.invalidateQueries({ queryKey: ['roles', projectId] })
  }

  const renderPerms = (perms: Record<string, string[]>) => {
    const chips: JSX.Element[] = []
    for (const [res, actions] of Object.entries(perms || {})) {
      for (const a of actions) {
        chips.push(
          <Tag key={`${res}-${a}`} color="blue" style={{ marginBottom: 4 }}>
            {resourceLabel(res, t)}·{actionLabel(a, t)}
          </Tag>,
        )
      }
    }
    return chips.length ? chips : <span style={{ color: '#999' }}>{t('role.none')}</span>
  }

  const assignable = users.filter(
    (u) => !u.is_admin && !members.some((m) => m.id === u.id),
  )

  const columns = [
    { title: t('role.roleName'), dataIndex: 'name', render: (v: string) => <b>{v}</b> },
    { title: t('common.description'), dataIndex: 'description', ellipsis: true },
    {
      title: t('role.perms'),
      dataIndex: 'permissions',
      render: (v: Record<string, string[]>) => renderPerms(v),
    },
    {
      title: t('role.members'),
      dataIndex: 'users',
      render: (v: Role['users']) =>
        v.length ? (
          <Space size={4} wrap>
            {v.slice(0, 5).map((u) => (
              <Tag key={u.id} icon={<UserOutlined />}>{u.display_name || u.username}</Tag>
            ))}
            {v.length > 5 && <span style={{ color: '#999' }}>+{v.length - 5}</span>}
          </Space>
        ) : (
          <span style={{ color: '#999' }}>{t('role.noMembers')}</span>
        ),
    },
    {
      title: t('common.action'),
      width: 160,
      render: (_: unknown, r: Role) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>{t('common.edit')}</Button>
          <Popconfirm title={t('role.deleteConfirm')} onConfirm={() => handleDeleteRole(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />}>{t('common.delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t('role.newRole')}</Button>
        <span style={{ marginLeft: 12, color: '#999', fontSize: 12 }}>
          {t('role.hint')}
        </span>
      </div>
      <DataTable chromeKey="project-roles" rowKey="id" columns={columns} dataSource={roles} pagination={false} />

      <Modal
        title={editing ? t('role.editRole') : t('role.newRole')}
        open={roleModalOpen}
        onOk={handleSaveRole}
        onCancel={() => setRoleModalOpen(false)}
        width={680}
        destroyOnClose
        okText={t('common.ok')}
        cancelText={t('common.cancel')}
      >
        <Form form={roleForm} layout="vertical">
          <Form.Item name="name" label={t('role.roleName')} rules={[{ required: true, message: t('role.nameRequired') }]}>
            <Input placeholder={t('role.namePh')} />
          </Form.Item>
          <Form.Item name="description" label={t('common.description')}>
            <Input placeholder={t('role.descPh')} />
          </Form.Item>
          <Divider style={{ margin: '12px 0' }}>{t('role.permDivider')}</Divider>
          <Form.Item name="permissions" label={null}>
            <PermissionMatrix resourceActions={resourceActions} form={roleForm} />
          </Form.Item>
          <Divider style={{ margin: '12px 0' }}>{t('role.members')}</Divider>
          <div style={{ marginBottom: 8, color: '#999', fontSize: 12 }}>
            {t('role.memberHint')}
          </div>
          <Select
            showSearch
            allowClear
            placeholder={t('role.searchUser')}
            optionFilterProp="label"
            style={{ width: '100%', marginBottom: 12 }}
            options={assignable.map((u) => ({
              label: userLabel(u, t),
              value: u.id,
            }))}
            value={pickUser}
            onChange={(id) => {
              if (id != null) addMember(Number(id))
              setPickUser(undefined)
            }}
          />
          {members.length ? (
            <Space size={[8, 8]} wrap>
              {members.map((u) => (
                <Tag
                  key={u.id}
                  icon={<UserOutlined />}
                  closable
                  onClose={(e) => {
                    e.preventDefault()
                    removeMember(u.id)
                  }}
                >
                  {u.display_name || u.username}
                </Tag>
              ))}
            </Space>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('role.emptyMembers')} />
          )}
        </Form>
      </Modal>
    </div>
  )
}

function PermissionMatrix({
  resourceActions,
  form,
}: {
  resourceActions: Record<string, string[]>
  form: any
}) {
  const t = useT()
  const value: Record<string, string[]> = form.getFieldValue('permissions') || {}
  return (
    <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: '4px 12px' }}>
      {Object.entries(resourceActions).map(([res, actions]) => (
        <div
          key={res}
          style={{
            display: 'flex',
            alignItems: 'center',
            padding: '6px 0',
            borderBottom: '1px solid #f5f5f5',
          }}
        >
          <span style={{ width: 80, fontWeight: 600 }}>{resourceLabel(res, t)}</span>
          <Checkbox.Group
            value={value[res] || []}
            onChange={(vals) => {
              const next = { ...value }
              next[res] = vals as string[]
              form.setFieldsValue({ permissions: next })
            }}
            options={actions.map((a) => ({ label: actionLabel(a, t), value: a }))}
          />
        </div>
      ))}
    </div>
  )
}
