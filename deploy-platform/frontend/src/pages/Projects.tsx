import { Card, Button, Modal, Form, Input, Space, Tag, Typography, Popconfirm, Empty } from 'antd'
import DataTable from '@/components/DataTable'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, post } from '@/api/client'
import type { Project } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import CatalogTransferButtons from '@/components/CatalogTransfer'
import { useIsMobile } from '@/hooks/useIsMobile'
import { useT } from '@/i18n'

export default function Projects() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  /** 窄屏改卡片：宽表会把长项目名竖排成一字一行。 */
  const isMobile = useIsMobile()
  const t = useT()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()

  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
  })

  const [creating, setCreating] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const handleCreate = async () => {
    const values = await form.validateFields()
    setCreating(true)
    try {
      await post('/projects', values)
      queryClient.invalidateQueries({ queryKey: ['projects'] })
      setOpen(false)
      form.resetFields()
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (project: Project) => {
    setDeletingId(project.id)
    try {
      await del(`/projects/${project.id}`)
      queryClient.invalidateQueries({ queryKey: ['projects'] })
    } finally {
      setDeletingId(null)
    }
  }

  const closeCreate = () => {
    setOpen(false)
    form.resetFields()
  }

  const columns = useMemo(
    () => [
      {
        title: t('projects.colName'),
        dataIndex: 'name',
        render: (v: string, r: Project) => (
          <a onClick={() => navigate(`/projects/${r.id}`)}>{v}</a>
        ),
      },
      { title: t('projects.colCode'), dataIndex: 'code', width: 120, render: (v: string) => <Tag>{v}</Tag> },
      { title: t('common.description'), dataIndex: 'description', ellipsis: true },
      {
        title: t('common.status'),
        dataIndex: 'status',
        width: 100,
        render: (v: string) =>
          v === 'active' ? <Tag color="green">{t('projects.active')}</Tag> : <Tag>{t('projects.inactive')}</Tag>,
      },
      {
        title: t('common.action'),
        key: 'actions',
        width: isAdmin ? 180 : 100,
        render: (_: unknown, r: Project) => (
          <Space>
            <Button type="link" onClick={() => navigate(`/projects/${r.id}`)}>
              {t('projects.enter')}
            </Button>
            {isAdmin && (
              <Popconfirm
                title={t('projects.deleteConfirm', { name: r.name })}
                description={t('projects.deleteDesc')}
                okText={t('common.delete')}
                okButtonProps={{ danger: true, loading: deletingId === r.id }}
                cancelText={t('common.cancel')}
                onConfirm={() => handleDelete(r)}
              >
                <Button type="link" danger icon={<DeleteOutlined />}>
                  {t('common.delete')}
                </Button>
              </Popconfirm>
            )}
          </Space>
        ),
      },
    ],
    [t, isAdmin, deletingId, navigate],
  )

  return (
    <Card
      title={t('projects.title')}
      extra={
        <Space wrap>
          {!isMobile && (
            <CatalogTransferButtons
              onImported={() => queryClient.invalidateQueries({ queryKey: ['projects'] })}
            />
          )}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            {t('projects.createTitle')}
          </Button>
        </Space>
      }
    >
      {!isMobile && (
        <Typography.Paragraph type="secondary">
          {t('projects.hint')}
        </Typography.Paragraph>
      )}
      {isMobile ? (
        projects.length === 0 ? (
          <Empty description={t('projects.empty')} />
        ) : (
          <div className="mobile-entity-list">
            {projects.map((p) => (
              <button
                key={p.id}
                type="button"
                className="mobile-entity-card"
                onClick={() => navigate(`/projects/${p.id}`)}
              >
                <div className="mobile-entity-title">{p.name}</div>
                <div className="mobile-entity-meta">
                  <Tag>{p.code}</Tag>
                  {p.status === 'active' ? <Tag color="green">{t('projects.active')}</Tag> : <Tag>{t('projects.inactive')}</Tag>}
                </div>
              </button>
            ))}
          </div>
        )
      ) : (
        <DataTable chromeKey="projects" rowKey="id" columns={columns} dataSource={projects} pagination={false} />
      )}

      <Modal
        title={t('projects.createTitle')}
        open={open}
        onOk={handleCreate}
        onCancel={closeCreate}
        confirmLoading={creating}
        styles={{ content: { maxWidth: 'calc(100vw - 24px)' } }}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('projects.colName')} rules={[{ required: true }]}>
            <Input placeholder={t('projects.namePlaceholder')} />
          </Form.Item>
          <Form.Item name="code" label={t('projects.code')} rules={[{ required: true }]}>
            <Input placeholder={t('projects.codePlaceholder')} />
          </Form.Item>
          <Form.Item name="description" label={t('common.description')}>
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}
