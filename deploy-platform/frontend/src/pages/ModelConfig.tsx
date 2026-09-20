import { useState } from 'react'
import {
  Card,
  Tabs,
  Tag,
  Button,
  Modal,
  Form,
  Input,
  InputNumber,
  Switch,
  Select,
  Space,
  message,
  Alert,
  Empty,
  Popconfirm,
  Statistic,
  Tooltip,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  StarOutlined,
  ApiOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { get, del, post, postLong, put } from '@/api/client'
import type { ModelAdapter, ModelRoute } from '@/api/types'
import DataTable from '@/components/DataTable'
import { useAuthStore } from '@/stores/auth'
import { t as translate, useT } from '@/i18n'

interface TestResult {
  ok: boolean
  latency_ms: number
  reply: string
  error: string
  provider_name: string
  model_name: string
  model_id: string
}

function showTestResult(r: TestResult) {
  if (r.ok) {
    Modal.success({
      title: translate('models.testOk'),
      content: translate('models.testOkBody', {
        provider: r.provider_name,
        model: r.model_name,
        id: r.model_id,
        ms: r.latency_ms,
        reply: r.reply || translate('models.emptyReply'),
      }),
    })
  } else {
    Modal.error({
      title: translate('models.testFail'),
      content: translate('models.testFailBody', {
        provider: r.provider_name,
        model: r.model_name,
        id: r.model_id,
        latency: r.latency_ms ? translate('models.testLatency', { ms: r.latency_ms }) : '',
        error: r.error || translate('models.unknownError'),
      }),
    })
  }
}

interface Provider {
  id: number
  name: string
  code: string
  api_base_url: string
  api_key_masked: string
  has_api_key: boolean
  description: string
  website: string
  is_active: boolean
}

interface LlmModel {
  id: number
  name: string
  model_id: string
  provider_id: number
  provider_code: string
  provider_name: string
  description: string
  max_tokens: number
  context_window?: number
  temperature: number
  capabilities?: string[]
  supports_vision?: boolean
  is_active: boolean
  is_default: boolean
  ready: boolean
}

interface UsageUserRow {
  user_id: number
  username: string
  display_name: string
  requests: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  last_used_at: string
}

interface UsageSummary {
  days: number
  since: string
  totals: {
    users: number
    requests: number
    input_tokens: number
    output_tokens: number
    total_tokens: number
  }
  users: UsageUserRow[]
}

interface ObservationRow {
  id: number
  username?: string
  user_id?: number
  model_id?: string
  task?: string
  status?: string
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  duration_ms?: number
  usage_source?: string
  finished_at?: string
}

function formatTokens(n: number) {
  return (n || 0).toLocaleString()
}

export default function ModelConfig() {
  const t = useT()
  const qc = useQueryClient()
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  const [providerOpen, setProviderOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)
  const [routeOpen, setRouteOpen] = useState(false)
  const [editingProvider, setEditingProvider] = useState<Provider | null>(null)
  /** 正在编辑的模型；为空表示弹窗是「添加」。 */
  const [editingModel, setEditingModel] = useState<LlmModel | null>(null)
  const [pForm] = Form.useForm()
  const [mForm] = Form.useForm()
  const [routeForm] = Form.useForm()
  const [usageDays, setUsageDays] = useState(7)

  const { data: providers = [] } = useQuery({
    queryKey: ['llm-providers'],
    queryFn: () => get<Provider[]>('/llm/providers'),
  })
  const { data: models = [] } = useQuery({
    queryKey: ['llm-models'],
    queryFn: () => get<LlmModel[]>('/llm/models'),
  })
  const routesQuery = useQuery({
    queryKey: ['llm-routes'],
    queryFn: () => get<ModelRoute[]>('/llm/routes'),
    retry: false,
  })
  const adaptersQuery = useQuery({
    queryKey: ['llm-adapters'],
    queryFn: async () => {
      try {
        return await get<ModelAdapter[]>('/llm/adapters')
      } catch {
        const extensions = await get<Array<{
          id: number
          name: string
          version: string
          source?: string
          enabled?: boolean
          status?: string
          manifest?: { display_name?: string; capabilities?: string[] }
        }>>('/harness/components', { kind: 'model-adapter' })
        return extensions.map((item) => ({
          id: item.id,
          name: item.manifest?.display_name || item.name,
          source: item.source,
          version: item.version,
          enabled: item.enabled,
          status: item.status,
          capabilities: item.manifest?.capabilities,
        }))
      }
    },
    retry: false,
  })
  const observationsQuery = useQuery({
    queryKey: ['llm-observations'],
    queryFn: () => get<ObservationRow[]>('/llm/observations', { limit: 100 }),
    retry: false,
    enabled: isAdmin,
  })
  const usageQuery = useQuery({
    queryKey: ['llm-usage-users', usageDays],
    queryFn: () => get<UsageSummary>('/llm/usage/users', { days: usageDays }),
    retry: false,
    enabled: isAdmin,
  })

  const saveProvider = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      put(`/llm/providers/${editingProvider!.id}`, values),
    onSuccess: () => {
      message.success(t('models.providerSaved'))
      qc.invalidateQueries({ queryKey: ['llm-providers'] })
      qc.invalidateQueries({ queryKey: ['llm-models'] })
      setProviderOpen(false)
    },
  })

  const saveModel = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      editingModel ? put(`/llm/models/${editingModel.id}`, values) : post('/llm/models', values),
    onSuccess: () => {
      message.success(editingModel ? t('models.modelSaved') : t('models.modelAdded'))
      qc.invalidateQueries({ queryKey: ['llm-models'] })
      qc.invalidateQueries({ queryKey: ['assistant-models'] })
      setModelOpen(false)
      setEditingModel(null)
      mForm.resetFields()
    },
  })

  const patchModel = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      put(`/llm/models/${id}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['llm-models'] })
      qc.invalidateQueries({ queryKey: ['assistant-models'] })
    },
  })

  const deleteModel = useMutation({
    mutationFn: (id: number) => del(`/llm/models/${id}`),
    onSuccess: () => {
      message.success(t('models.modelDeleted'))
      qc.invalidateQueries({ queryKey: ['llm-models'] })
      qc.invalidateQueries({ queryKey: ['assistant-models'] })
    },
  })
  const saveRoute = useMutation({
    mutationFn: (values: Record<string, unknown>) => post('/llm/routes', values),
    onSuccess: () => {
      message.success(t('models.routeSaved'))
      qc.invalidateQueries({ queryKey: ['llm-routes'] })
      setRouteOpen(false)
      routeForm.resetFields()
    },
  })

  const [testingId, setTestingId] = useState<string | null>(null)
  const runTest = async (url: string, body?: Record<string, unknown>, id?: string) => {
    const key = id || url
    setTestingId(key)
    try {
      const r = await postLong<TestResult>(url, body || {}, 40000)
      showTestResult(r)
    } catch (e) {
      Modal.error({ title: t('models.testFail'), content: e instanceof Error ? e.message : String(e) })
    } finally {
      setTestingId(null)
    }
  }

  const openEditProvider = (p: Provider) => {
    setEditingProvider(p)
    pForm.setFieldsValue({
      name: p.name,
      api_base_url: p.api_base_url,
      api_key: '',
      is_active: p.is_active,
      description: p.description,
    })
    setProviderOpen(true)
  }

  /** 打开添加模型弹窗，清空表单。 */
  const openAddModel = () => {
    setEditingModel(null)
    mForm.resetFields()
    mForm.setFieldsValue({ max_tokens: 4096, temperature: 0.2, is_default: false, supports_vision: false })
    setModelOpen(true)
  }

  /** 用当前行填表，弹窗改成编辑已有模型。 */
  const openEditModel = (r: LlmModel) => {
    setEditingModel(r)
    mForm.setFieldsValue({
      provider_id: r.provider_id,
      name: r.name,
      model_id: r.model_id,
      max_tokens: r.max_tokens,
      context_window: r.context_window || 0,
      supports_vision: !!r.supports_vision,
      is_default: r.is_default,
    })
    setModelOpen(true)
  }

  return (
    <div>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('menu.models')}
        description={t('models.pageDesc')}
      />
      <Tabs
        items={[
          {
            key: 'providers',
            label: t('models.tabProviders', { n: providers.length }),
            children: (
              <Card>
                <DataTable
                  chromeKey="model-providers"
                  rowKey="id"
                  dataSource={providers}
                  pagination={false}
                  columns={[
                    { title: t('models.colProvider'), dataIndex: 'name', render: (v: string, r: Provider) => (
                      <Space>
                        {v}
                        <Tag>{r.code}</Tag>
                      </Space>
                    ) },
                    { title: t('models.apiUrl'), dataIndex: 'api_base_url', ellipsis: true },
                    {
                      title: 'API Key',
                      dataIndex: 'api_key_masked',
                      width: 160,
                      render: (v: string, r: Provider) =>
                        r.has_api_key ? <code>{v}</code> : <Tag>{t('models.notConfigured')}</Tag>,
                    },
                    {
                      title: t('common.enabled'),
                      dataIndex: 'is_active',
                      width: 80,
                      render: (v: boolean) => (v ? <Tag color="green">{t('models.yes')}</Tag> : <Tag>{t('models.no')}</Tag>),
                    },
                    {
                      title: t('common.action'),
                      width: 180,
                      render: (_: unknown, r: Provider) =>
                        isAdmin ? (
                        <Space>
                          <Button size="small" icon={<EditOutlined />} onClick={() => openEditProvider(r)}>
                            {t('models.configure')}
                          </Button>
                          <Button
                            size="small"
                            icon={<ApiOutlined />}
                            loading={testingId === `p-${r.id}`}
                            disabled={!r.has_api_key}
                            onClick={() => runTest(`/llm/providers/${r.id}/test`, {}, `p-${r.id}`)}
                          >
                            {t('models.test')}
                          </Button>
                        </Space>
                        ) : null,
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'models',
            label: t('models.tabModels', { n: models.length }),
            children: (
              <Card
                extra={
                  isAdmin ? (
                    <Button type="primary" icon={<PlusOutlined />} onClick={openAddModel}>
                      {t('models.addModel')}
                    </Button>
                  ) : null
                }
              >
                <DataTable
                  chromeKey="model-models"
                  rowKey="id"
                  dataSource={models}
                  pagination={false}
                  columns={[
                    {
                      title: t('models.colModel'),
                      dataIndex: 'name',
                      render: (v: string, r: LlmModel) => (
                        <Space>
                          {v}
                          {r.is_default && (
                            <Tag color="gold" icon={<StarOutlined />}>
                              {t('models.default')}
                            </Tag>
                          )}
                          {r.ready ? <Tag color="green">{t('models.ready')}</Tag> : <Tag>{t('models.missingKey')}</Tag>}
                        </Space>
                      ),
                    },
                    { title: 'model_id', dataIndex: 'model_id', render: (v: string) => <code>{v}</code> },
                    { title: t('models.colProvider'), dataIndex: 'provider_name' },
                    { title: 'max_tokens', dataIndex: 'max_tokens', width: 110 },
                    {
                      title: t('models.capability'),
                      width: 120,
                      render: (_: unknown, r: LlmModel) => (
                        <Tag
                          color={r.supports_vision ? 'purple' : 'default'}
                          style={{ cursor: isAdmin ? 'pointer' : 'default' }}
                          onClick={() =>
                            isAdmin &&
                            patchModel.mutate({
                              id: r.id,
                              body: { capabilities: r.supports_vision ? [] : ['vision'] },
                            })
                          }
                        >
                          {r.supports_vision ? t('models.vision') : t('models.textOnly')}
                        </Tag>
                      ),
                    },
                    {
                      title: t('common.action'),
                      width: 360,
                      render: (_: unknown, r: LlmModel) =>
                        isAdmin ? (
                        <Space wrap size={4}>
                          <Button size="small" icon={<EditOutlined />} onClick={() => openEditModel(r)}>
                            {t('models.modify')}
                          </Button>
                          <Button
                            size="small"
                            icon={<ApiOutlined />}
                            loading={testingId === `m-${r.id}`}
                            disabled={!r.ready}
                            onClick={() => runTest(`/llm/models/${r.id}/test`, {}, `m-${r.id}`)}
                          >
                            {t('models.test')}
                          </Button>
                          {!r.is_default && (
                            <Popconfirm title={t('models.setDefaultConfirm')} onConfirm={() => patchModel.mutate({ id: r.id, body: { is_default: true } })}>
                              <Button size="small">{t('models.setDefault')}</Button>
                            </Popconfirm>
                          )}
                          <Button
                            size="small"
                            onClick={() =>
                              patchModel.mutate({ id: r.id, body: { is_active: !r.is_active } })
                            }
                          >
                            {r.is_active ? t('common.disabled') : t('common.enabled')}
                          </Button>
                          <Tooltip title={r.is_default ? t('models.deleteDefaultHint') : undefined}>
                            <span>
                              <Popconfirm
                                title={t('models.deleteConfirm')}
                                description={t('models.deleteDesc')}
                                disabled={r.is_default}
                                onConfirm={() => deleteModel.mutate(r.id)}
                              >
                                <Button
                                  size="small"
                                  danger
                                  icon={<DeleteOutlined />}
                                  disabled={r.is_default || deleteModel.isPending}
                                >
                                  {t('common.delete')}
                                </Button>
                              </Popconfirm>
                            </span>
                          </Tooltip>
                        </Space>
                        ) : null,
                    },
                  ]}
                />
              </Card>
            ),
          },
          {
            key: 'routes',
            label: t('models.tabRoutes', { n: routesQuery.data?.length || 0 }),
            children: (
              <Card
                extra={
                  isAdmin ? (
                    <Button type="primary" icon={<PlusOutlined />} disabled={routesQuery.isError} onClick={() => setRouteOpen(true)}>
                      {t('models.addRoute')}
                    </Button>
                  ) : null
                }
              >
                {routesQuery.isError ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t('models.routesUnavailable')}
                    description={t('models.routesUnavailableDesc')}
                  />
                ) : (
                  <DataTable
                    chromeKey="model-routes"
                    rowKey="id"
                    dataSource={routesQuery.data || []}
                    pagination={false}
                    columns={[
                      { title: t('models.task'), dataIndex: 'task', render: (value: string) => <code>{value}</code> },
                      { title: t('models.primaryModel'), dataIndex: 'model_name' },
                      { title: t('models.fallbackModel'), dataIndex: 'fallback_model', render: (value: string) => value || '—' },
                      { title: t('models.strategy'), dataIndex: 'strategy', render: (value: string) => <Tag color="blue">{value || 'priority'}</Tag> },
                      { title: t('common.status'), dataIndex: 'enabled', width: 90, render: (value: boolean) => <Tag color={value === false ? 'default' : 'green'}>{value === false ? t('common.disabled') : t('common.enabled')}</Tag> },
                    ]}
                  />
                )}
              </Card>
            ),
          },
          {
            key: 'adapters',
            label: t('models.tabAdapters', { n: adaptersQuery.data?.length || 0 }),
            children: (
              <Card>
                {adaptersQuery.isError ? (
                  <Empty description={t('models.adaptersEmpty')} />
                ) : (
                  <DataTable
                    chromeKey="model-adapters"
                    rowKey="id"
                    dataSource={adaptersQuery.data || []}
                    pagination={false}
                    columns={[
                      { title: t('models.colAdapter'), dataIndex: 'name' },
                      { title: t('common.version'), dataIndex: 'version', width: 100 },
                      { title: t('models.source'), dataIndex: 'source' },
                      { title: t('models.capability'), dataIndex: 'capabilities', render: (values: string[] = []) => <Space wrap>{values.map((value) => <Tag key={value}>{value}</Tag>)}</Space> },
                      { title: t('common.status'), render: (_: unknown, row: ModelAdapter) => <Tag color={row.enabled ? 'green' : 'default'}>{row.enabled ? t('models.adapterOn') : row.status || t('models.adapterOff')}</Tag> },
                      {
                        title: t('common.action'),
                        width: 100,
                        render: (_: unknown, row: ModelAdapter) =>
                          isAdmin ? (
                          <Button
                            size="small"
                            onClick={async () => {
                              try {
                                await post(`/harness/components/${row.id}/enabled`, { enabled: !row.enabled })
                                message.success(row.enabled ? t('models.adapterDisabled') : t('models.adapterEnabled'))
                                adaptersQuery.refetch()
                              } catch {
                                message.error(t('models.adapterUpdateFail'))
                              }
                            }}
                          >
                            {row.enabled ? t('common.disabled') : t('common.enabled')}
                          </Button>
                          ) : null,
                      },
                    ]}
                  />
                )}
              </Card>
            ),
          },
          {
            key: 'usage',
            label: t('models.usage'),
            children: (
              <Card
                extra={
                  <Space>
                    <Select
                      value={usageDays}
                      style={{ width: 120 }}
                      onChange={setUsageDays}
                      options={[
                        { value: 1, label: t('models.today') },
                        { value: 7, label: t('models.lastDays', { n: 7 }) },
                        { value: 30, label: t('models.lastDays', { n: 30 }) },
                        { value: 90, label: t('models.lastDays', { n: 90 }) },
                      ]}
                    />
                    <Button icon={<ReloadOutlined />} onClick={() => usageQuery.refetch()}>{t('common.refresh')}</Button>
                  </Space>
                }
              >
                {usageQuery.isError ? (
                  <Alert type="warning" showIcon message={t('models.usageUnavailable')} description={t('models.usageUnavailableDesc')} />
                ) : (
                  <>
                    <Space size={32} style={{ marginBottom: 20 }} wrap>
                      <Statistic title={t('models.userCount')} value={usageQuery.data?.totals.users || 0} />
                      <Statistic title={t('models.callCount')} value={usageQuery.data?.totals.requests || 0} />
                      <Statistic title={t('models.inputTokens')} value={usageQuery.data?.totals.input_tokens || 0} />
                      <Statistic title={t('models.outputTokens')} value={usageQuery.data?.totals.output_tokens || 0} />
                      <Statistic title={t('models.totalTokens')} value={usageQuery.data?.totals.total_tokens || 0} />
                    </Space>
                    <DataTable
                      chromeKey="model-usage-users"
                      rowKey={(row) => String(row.user_id || row.username)}
                      dataSource={usageQuery.data?.users || []}
                      pagination={false}
                      locale={{ emptyText: <Empty description={t('models.usageEmpty')} /> }}
                      columns={[
                        {
                          title: t('models.user'),
                          dataIndex: 'display_name',
                          render: (_: string, row: UsageUserRow) => (
                            <Space>
                              {row.display_name || row.username}
                              {row.username && row.display_name !== row.username ? <Tag>{row.username}</Tag> : null}
                            </Space>
                          ),
                        },
                        { title: t('models.calls'), dataIndex: 'requests', width: 90 },
                        { title: t('models.input'), dataIndex: 'input_tokens', render: (v: number) => formatTokens(v) },
                        { title: t('models.output'), dataIndex: 'output_tokens', render: (v: number) => formatTokens(v) },
                        { title: t('models.total'), dataIndex: 'total_tokens', render: (v: number) => formatTokens(v) },
                        { title: t('models.lastUsed'), dataIndex: 'last_used_at', ellipsis: true },
                      ]}
                    />
                  </>
                )}
              </Card>
            ),
          },
          {
            key: 'observability',
            label: t('models.observe'),
            children: (
              <Card
                extra={<Button icon={<ReloadOutlined />} onClick={() => observationsQuery.refetch()}>{t('common.refresh')}</Button>}
              >
                {observationsQuery.isError ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t('models.observeUnavailable')}
                    description={t('models.observeUnavailableDesc')}
                  />
                ) : (
                  <>
                    <Space size={32} style={{ marginBottom: 20 }}>
                      <Statistic title={t('models.recentCalls')} value={(observationsQuery.data || []).length} />
                      <Statistic
                        title="Token"
                        value={(observationsQuery.data || []).reduce((sum, item) => sum + (item.total_tokens || 0), 0)}
                      />
                    </Space>
                    <DataTable
                      chromeKey="model-observations"
                      rowKey={(row) => String(row.id)}
                      dataSource={observationsQuery.data || []}
                      pagination={false}
                      columns={[
                        { title: t('models.time'), dataIndex: 'finished_at', ellipsis: true },
                        { title: t('models.user'), dataIndex: 'username', width: 120, render: (v: string) => v || '—' },
                        { title: t('models.colModel'), dataIndex: 'model_id' },
                        { title: t('models.task'), dataIndex: 'task', width: 100 },
                        { title: t('common.status'), dataIndex: 'status', width: 90 },
                        { title: t('models.input'), dataIndex: 'input_tokens', render: (v: number) => formatTokens(v || 0) },
                        { title: t('models.output'), dataIndex: 'output_tokens', render: (v: number) => formatTokens(v || 0) },
                        { title: t('models.total'), dataIndex: 'total_tokens', render: (v: number) => formatTokens(v || 0) },
                        { title: t('models.duration'), dataIndex: 'duration_ms', render: (v: number) => t('models.durationMs', { n: v ?? 0 }) },
                      ]}
                    />
                  </>
                )}
              </Card>
            ),
          },
        ]}
      />

      <Modal
        title={editingProvider ? t('models.configProviderNamed', { name: editingProvider.name }) : t('models.configProvider')}
        open={providerOpen}
        onCancel={() => setProviderOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setProviderOpen(false)}>
            {t('common.cancel')}
          </Button>,
          <Button
            key="test"
            icon={<ApiOutlined />}
            loading={testingId === `pform-${editingProvider?.id}`}
            onClick={async () => {
              if (!editingProvider) return
              const values = await pForm.validateFields(['api_base_url'])
              const body: Record<string, unknown> = { api_base_url: values.api_base_url }
              const key = pForm.getFieldValue('api_key')
              if (key) body.api_key = key
              await runTest(`/llm/providers/${editingProvider.id}/test`, body, `pform-${editingProvider.id}`)
            }}
          >
            {t('models.testConnection')}
          </Button>,
          <Button
            key="ok"
            type="primary"
            loading={saveProvider.isPending}
            onClick={async () => {
              const values = await pForm.validateFields()
              if (!values.api_key) delete values.api_key
              saveProvider.mutate(values)
            }}
          >
            {t('common.save')}
          </Button>,
        ]}
        destroyOnClose
      >
        <Form form={pForm} layout="vertical">
          <Form.Item name="name" label={t('models.displayName')}>
            <Input />
          </Form.Item>
          <Form.Item
            name="api_base_url"
            label={t('models.apiUrl')}
            extra={
              editingProvider?.code === 'tencent-lkeap'
                ? t('models.apiBaseTencent')
                : t('models.apiBaseOpenAI')
            }
          >
            <Input placeholder="https://tokenhub.tencentmaas.com/v1" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" extra={t('models.apiKeyExtra')}>
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Form.Item name="is_active" label={t('common.enabled')} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="description" label={t('models.notes')}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editingModel ? t('models.editModelNamed', { name: editingModel.name }) : t('models.addModel')}
        open={modelOpen}
        onCancel={() => {
          setModelOpen(false)
          setEditingModel(null)
        }}
        onOk={async () => {
          const values = await mForm.validateFields()
          const { supports_vision, ...rest } = values
          saveModel.mutate({
            ...rest,
            capabilities: supports_vision ? ['vision'] : [],
          })
        }}
        confirmLoading={saveModel.isPending}
        destroyOnClose
      >
        <Form form={mForm} layout="vertical" initialValues={{ max_tokens: 4096, temperature: 0.2, is_default: false }}>
          <Form.Item name="provider_id" label={t('models.colProvider')} rules={[{ required: true }]}>
            <Select
              options={providers.map((p) => ({ value: p.id, label: t('models.providerOption', { name: p.name, code: p.code }) }))}
            />
          </Form.Item>
          <Form.Item name="name" label={t('models.displayName')} rules={[{ required: true }]}>
            <Input placeholder="DeepSeek Chat" />
          </Form.Item>
          <Form.Item
            name="model_id"
            label="model_id"
            rules={[{ required: true }]}
            extra={t('models.modelIdExtra')}
          >
            <Input placeholder="deepseek-v4-pro" />
          </Form.Item>
          <Form.Item name="max_tokens" label="max_tokens">
            <InputNumber min={256} max={32768} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="context_window" label={t('models.contextWindow')} extra={t('models.contextWindowExtra')}>
            <InputNumber min={0} max={2000000} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            name="supports_vision"
            label={t('models.supportsVision')}
            valuePropName="checked"
            extra={t('models.supportsVisionExtra')}
          >
            <Switch />
          </Form.Item>
          <Form.Item name="is_default" label={t('models.setDefault')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={t('models.addTaskRoute')}
        open={routeOpen}
        onCancel={() => setRouteOpen(false)}
        confirmLoading={saveRoute.isPending}
        onOk={async () => saveRoute.mutate(await routeForm.validateFields())}
        destroyOnClose
      >
        <Form form={routeForm} layout="vertical" initialValues={{ strategy: 'priority', enabled: true }}>
          <Form.Item name="task" label={t('models.taskId')} rules={[{ required: true }]}>
            <Input placeholder="assistant.chat / pipeline.diagnose" />
          </Form.Item>
          <Form.Item name="model_id" label={t('models.primaryModel')} rules={[{ required: true }]}>
            <Select options={models.map((model) => ({ value: model.id, label: `${model.provider_name} / ${model.name}` }))} />
          </Form.Item>
          <Form.Item name="fallback_model_id" label={t('models.fallbackModel')}>
            <Select allowClear options={models.map((model) => ({ value: model.id, label: `${model.provider_name} / ${model.name}` }))} />
          </Form.Item>
          <Form.Item name="strategy" label={t('models.routeStrategy')}>
            <Select options={[
              { value: 'priority', label: t('models.strategyPriority') },
              { value: 'round-robin', label: t('models.strategyRoundRobin') },
              { value: 'latency', label: t('models.strategyLatency') },
            ]} />
          </Form.Item>
          <Form.Item name="enabled" label={t('common.enabled')} valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
