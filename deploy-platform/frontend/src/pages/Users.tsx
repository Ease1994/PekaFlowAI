import { useEffect, useMemo, useState } from 'react'
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  message,
} from 'antd'
import {
  CloudSyncOutlined,
  DeleteOutlined,
  KeyOutlined,
  PlusOutlined,
  SearchOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, post, put } from '@/api/client'
import DataTable from '@/components/DataTable'
import { useT } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

interface AccountUser {
  id: number
  username: string
  display_name: string
  email: string
  is_admin: boolean
  source: string
  identities?: string[]
  status: string
  last_login_at: string
  created_at: string
  has_password: boolean
}

interface LdapEntry {
  dn: string
  username: string
  email: string
  display_name: string
  upn: string
}

export default function Users() {
  const t = useT()
  const qc = useQueryClient()
  /** 当前登录用户。编辑自己时管理员开关必须锁死，避免保存后把自己踢出系统管理。 */
  const me = useAuthStore((s) => s.user)
  const [kw, setKw] = useState('')
  const [source, setSource] = useState<string>()
  const [status, setStatus] = useState<string>()
  const [page, setPage] = useState(1)
  const [createOpen, setCreateOpen] = useState(false)
  const [editUser, setEditUser] = useState<AccountUser | null>(null)
  const [pwdUser, setPwdUser] = useState<AccountUser | null>(null)
  const [ldapOpen, setLdapOpen] = useState(false)
  const [ldapKw, setLdapKw] = useState('')
  const [ldapRows, setLdapRows] = useState<LdapEntry[]>([])
  const [ldapSelected, setLdapSelected] = useState<LdapEntry[]>([])
  const [createForm] = Form.useForm()
  const [editForm] = Form.useForm()
  const [pwdForm] = Form.useForm()

  /** 登录来源：颜色固定，句子按当前语言现取。 */
  const SOURCE: Record<string, { color: string; text: string }> = {
    local: { color: 'default', text: t('users.local') },
    ldap: { color: 'blue', text: 'LDAP' },
    wecom: { color: 'green', text: t('users.wecom') },
  }

  const { data: users = [], isFetching } = useQuery({
    queryKey: ['account-users', kw, source, status],
    queryFn: () =>
      get<AccountUser[]>('/account/users', {
        keyword: kw || undefined,
        source: source || undefined,
        status: status || undefined,
      }),
  })

  // 搜完常常只剩几条，还停在原页码就是一张空表，看着像没搜到
  useEffect(() => {
    setPage(1)
  }, [kw, source, status])

  const stats = useMemo(() => {
    const all = users
    return {
      total: all.length,
      local: all.filter((u) => u.source === 'local').length,
      ldap: all.filter((u) => u.source === 'ldap').length,
      disabled: all.filter((u) => u.status === 'disabled').length,
    }
  }, [users])

  const invalidate = () => qc.invalidateQueries({ queryKey: ['account-users'] })

  /** 正在改自己的账号。 */
  const editingSelf = !!editUser && editUser.id === me?.id
  /** 启用中的管理员只剩被编辑的这一号；再关开关平台就没人能进系统管理。 */
  const lastActiveAdmin =
    !!editUser?.is_admin && users.filter((u) => u.is_admin && u.status === 'active').length <= 1
  /** 编辑弹窗里的管理员开关锁为开：改自己，或平台只剩这一名启用管理员。 */
  const lockAdminSwitch = editingSelf || lastActiveAdmin

  const createMut = useMutation({
    mutationFn: (body: Record<string, unknown>) => post<AccountUser>('/account/users', body),
    onSuccess: () => {
      message.success(t('users.createdLocal'))
      setCreateOpen(false)
      createForm.resetFields()
      invalidate()
    },
  })
  const saveMut = useMutation({
    mutationFn: (body: { id: number } & Record<string, unknown>) => put<AccountUser>(`/account/users/${body.id}`, body),
    onSuccess: () => {
      message.success(t('users.saved'))
      setEditUser(null)
      invalidate()
    },
  })
  const pwdMut = useMutation({
    mutationFn: (body: { id: number; password: string }) =>
      post(`/account/users/${body.id}/reset-password`, { password: body.password }),
    onSuccess: () => {
      message.success(t('users.pwdReset'))
      setPwdUser(null)
      pwdForm.resetFields()
    },
  })
  const delMut = useMutation({
    mutationFn: (id: number) => del(`/account/users/${id}`),
    onSuccess: () => {
      message.success(t('project.deleted'))
      invalidate()
    },
  })
  const searchLdap = useMutation({
    mutationFn: () => post<LdapEntry[]>('/account/ldap/search', { keyword: ldapKw }),
    onSuccess: (rows) => {
      setLdapRows(rows)
      if (!rows.length) message.info(t('users.noLdapMatch'))
    },
  })
  const importLdap = useMutation({
    mutationFn: () => post<{ imported: number }>('/account/ldap/import', { users: ldapSelected }),
    onSuccess: (r) => {
      message.success(t('users.importedN', { n: r.imported }))
      setLdapOpen(false)
      setLdapSelected([])
      invalidate()
    },
  })
  const testLdap = useMutation({
    mutationFn: () => post<{ ok: boolean }>('/account/ldap/test', {}),
    onSuccess: () => message.success(t('users.ldapOk')),
  })

  return (
    <div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Card size="small">{t('users.peopleN', { n: stats.total })}</Card>
        <Card size="small">{t('users.localN', { n: stats.local })}</Card>
        <Card size="small">{t('users.ldapN', { n: stats.ldap })}</Card>
        <Card size="small">{t('users.disabledN', { n: stats.disabled })}</Card>
      </Space>
      <Card
        title={
          <Space>
            <UserOutlined />
            {t('menu.users')}
          </Space>
        }
        extra={
          <Space>
            <Button onClick={() => testLdap.mutate()} loading={testLdap.isPending}>
              {t('users.testLdap')}
            </Button>
            <Button icon={<CloudSyncOutlined />} onClick={() => setLdapOpen(true)}>
              {t('users.importLdap')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              {t('users.addLocal')}
            </Button>
          </Space>
        }
      >
        <Space wrap style={{ marginBottom: 12 }}>
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder={t('users.searchPh')}
            style={{ width: 240 }}
            value={kw}
            onChange={(e) => setKw(e.target.value)}
          />
          <Select
            allowClear
            placeholder={t('users.source')}
            style={{ width: 120 }}
            value={source}
            onChange={setSource}
            options={[
              { value: 'local', label: t('users.local') },
              { value: 'ldap', label: 'LDAP' },
              { value: 'wecom', label: t('users.wecom') },
            ]}
          />
          <Select
            allowClear
            placeholder={t('common.status')}
            style={{ width: 120 }}
            value={status}
            onChange={setStatus}
            options={[
              { value: 'active', label: t('common.enabled') },
              { value: 'disabled', label: t('users.disable') },
            ]}
          />
        </Space>
        <DataTable
          chromeKey="users"
          rowKey="id"
          size="small"
          loading={isFetching}
          dataSource={users}
          pagination={{ current: page, onChange: setPage }}
          columns={[
            { title: t('users.account'), dataIndex: 'username', width: 140 },
            { title: t('users.displayName'), dataIndex: 'display_name', width: 120 },
            { title: t('users.email'), dataIndex: 'email' },
            {
              title: t('users.loginMethod'),
              dataIndex: 'identities',
              width: 140,
              render: (_: string[] | undefined, r: AccountUser) => (
                <>
                  {(r.identities?.length ? r.identities : [r.source]).map((s) => (
                    <Tag key={s} color={SOURCE[s]?.color}>
                      {SOURCE[s]?.text || s}
                    </Tag>
                  ))}
                </>
              ),
            },
            {
              title: t('users.role'),
              dataIndex: 'is_admin',
              width: 90,
              render: (v: boolean) => (v ? <Tag color="gold">{t('layout.admin')}</Tag> : <Tag>{t('users.normal')}</Tag>),
            },
            {
              title: t('common.status'),
              dataIndex: 'status',
              width: 80,
              render: (s: string, r: AccountUser) => (
                <Switch
                  checked={s === 'active'}
                  checkedChildren={t('common.enabled')}
                  unCheckedChildren={t('users.disable')}
                  // 连点会发出多个请求，响应乱序时开关显示的状态和库里的对不上
                  loading={saveMut.isPending}
                  disabled={saveMut.isPending || r.id === me?.id}
                  onChange={(on) => saveMut.mutate({ id: r.id, status: on ? 'active' : 'disabled' })}
                />
              ),
            },
            { title: t('users.lastLogin'), dataIndex: 'last_login_at', width: 170, render: (v: string) => v.replace('T', ' ').slice(0, 19) || '—' },
            {
              title: t('common.action'),
              width: 220,
              render: (_: unknown, r: AccountUser) => (
                <Space>
                  <Button type="link" size="small" onClick={() => {
                    setEditUser(r)
                    editForm.setFieldsValue(r)
                  }}>
                    {t('common.edit')}
                  </Button>
                  {!(r.identities || []).includes('ldap') && r.source !== 'ldap' && (
                    <Button type="link" size="small" icon={<KeyOutlined />} onClick={() => setPwdUser(r)}>
                      {t('users.resetPwd')}
                    </Button>
                  )}
                  <Popconfirm title={t('users.deleteConfirm')} onConfirm={() => delMut.mutate(r.id)}>
                    <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                      {t('common.delete')}
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={t('users.addLocal')}
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false)
          createForm.resetFields()
        }}
        onOk={() => createForm.validateFields().then((v) => createMut.mutate(v))}
        confirmLoading={createMut.isPending}
        okText={t('common.create')}
      >
        <Form form={createForm} layout="vertical">
          <Form.Item name="username" label={t('users.account')} rules={[{ required: true }]}>
            <Input placeholder={t('users.usernamePh')} />
          </Form.Item>
          <Form.Item name="display_name" label={t('users.displayName')}>
            <Input />
          </Form.Item>
          <Form.Item name="email" label={t('users.email')}>
            <Input />
          </Form.Item>
          <Form.Item name="password" label={t('users.initPwd')} rules={[{ required: true, min: 6 }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item name="is_admin" label={t('layout.admin')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('users.editUser')}
        open={!!editUser}
        onCancel={() => setEditUser(null)}
        onOk={() =>
          editForm.validateFields().then((v) => {
            if (!editUser) return
            // 开关禁用时表单仍可能带上 false，提交时强制保留管理员，避免绕过界面打接口。
            saveMut.mutate({
              id: editUser.id,
              ...v,
              is_admin: lockAdminSwitch ? true : v.is_admin,
            })
          })
        }
        confirmLoading={saveMut.isPending}
      >
        <Form form={editForm} layout="vertical">
          <Form.Item label={t('users.account')}>
            <Input disabled value={editUser?.username} />
          </Form.Item>
          <Form.Item name="display_name" label={t('users.displayName')}>
            <Input />
          </Form.Item>
          <Form.Item name="email" label={t('users.email')}>
            <Input />
          </Form.Item>
          <Form.Item
            name="is_admin"
            label={t('layout.admin')}
            valuePropName="checked"
            extra={
              lockAdminSwitch
                ? editingSelf
                  ? t('users.cannotDemoteSelf')
                  : t('users.keepOneAdmin')
                : undefined
            }
          >
            <Switch disabled={lockAdminSwitch} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('users.resetPwdTitle', { name: pwdUser?.username || '' })}
        open={!!pwdUser}
        onCancel={() => setPwdUser(null)}
        onOk={() =>
          pwdForm.validateFields().then((v) => pwdUser && pwdMut.mutate({ id: pwdUser.id, password: v.password }))
        }
        confirmLoading={pwdMut.isPending}
      >
        <Form form={pwdForm} layout="vertical">
          <Form.Item name="password" label={t('users.newPwd')} rules={[{ required: true, min: 6 }]}>
            <Input.Password />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t('users.importLdapTitle')}
        width={720}
        open={ldapOpen}
        onCancel={() => {
          setLdapOpen(false)
          // 不清的话，下次打开还挂着上次的搜索结果和勾选，容易误导入
          setLdapKw('')
          setLdapRows([])
          setLdapSelected([])
        }}
        onOk={() => importLdap.mutate()}
        okButtonProps={{ disabled: !ldapSelected.length }}
        confirmLoading={importLdap.isPending}
        okText={t('users.importSelected', { n: ldapSelected.length })}
      >
        <Space style={{ marginBottom: 12 }}>
          <Input
            placeholder={t('users.ldapSearchPh')}
            value={ldapKw}
            onChange={(e) => setLdapKw(e.target.value)}
            onPressEnter={() => searchLdap.mutate()}
            style={{ width: 280 }}
          />
          <Button type="primary" loading={searchLdap.isPending} onClick={() => searchLdap.mutate()}>
            {t('common.search')}
          </Button>
        </Space>
        <DataTable
          chromeKey="users-ldap"
          rowKey="dn"
          size="small"
          dataSource={ldapRows}
          pagination={false}
          rowSelection={{
            // 受控才能在关闭弹窗时真正清掉勾选，否则重新搜到同一个人还是选中态
            selectedRowKeys: ldapSelected.map((r) => r.dn),
            onChange: (_keys, rows) => setLdapSelected(rows),
          }}
          columns={[
            { title: t('users.domainAccount'), dataIndex: 'username', width: 120 },
            { title: t('users.displayName'), dataIndex: 'display_name' },
            { title: t('users.email'), dataIndex: 'email' },
            { title: 'UPN', dataIndex: 'upn' },
          ]}
        />
      </Modal>
    </div>
  )
}
