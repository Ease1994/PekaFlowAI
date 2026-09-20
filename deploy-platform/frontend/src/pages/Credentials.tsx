import { Alert, Card, Tabs, Tag, Button, Modal, Form, Input, Select, Space, Popconfirm, message } from 'antd'
import DataTable from '@/components/DataTable'
import { PlusOutlined, DeleteOutlined, EditOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post, put, del } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useT } from '@/i18n'

interface Credential {
  id: number
  project_id: number | null
  project_name: string
  name: string
  type: string
  description: string
  ciphertext: string
  created_at: string
}

interface ProjectItem {
  id: number
  name: string
  code: string
}

/**
 * 按凭证类型切换录入框。
 *
 * 账号密码给华为云 SWR / 阿里云 ACR 等镜像仓库 docker login 用，必须分开填用户名和密码。
 * 编辑时留空表示不改密文。
 *
 * @param editing 是否正在改已有凭证；新建时用户名密码必填
 */
function CredentialSecretFields({ editing }: { editing: boolean }) {
  const t = useT()
  const form = Form.useFormInstance()
  const type = Form.useWatch('type', form) || 'token'
  if (type === 'password') {
    return (
      <>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('cred.registryHint')}
        />
        <Form.Item
          name="username"
          label={t('cred.username')}
          rules={editing ? [] : [{ required: true, message: t('cred.usernameRequired') }]}
        >
          <Input placeholder={t('cred.usernamePh')} autoComplete="off" />
        </Form.Item>
        <Form.Item
          name="password"
          label={t('cred.password')}
          rules={editing ? [] : [{ required: true, message: t('cred.passwordRequired') }]}
          extra={editing ? t('cred.passwordKeep') : t('cred.encrypted')}
        >
          <Input.Password placeholder={editing ? t('cred.leaveUnchanged') : t('cred.passwordPh')} />
        </Form.Item>
      </>
    )
  }
  const label = type === 'ssh' ? t('pipe.sshKey') : 'Token'
  return (
    <Form.Item
      name="secret"
      label={label}
      rules={editing ? [] : [{ required: true, message: t('cred.enterField', { label }) }]}
      extra={editing ? t('cred.secretKeep') : t('cred.encrypted')}
    >
      {type === 'ssh' ? (
        <Input.TextArea rows={4} placeholder={editing ? t('cred.leaveSecret') : t('cred.pasteKey')} />
      ) : (
        <Input.Password placeholder={editing ? t('cred.leaveSecret') : t('cred.tokenPh')} />
      )}
    </Form.Item>
  )
}

export default function Credentials() {
  const t = useT()
  const queryClient = useQueryClient()
  const isAdmin = useAuthStore((s) => s.user?.is_admin)
  const [tab, setTab] = useState('project')
  const [projectFilter, setProjectFilter] = useState<number>()
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)
  const [form] = Form.useForm()

  const { data: credentials = [] } = useQuery({
    queryKey: ['credentials', 'all'],
    queryFn: () => get<Credential[]>('/credentials', { scope: 'all' }),
  })
  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<ProjectItem[]>('/projects'),
  })

  const globalCreds = useMemo(() => credentials.filter((c) => !c.project_id), [credentials])
  const projectCreds = useMemo(
    () =>
      credentials.filter(
        (c) => c.project_id && (!projectFilter || c.project_id === projectFilter),
      ),
    [credentials, projectFilter],
  )

  const closeModal = () => {
    setOpen(false)
    setEditing(null)
    form.resetFields()
  }

  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async () => {
    const values = await form.validateFields()
    setSubmitting(true)
    try {
      const credType = values.type || 'token'
      const payload: Record<string, unknown> = {
        name: values.name,
        type: credType,
        description: values.description || '',
      }
      if (credType === 'password') {
        if (values.username || values.password) {
          if (!values.username || !values.password) {
            message.warning(t('cred.bothUserPass'))
            return
          }
          payload.username = values.username
          payload.password = values.password
        } else if (!editing) {
          message.warning(t('cred.needUserPass'))
          return
        }
      } else if (values.secret) {
        payload.secret = values.secret
      }
      if (editing) {
        await put(`/credentials/${editing.id}`, payload)
        message.success(t('cred.updatedHint'))
      } else {
        await post('/credentials', { ...payload, project_id: values.project_id ?? null })
        message.success(t('common.created'))
      }
      queryClient.invalidateQueries({ queryKey: ['credentials'] })
      closeModal()
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async (id: number) => {
    await del(`/credentials/${id}`)
    queryClient.invalidateQueries({ queryKey: ['credentials'] })
  }

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({
      type: 'token',
      project_id: tab === 'project' ? projectFilter ?? projects[0]?.id : null,
    })
    setOpen(true)
  }

  const openEdit = (row: Credential) => {
    setEditing(row)
    form.setFieldsValue({
      name: row.name,
      type: row.type,
      description: row.description,
      project_id: row.project_id,
      secret: '',
    })
    setOpen(true)
  }

  /** 凭证类型下拉：Token / SSH Key 保持原样，账号密码走现有 pipe 键。 */
  const typeOptions = [
    { label: 'Token', value: 'token' },
    { label: 'SSH Key', value: 'ssh' },
    { label: t('pipe.password'), value: 'password' },
  ]

  /** 列表上的类型标签，颜色按类型固定。 */
  const typeTag = (v: string) => {
    if (v === 'token') return <Tag color="blue">Token</Tag>
    if (v === 'ssh') return <Tag color="green">SSH Key</Tag>
    if (v === 'password') return <Tag color="orange">{t('pipe.password')}</Tag>
    return v
  }

  const projectOptions = projects.map((p) => ({
    value: p.id,
    label: t('cred.namedCode', { name: p.name, code: p.code }),
  }))

  const baseColumns = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      render: (v: string) => (
        <Space>
          <SafetyCertificateOutlined />
          {v}
        </Space>
      ),
    },
    { title: t('common.type'), dataIndex: 'type', width: 120, render: (v: string) => typeTag(v) },
    { title: t('cred.ciphertext'), dataIndex: 'ciphertext', render: (v: string) => <code>{v}</code> },
    { title: t('common.description'), dataIndex: 'description', ellipsis: true },
    {
      title: t('common.action'),
      width: 160,
      render: (_: unknown, r: Credential) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
            {t('cred.modify')}
          </Button>
          <Popconfirm title={t('cred.deleteConfirm')} onConfirm={() => handleDelete(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />}>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const projectColumns = [
    baseColumns[0],
    {
      title: t('cred.belongProject'),
      dataIndex: 'project_name',
      width: 180,
      render: (v: string) => <Tag color="geekblue">{v || '-'}</Tag>,
    },
    ...baseColumns.slice(1),
  ]

  return (
    <Card
      title={t('menu.credentials')}
      extra={
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          {t('cred.newCred')}
        </Button>
      }
    >
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'project',
            label: t('cred.projectTab', { n: credentials.filter((c) => c.project_id).length }),
            children: (
              <div>
                <Space style={{ marginBottom: 12 }}>
                  <span style={{ color: '#666' }}>{t('cred.projectColon')}</span>
                  <Select
                    style={{ width: 260 }}
                    allowClear
                    showSearch
                    optionFilterProp="label"
                    placeholder={t('cred.allProjects')}
                    value={projectFilter}
                    onChange={setProjectFilter}
                    options={projectOptions}
                  />
                </Space>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={t('cred.projectHint')}
                />
                <DataTable chromeKey="credentials-project" rowKey="id" columns={projectColumns} dataSource={projectCreds} pagination={false} />
              </div>
            ),
          },
          {
            key: 'global',
            label: t('cred.globalTab', { n: globalCreds.length }),
            children: (
              <div>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 12 }}
                  message={t('cred.globalHint')}
                />
                <DataTable chromeKey="credentials-global" rowKey="id" columns={baseColumns} dataSource={globalCreds} pagination={false} />
              </div>
            ),
          },
        ]}
      />

      <Modal
        title={editing ? t('cred.editCred') : t('cred.newCred')}
        open={open}
        onOk={handleSubmit}
        onCancel={closeModal}
        confirmLoading={submitting}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="project_id"
            label={t('cred.owner')}
            extra={isAdmin ? t('cred.ownerAdminExtra') : t('cred.ownerUserExtra')}
          >
            <Select
              allowClear={!!isAdmin}
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              placeholder={isAdmin ? t('cred.globalPh') : t('deploy.pickProject')}
              options={projectOptions}
            />
          </Form.Item>
          <Form.Item name="name" label={t('cred.credName')} rules={[{ required: true }]}>
            <Input placeholder={t('cred.namePh')} />
          </Form.Item>
          <Form.Item name="type" label={t('common.type')} initialValue="token">
            <Select options={typeOptions} />
          </Form.Item>
          <CredentialSecretFields editing={!!editing} />
          <Form.Item name="description" label={t('common.description')}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
