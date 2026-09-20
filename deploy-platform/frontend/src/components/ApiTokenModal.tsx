import { Modal, Form, Input, InputNumber, Button, Tag, Space, Popconfirm, Alert } from 'antd'
import DataTable from '@/components/DataTable'
import { PlusOutlined, DeleteOutlined, CopyOutlined, KeyOutlined } from '@ant-design/icons'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, post, del } from '@/api/client'
import { copyText } from '@/utils/clipboard'
import { useT } from '@/i18n'

interface ApiToken {
  id: number
  name: string
  expires_at: string | null
  last_used_at: string | null
  revoked: boolean
  created_at: string
}

/** 用户下拉框弹出的 API Token 管理（创建/列表/吊销）。 */
export default function ApiTokenModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient()
  const t = useT()
  const [form] = Form.useForm()
  const [newToken, setNewToken] = useState<string | null>(null)

  const { data: tokens = [] } = useQuery({
    queryKey: ['api-tokens'],
    queryFn: () => get<ApiToken[]>('/api-tokens'),
    enabled: open,
  })

  const handleCreate = async () => {
    const values = await form.validateFields()
    const r = await post<ApiToken & { token: string }>('/api-tokens', {
      name: values.name,
      days: values.days,
    })
    setNewToken(r.token)
    form.resetFields()
    queryClient.invalidateQueries({ queryKey: ['api-tokens'] })
  }

  const handleRevoke = async (id: number) => {
    await del(`/api-tokens/${id}`)
    queryClient.invalidateQueries({ queryKey: ['api-tokens'] })
  }

  const copyToken = () => {
    if (!newToken) return
    void copyText(newToken)
  }

  const columns = [
    { title: t('token.colName'), dataIndex: 'name' },
    {
      title: t('token.colExpires'),
      dataIndex: 'expires_at',
      render: (v: string | null) =>
        v ? new Date(v).toLocaleDateString() : <Tag color="purple">{t('token.forever')}</Tag>,
    },
    {
      title: t('token.colLastUsed'),
      dataIndex: 'last_used_at',
      render: (v: string | null) =>
        v ? new Date(v).toLocaleString() : <span style={{ color: '#999' }}>{t('token.unused')}</span>,
    },
    {
      title: t('token.colAction'),
      width: 90,
      render: (_: unknown, r: ApiToken) => (
        <Popconfirm title={t('token.revokeConfirm')} onConfirm={() => handleRevoke(r.id)}>
          <Button size="small" danger icon={<DeleteOutlined />}>
            {t('token.revoke')}
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Modal
      title={
        <Space>
          <KeyOutlined />
          {t('token.title')}
        </Space>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={680}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('token.info')}
        description={t('token.infoDetail')}
      />

      <Form form={form} layout="inline" style={{ marginBottom: 8, rowGap: 8 }}>
        <Form.Item name="name" rules={[{ required: true, message: t('token.nameRequired') }]} style={{ flex: 1 }}>
          <Input placeholder={t('token.namePlaceholder')} prefix={<KeyOutlined />} />
        </Form.Item>
        <Form.Item name="days" initialValue={365} rules={[{ required: true }]}>
          <InputNumber min={1} max={1095} addonAfter={t('token.days')} style={{ width: 140 }} />
        </Form.Item>
        <Form.Item>
          <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
            {t('token.create')}
          </Button>
        </Form.Item>
      </Form>
      <div style={{ marginBottom: 12, color: '#999', fontSize: 12 }}>{t('token.ttlHint')}</div>

      {newToken && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('token.copyNow')}
          description={
            <Space.Compact style={{ width: '100%' }}>
              <Input value={newToken} readOnly onFocus={(e) => e.target.select()} />
              <Button icon={<CopyOutlined />} onClick={copyToken}>
                {t('token.copy')}
              </Button>
            </Space.Compact>
          }
        />
      )}

      <DataTable chromeKey="api-tokens" rowKey="id" columns={columns} dataSource={tokens} pagination={false} size="small" />
    </Modal>
  )
}
