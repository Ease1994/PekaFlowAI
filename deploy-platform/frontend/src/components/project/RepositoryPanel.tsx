import { Tag, Button, Modal, Form, Input, Select, Space, Popconfirm } from 'antd'
import DataTable from '@/components/DataTable'
import { PlusOutlined, EditOutlined, DeleteOutlined, LinkOutlined } from '@ant-design/icons'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post, put, del } from '@/api/client'
import type { Repository, Credential } from '@/api/types'
import { useT } from '@/i18n'

/** 项目详情页的「代码库」Tab：仅显示/管理本项目代码库。 */
/** 从 Git URL 派生别名（蓝盾规范：group/project）。前端镜像逻辑，保持 UI 实时响应。 */
function deriveAlias(url: string): string {
  if (!url) return ''
  const u = url.trim()
  const m1 = u.match(/^https?:\/\/[^/]+\/(.+?)(?:\.git)?\/?$/)
  if (m1) return m1[1]
  const m2 = u.match(/^(?:ssh:\/\/[^@/]+@[^/:]+(?::\d+)?\/|[\w-]+@[^:]+:)(.+?)(?:\.git)?\/?$/)
  if (m2) return m2[1]
  return ''
}

export default function RepositoryPanel({ projectId }: { projectId: number }) {
  const t = useT()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Repository | null>(null)
  const [form] = Form.useForm()

  const { data: repos = [] } = useQuery({
    queryKey: ['repositories', projectId],
    queryFn: () => get<Repository[]>('/repositories', { project_id: projectId }),
  })

  const { data: creds = [] } = useQuery({
    queryKey: ['credentials', projectId],
    queryFn: () => get<Credential[]>('/credentials', { project_id: projectId }),
  })

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ provider: 'gitlab', default_branch: 'master' })
    setOpen(true)
  }

  const openEdit = (r: Repository) => {
    setEditing(r)
    form.setFieldsValue({
      name: r.name,
      alias: r.alias,
      url: r.url,
      provider: r.provider,
      default_branch: r.default_branch,
      credential_id: r.credential_id ?? undefined,
    })
    setOpen(true)
  }

  // URL 变化时实时同步别名（仅当 alias 为空或与旧 URL 派生结果一致时）
  const handleUrlChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const url = e.target.value
    const currentAlias = form.getFieldValue('alias')
    const prevUrl = editing?.url || ''
    const expectedAlias = deriveAlias(prevUrl)
    if (!currentAlias || currentAlias === expectedAlias) {
      form.setFieldsValue({ alias: deriveAlias(url) })
    }
  }

  const handleSave = async () => {
    const values = await form.validateFields()
    const payload = { ...values, project_id: projectId }
    if (editing) {
      await put(`/repositories/${editing.id}`, payload)
    } else {
      await post('/repositories', payload)
    }
    queryClient.invalidateQueries({ queryKey: ['repositories', projectId] })
    setOpen(false)
  }

  const handleDelete = async (id: number) => {
    await del(`/repositories/${id}`)
    queryClient.invalidateQueries({ queryKey: ['repositories', projectId] })
  }

  const columns = [
    {
      title: t('repo.alias'),
      dataIndex: 'alias',
      render: (v: string) => (
        <Space>
          <LinkOutlined />
          <code style={{ color: '#1677ff' }}>{v || '-'}</code>
        </Space>
      ),
    },
    { title: t('repo.url'), dataIndex: 'url', ellipsis: true },
    {
      title: t('repo.type'),
      dataIndex: 'provider',
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    { title: t('repo.defaultBranch'), dataIndex: 'default_branch' },
    {
      title: t('repo.credential'),
      dataIndex: 'credential_name',
      render: (v: string | null) =>
        v ? <Tag color="green">{v}</Tag> : <span style={{ color: '#999' }}>{t('repo.unlinked')}</span>,
    },
    {
      title: t('common.action'),
      width: 170,
      render: (_: unknown, r: Repository) => (
        <Space>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
            {t('common.edit')}
          </Button>
          <Popconfirm title={t('repo.deleteConfirm')} onConfirm={() => handleDelete(r.id)}>
            <Button size="small" danger icon={<DeleteOutlined />}>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          {t('repo.link')}
        </Button>
        <span style={{ marginLeft: 12, color: '#999', fontSize: 12 }}>
          {t('repo.hint')}
        </span>
      </div>
      <DataTable chromeKey="project-repos" rowKey="id" columns={columns} dataSource={repos} pagination={false} />

      <Modal
        title={editing ? t('repo.edit') : t('repo.link')}
        open={open}
        onOk={handleSave}
        onCancel={() => setOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="url"
            label={t('repo.urlLabel')}
            rules={[{ required: true, message: t('repo.urlRequired') }]}
          >
            <Input placeholder="https://gitlab.example.com/group/repo.git" onChange={handleUrlChange} />
          </Form.Item>
          <Form.Item
            name="alias"
            label={t('repo.aliasLabel')}
            tooltip={t('repo.aliasTip')}
          >
            <Input placeholder={t('repo.aliasPlaceholder')} />
          </Form.Item>
          <Form.Item name="provider" label={t('repo.type')} initialValue="gitlab">
            <Select
              options={[
                { label: 'GitLab', value: 'gitlab' },
                { label: 'GitHub', value: 'github' },
              ]}
            />
          </Form.Item>
          <Form.Item name="default_branch" label={t('repo.defaultBranch')} initialValue="master">
            <Input placeholder="master / main" />
          </Form.Item>
          <Form.Item name="credential_id" label={t('repo.credLabel')}>
            <Select
              allowClear
              placeholder={t('repo.credPlaceholder')}
              options={creds.map((c) => ({
                label: `${c.name}（${c.project_id ? t('repo.projectCred') : t('repo.globalCred')}）`,
                value: c.id,
              }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
