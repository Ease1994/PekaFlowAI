import { useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Popconfirm,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd'
import { ApiOutlined, CloudServerOutlined, DownloadOutlined, ReloadOutlined, UploadOutlined } from '@ant-design/icons'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { del, get, getBlob, post, postForm } from '@/api/client'
import type {
  HarnessComponent,
  HarnessLifecycleEvent,
  HarnessRuntime,
  HarnessTool,
  Plugin,
  PluginDraft,
} from '@/api/types'
import PluginDraftDrawer from '@/components/PluginDraftDrawer'
import DataTable from '@/components/DataTable'
import { useAuthStore } from '@/stores/auth'
import { t, useT } from '@/i18n'

/** 流水线插件分类：颜色固定，文案随当前界面语言取。 */
function categoryItem(v: string): { color: string; text: string } {
  const colors: Record<string, string> = {
    source: 'blue',
    build: 'geekblue',
    deploy: 'red',
    notify: 'purple',
    trigger: 'cyan',
    exec: 'gold',
    artifact: 'magenta',
    pipeline: 'orange',
  }
  const keys: Record<string, string> = {
    source: 'store.catSource',
    build: 'store.catBuild',
    deploy: 'store.catDeploy',
    notify: 'store.catNotify',
    trigger: 'store.catTrigger',
    exec: 'store.catExec',
    artifact: 'store.catArtifact',
    pipeline: 'store.catPipeline',
  }
  const key = keys[v]
  return { color: colors[v] || 'default', text: key ? t(key) : v }
}

/** 插件草稿审批状态。 */
function draftStatusItem(v: string): { color: string; text: string } {
  const colors: Record<string, string> = {
    pending: 'orange',
    published: 'green',
    rejected: 'red',
  }
  const keys: Record<string, string> = {
    pending: 'store.draftPending',
    published: 'store.draftPublished',
    rejected: 'store.draftRejected',
  }
  const key = keys[v]
  return { color: colors[v] || 'default', text: key ? t(key) : v }
}

/** 内置助手工具分类名。 */
function builtinCategoryLabel(v?: string): string {
  const keys: Record<string, string> = {
    catalog: 'store.toolCatCatalog',
    observe: 'store.toolCatObserve',
    diagnose: 'store.toolCatDiagnose',
    delivery: 'store.toolCatDelivery',
    access: 'store.toolCatAccess',
    authoring: 'store.toolCatAuthoring',
    meta: 'store.toolCatMeta',
  }
  const key = keys[v || '']
  return key ? t(key) : v || '-'
}

/** 工具风险等级。 */
function riskItem(v?: string): { color: string; text: string } {
  const code = v || 'read'
  const colors: Record<string, string> = {
    read: 'blue',
    write: 'orange',
    destructive: 'red',
  }
  const keys: Record<string, string> = {
    read: 'store.riskRead',
    write: 'store.riskWrite',
    destructive: 'store.riskDestructive',
  }
  const key = keys[code]
  return { color: colors[code] || 'default', text: key ? t(key) : code }
}

/** 运行时健康状态。 */
function healthItem(status: string): { color: string; text: string } {
  const colors: Record<string, string> = {
    healthy: 'green',
    degraded: 'gold',
    unhealthy: 'red',
    unknown: 'default',
  }
  const keys: Record<string, string> = {
    healthy: 'store.healthOk',
    degraded: 'store.healthDegraded',
    unhealthy: 'store.healthBad',
    unknown: 'store.healthUnknown',
  }
  const key = keys[status]
  return { color: colors[status] || 'default', text: key ? t(key) : status }
}

/**
 * 运行时健康标签。unknown 是还没检查，不是故障。
 * @param status 后端 health_status
 * @param message 检查说明，作悬停说明
 */
function healthTag(status?: string, message?: string) {
  if (!status) return null
  const item = healthItem(status)
  const tag = <Tag color={item.color}>{item.text}</Tag>
  return message ? <Tooltip title={message}>{tag}</Tooltip> : tag
}

/** 启用 / 停用 / 卸载，不把英文 status 直接铺到页面上。 */
function lifecycleStatusTag(row: HarnessComponent) {
  if (row.enabled) return <Tag color="green">{t('store.enabledTag')}</Tag>
  if (row.status === 'uninstalled') return <Tag>{t('store.uninstalled')}</Tag>
  if (row.status === 'error') return <Tag color="red">{t('store.healthBad')}</Tag>
  return <Tag>{t('store.disabledTag')}</Tag>
}

/**
 * 带 JWT 拉取 zip 并触发浏览器下载。
 * 内置插件/技能不能改平台副本，下载下来改标识后再上传才是第三方包。
 */
async function downloadTemplate(path: string, filename: string) {
  try {
    const blob = await getBlob(path)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    message.success(t('store.downloadStarted', { filename }))
  } catch {
    /* 错误已由请求拦截器提示 */
  }
}

/** 拼下载文件名：{标识}-{版本}.zip */
function storeZipName(name: string, version?: string) {
  return `${name}-${version || '1.0.0'}.zip`
}

export default function Store() {
  const t = useT()
  const queryClient = useQueryClient()
  const user = useAuthStore((s) => s.user)
  const isAdmin = !!user?.is_admin
  const userId = user?.id
  const [draftId, setDraftId] = useState<number | null>(null)
  const [resourceComponent, setResourceComponent] = useState<HarnessComponent | null>(null)
  const { data: plugins = [] } = useQuery({
    queryKey: ['plugins'],
    queryFn: () => get<Plugin[]>('/store/plugins'),
  })

  const { data: drafts = [] } = useQuery({
    queryKey: ['plugin-drafts'],
    queryFn: () => get<PluginDraft[]>('/store/plugin-drafts'),
  })

  const { data: templates = [] } = useQuery({
    queryKey: ['templates'],
    queryFn: () => get<Plugin[]>('/store/templates'),
  })

  const componentsQuery = useQuery({
    queryKey: ['harness-components'],
    queryFn: () => get<HarnessComponent[]>('/harness/components'),
    retry: false,
  })
  const runtimeQuery = useQuery({
    queryKey: ['harness-runtime'],
    queryFn: () => get<HarnessRuntime>('/harness/runtime'),
    retry: false,
  })
  const eventsQuery = useQuery({
    queryKey: ['harness-events'],
    queryFn: () => get<HarnessLifecycleEvent[]>('/harness/events'),
    retry: false,
  })
  const builtinToolsQuery = useQuery({
    queryKey: ['harness-tools'],
    queryFn: () => get<HarnessTool[]>('/harness/tools'),
    retry: false,
  })

  const refreshHarness = () => {
    queryClient.invalidateQueries({ queryKey: ['harness-components'] })
    queryClient.invalidateQueries({ queryKey: ['harness-runtime'] })
    queryClient.invalidateQueries({ queryKey: ['harness-events'] })
    queryClient.invalidateQueries({ queryKey: ['harness-tools'] })
    queryClient.invalidateQueries({ queryKey: ['ai-skills'] })
  }

  /** 商店安装会改 installed 标记，编排器读的是 ['plugins','installed']，必须一起刷。 */
  const refreshPlugins = () => {
    queryClient.invalidateQueries({ queryKey: ['plugins'] })
    refreshHarness()
  }

  const lifecycleMut = useMutation({
    mutationFn: ({
      component,
      action,
    }: {
      component: HarnessComponent
      action: 'enable' | 'disable' | 'uninstall' | 'health' | 'reinstall' | 'purge'
    }) => {
      if (action === 'purge') {
        return del(`/harness/components/${component.id}`)
      }
      if (action === 'enable' || action === 'disable') {
        return post(`/harness/components/${component.id}/enabled`, { enabled: action === 'enable' })
      }
      return post(`/harness/components/${component.id}/${action}`)
    },
    onSuccess: (_, variables) => {
      const tips: Record<string, string> = {
        uninstall: t('store.uninstalledComponent'),
        reinstall: t('store.reinstalled'),
        enable: t('store.enabledNow'),
        purge: t('store.purgedFromStore'),
      }
      message.success(tips[variables.action] || t('store.componentUpdated'))
      refreshHarness()
    },
  })

  const installMut = useMutation({
    mutationFn: (id: number) => post(`/store/plugins/${id}/install`),
    onSuccess: () => {
      message.success(t('store.pluginInstalled'))
      refreshPlugins()
    },
  })
  const uninstallMut = useMutation({
    mutationFn: (id: number) => post(`/store/plugins/${id}/uninstall`),
    onSuccess: () => {
      message.success(t('store.uninstalled'))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      refreshHarness()
    },
  })
  const deletePluginMut = useMutation({
    mutationFn: (id: number) => del(`/store/plugins/${id}`),
    onSuccess: () => {
      message.success(t('store.deletedFromRepo'))
      queryClient.invalidateQueries({ queryKey: ['plugins'] })
      refreshHarness()
    },
  })
  const deleteDraftMut = useMutation({
    mutationFn: (id: number) => del(`/store/plugin-drafts/${id}`),
    onSuccess: () => {
      message.success(t('store.draftDeleted'))
      queryClient.invalidateQueries({ queryKey: ['plugin-drafts'] })
    },
  })

  const pluginColumns = [
    { title: t('store.plugin'), dataIndex: 'display_name' },
    {
      title: t('store.category'),
      dataIndex: 'category',
      render: (v: string) => {
        const c = categoryItem(v)
        return <Tag color={c.color}>{c.text}</Tag>
      },
    },
    { title: t('store.identifier'), dataIndex: 'name', render: (v: string) => <code>{v}</code> },
    { title: t('common.version'), dataIndex: 'version', width: 90 },
    { title: t('store.language'), dataIndex: 'language', width: 80, render: (v: string) => v || '—' },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 150,
      render: (_: string, row: Plugin) => (
        <Space size={4}>
          {row.installed ? <Tag color="green">{t('store.installed')}</Tag> : <Tag>{t('store.notInstalled')}</Tag>}
          {row.builtin ? <Tag>{t('store.builtin')}</Tag> : <Tag color="cyan">{t('store.thirdParty')}</Tag>}
        </Space>
      ),
    },
    { title: t('common.description'), dataIndex: 'description', ellipsis: true },
    {
      title: t('common.action'),
      width: 360,
      render: (_: unknown, row: Plugin) => {
        const canDownload = Boolean(row.has_package)
        return (
          <Space size={4} wrap>
            <Tooltip
              title={
                canDownload
                  ? t('store.downloadSrcTip')
                  : t('store.noPackageTip')
              }
            >
              <span>
                <Button
                  size="small"
                  icon={<DownloadOutlined />}
                  disabled={!canDownload}
                  onClick={() =>
                    downloadTemplate(
                      `/store/plugins/${encodeURIComponent(row.name)}/package`,
                      storeZipName(row.name, row.version),
                    )
                  }
                >
                  {t('store.download')}
                </Button>
              </span>
            </Tooltip>
            {row.builtin ? (
              <span style={{ color: '#999' }}>{t('store.cannotUninstall')}</span>
            ) : isAdmin ? (
              <>
                {row.installed ? (
                  <Popconfirm title={t('store.uninstallPluginConfirm')} onConfirm={() => uninstallMut.mutate(row.id)}>
                    <Button size="small">{t('store.uninstall')}</Button>
                  </Popconfirm>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    disabled={!row.package_path}
                    onClick={() => installMut.mutate(row.id)}
                  >
                    {t('store.install')}
                  </Button>
                )}
                <Popconfirm
                  title={t('store.deletePluginConfirm')}
                  onConfirm={() => deletePluginMut.mutate(row.id)}
                >
                  <Button size="small" danger>
                    {t('common.delete')}
                  </Button>
                </Popconfirm>
              </>
            ) : row.installed ? (
              <span style={{ color: '#999' }}>{t('store.installed')}</span>
            ) : (
              <span style={{ color: '#999' }}>{t('store.waitAdminInstall')}</span>
            )}
          </Space>
        )
      },
    },
  ]

  const draftColumns = [
    { title: t('store.plugin'), dataIndex: 'display_name', render: (v: string, row: PluginDraft) => v || row.name },
    { title: t('store.identifier'), dataIndex: 'name', render: (v: string) => <code>{v}</code> },
    { title: t('common.version'), dataIndex: 'version', width: 90 },
    {
      title: t('store.source'),
      dataIndex: 'source',
      width: 100,
      render: (v: string) => (v === 'ai' ? <Tag color="purple">{t('store.aiDraft')}</Tag> : <Tag>{t('store.manual')}</Tag>),
    },
    {
      title: t('store.lint'),
      width: 160,
      render: (_: unknown, row: PluginDraft) => {
        const high = row.lint?.high_count || 0
        const warn = row.lint?.warn_count || 0
        return (
          <Space size={4}>
            {high > 0 ? <Tag color="volcano">{t('store.highRiskN', { n: high })}</Tag> : <Tag color="green">{t('store.noHighRisk')}</Tag>}
            {warn > 0 ? <Tag color="gold">{t('store.warnN', { n: warn })}</Tag> : null}
          </Space>
        )
      },
    },
    {
      title: t('store.trial'),
      dataIndex: 'trial_status',
      width: 100,
      render: (v: string) =>
        v ? (
          <Tag color={v === 'success' ? 'green' : v === 'failed' ? 'red' : 'blue'}>{v}</Tag>
        ) : (
          <span style={{ color: '#999' }}>{t('store.notTried')}</span>
        ),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      width: 90,
      render: (v: string) => {
        const s = draftStatusItem(v)
        return <Tag color={s.color}>{s.text}</Tag>
      },
    },
    {
      title: t('common.action'),
      width: 160,
      render: (_: unknown, row: PluginDraft) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => setDraftId(row.id)}>
            {t('store.reviewCode')}
          </Button>
          <Popconfirm title={t('store.deleteIrreversible')} onConfirm={() => deleteDraftMut.mutate(row.id)}>
            <Button size="small" type="link" danger>
              {t('common.delete')}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  const templateColumns = [
    { title: t('store.templateName'), dataIndex: 'name' },
    { title: t('common.description'), dataIndex: 'description', ellipsis: true },
    { title: t('common.version'), dataIndex: 'version', width: 90 },
    {
      title: t('store.shareScope'),
      dataIndex: 'scope',
      render: (v: string) => (v === 'public' ? <Tag color="green">{t('store.public')}</Tag> : <Tag>{t('store.private')}</Tag>),
    },
  ]

  const components = componentsQuery.data || []
  const skills = components.filter((item) => item.kind === 'agent-skill')
  const tools = components.filter((item) => item.kind === 'agent-tool')
  /** 内置助手工具，不是技能包。展示在 Agent 工具 → 内置工具。 */
  const builtinTools = (builtinToolsQuery.data || []).filter((item) => item.source === 'builtin')
  const componentName = (row: HarnessComponent) => row.manifest.display_name || row.name
  const canManageComponent = (row: HarnessComponent) =>
    isAdmin ||
    (row.kind === 'agent-skill' &&
      (row.owner_user_id === userId || (!row.owner_user_id && row.publisher_id === userId)))

  /** 卸载只改状态；删除才从仓库拿走。个人技能主人、全员技能发布者或管理员可下架。别人不能删。 */
  const lifecycleActions = (row: HarnessComponent) =>
    canManageComponent(row) ? (
    <Space size={4} wrap>
      {row.status === 'uninstalled' ? (
        <Button
          size="small"
          type="primary"
          onClick={() => lifecycleMut.mutate({ component: row, action: 'reinstall' })}
        >
          {t('store.install')}
        </Button>
      ) : (
        <Button
          size="small"
          onClick={() =>
            lifecycleMut.mutate({ component: row, action: row.enabled ? 'disable' : 'enable' })
          }
        >
          {row.enabled ? t('common.disabled') : t('common.enabled')}
        </Button>
      )}
      {row.status !== 'uninstalled' ? (
        <Button size="small" onClick={() => lifecycleMut.mutate({ component: row, action: 'health' })}>
          {t('store.check')}
        </Button>
      ) : null}
      {row.status === 'uninstalled' ? null : (
        <Popconfirm title={t('store.uninstallConfirm', { name: componentName(row) })} onConfirm={() => lifecycleMut.mutate({ component: row, action: 'uninstall' })}>
          <Button size="small">{t('store.uninstall')}</Button>
        </Popconfirm>
      )}
      <Popconfirm
        title={t('store.purgeConfirm', { name: componentName(row) })}
        onConfirm={() => lifecycleMut.mutate({ component: row, action: 'purge' })}
      >
        <Button size="small" danger>
          {t('common.delete')}
        </Button>
      </Popconfirm>
    </Space>
    ) : null

  const skillColumns = [
    {
      title: t('store.skill'),
      render: (_: unknown, row: HarnessComponent) => (
        <Space direction="vertical" size={0}>
          <b>{componentName(row)}</b>
          <span style={{ color: '#999', fontSize: 12 }}>{row.manifest.description || row.key}</span>
        </Space>
      ),
    },
    { title: t('common.version'), dataIndex: 'version', width: 90 },
    {
      title: t('store.scope'),
      width: 90,
      render: (_: unknown, row: HarnessComponent) =>
        row.owner_user_id ? <Tag color="purple">{t('store.personal')}</Tag> : <Tag>{t('store.everyone')}</Tag>,
    },
    {
      title: t('store.capabilities'),
      width: 220,
      render: (_: unknown, row: HarnessComponent) => (
        <Space size={[4, 4]} wrap>
          {(row.manifest.capabilities || []).map((value) => <Tag key={value}>{value}</Tag>)}
        </Space>
      ),
    },
    {
      title: t('common.status'),
      width: 160,
      render: (_: unknown, row: HarnessComponent) => (
        <Space size={4}>
          {lifecycleStatusTag(row)}
          {row.status === 'uninstalled' ? null : healthTag(row.health_status, row.health_message)}
        </Space>
      ),
    },
    {
      title: t('common.action'),
      width: 420,
      render: (_: unknown, row: HarnessComponent) => (
        <Space wrap>
          <Tooltip title={t('store.downloadSkillTip')}>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              onClick={() =>
                downloadTemplate(
                  `/harness/components/${row.id}/package`,
                  storeZipName(row.name, row.version),
                )
              }
            >
              {t('store.download')}
            </Button>
          </Tooltip>
          <Button size="small" onClick={() => setResourceComponent(row)}>{t('store.resources')}</Button>
          {lifecycleActions(row)}
        </Space>
      ),
    },
  ]

  const toolColumns = [
    {
      title: t('store.tool'),
      render: (_: unknown, row: HarnessComponent) => (
        <Space direction="vertical" size={0}>
          <b>{componentName(row)}</b>
          <code style={{ fontSize: 12 }}>{row.manifest.entrypoint || row.key}</code>
        </Space>
      ),
    },
    {
      title: t('store.signature'),
      width: 120,
      render: (_: unknown, row: HarnessComponent) => {
        const metadata = row.manifest.metadata || {}
        const signed = Boolean(metadata.signed ?? metadata.signature)
        return <Tag color={signed ? 'green' : 'red'}>{signed ? t('store.verified') : t('store.unverified')}</Tag>
      },
    },
    {
      title: t('store.isolation'),
      width: 130,
      render: (_: unknown, row: HarnessComponent) => {
        const metadata = row.manifest.metadata || {}
        return <Tag color="blue">{String(metadata.isolation || metadata.sandbox || t('store.processIsolation'))}</Tag>
      },
    },
    {
      title: 'Schema',
      width: 100,
      render: (_: unknown, row: HarnessComponent) => (
        <Tag color={Object.keys(row.manifest.config_schema || {}).length ? 'cyan' : 'default'}>
          {Object.keys(row.manifest.config_schema || {}).length ? t('store.schemaDeclared') : t('store.none')}
        </Tag>
      ),
    },
    {
      title: t('store.metrics'),
      width: 180,
      render: (_: unknown, row: HarnessComponent) => {
        const metrics = (row.manifest.metadata?.metrics || {}) as Record<string, unknown>
        return Object.keys(metrics).length ? (
          <span>{Object.entries(metrics).slice(0, 2).map(([k, v]) => `${k}: ${String(v)}`).join(' · ')}</span>
        ) : <span style={{ color: '#999' }}>{t('store.noMetrics')}</span>
      },
    },
    {
      title: t('common.status'),
      width: 160,
      render: (_: unknown, row: HarnessComponent) => (
        <Space size={4}>
          {lifecycleStatusTag(row)}
          {row.status === 'uninstalled' ? null : healthTag(row.health_status, row.health_message)}
        </Space>
      ),
    },
    { title: t('common.action'), width: 360, render: (_: unknown, row: HarnessComponent) => (
      <Space wrap>
        <Tooltip title={t('store.downloadToolTip')}>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() =>
              downloadTemplate(
                `/harness/components/${row.id}/package`,
                storeZipName(row.name, row.version),
              )
            }
          >
            {t('store.download')}
          </Button>
        </Tooltip>
        {lifecycleActions(row)}
      </Space>
    ) },
  ]

  const builtinColumns = [
    {
      title: t('store.tool'),
      render: (_: unknown, row: HarnessTool) => (
        <Space direction="vertical" size={0}>
          <Space size={8}>
            <b>{row.display_name || row.name}</b>
            <Tag>{t('store.builtin')}</Tag>
          </Space>
          <code style={{ fontSize: 12 }}>{row.name}</code>
        </Space>
      ),
    },
    {
      title: t('store.category'),
      width: 90,
      render: (_: unknown, row: HarnessTool) => builtinCategoryLabel(row.category),
    },
    {
      title: t('store.risk'),
      width: 90,
      render: (_: unknown, row: HarnessTool) => {
        const risk = riskItem(row.risk)
        return <Tag color={risk.color}>{risk.text}</Tag>
      },
    },
    {
      title: t('store.isolation'),
      width: 100,
      render: () => <Tag color="geekblue">{t('store.inProcess')}</Tag>,
    },
    {
      title: t('common.description'),
      ellipsis: true,
      dataIndex: 'description',
    },
    {
      title: t('common.action'),
      width: 140,
      render: (_: unknown, row: HarnessTool) => (
        <Tooltip title={t('store.downloadBuiltinTip')}>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() =>
              downloadTemplate(
                `/harness/tools/${encodeURIComponent(row.name)}/skill-package`,
                `${row.name}-skill-1.0.0.zip`,
              )
            }
          >
            {t('store.download')}
          </Button>
        </Tooltip>
      ),
    },
  ]

  const harnessFallback = componentsQuery.isError

  return (
    <Card
      title={t('menu.skills')}
      extra={
        <Button icon={<ReloadOutlined />} onClick={refreshHarness}>
          {t('store.refreshRuntime')}
        </Button>
      }
    >
      {harnessFallback && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('store.harnessUnavailable')}
          description={t('store.harnessUnavailableDesc')}
        />
      )}
      <Tabs
        items={[
          {
            key: 'pipeline',
            label: t('store.pipelineTab', { plugins: plugins.length, drafts: drafts.filter((d) => d.status === 'pending').length }),
            children: (
              <>
                <p style={{ color: '#666' }}>
                  {t('store.pipelineIntro')}
                </p>
                <Tabs
                  type="card"
                  defaultActiveKey={drafts.some((d) => d.status === 'pending') ? 'drafts' : 'plugins'}
                  items={[
                    {
                      key: 'plugins',
                      label: t('store.pluginsTab', { n: plugins.length }),
                      children: (
                        <>
                          <div style={{ marginBottom: 12, textAlign: 'right' }}>
                            <Space>
                              <Button
                                icon={<DownloadOutlined />}
                                onClick={() =>
                                  downloadTemplate('/store/plugins/template', 'rp-pipeline-plugin-template.zip')
                                }
                              >
                                {t('store.downloadPluginTemplate')}
                              </Button>
                              {isAdmin ? (
                              <Upload
                                accept=".zip"
                                showUploadList={false}
                                customRequest={async (opt) => {
                                  try {
                                    const form = new FormData()
                                    form.append('file', opt.file as File)
                                    await postForm('/store/plugins/upload', form)
                                    message.success(t('store.uploadedInstall'))
                                    refreshPlugins()
                                    opt.onSuccess?.(undefined)
                                  } catch (e) {
                                    opt.onError?.(e as Error)
                                  }
                                }}
                              >
                                <Button icon={<UploadOutlined />} type="primary">{t('store.uploadPluginZip')}</Button>
                              </Upload>
                              ) : null}
                            </Space>
                          </div>
                          <DataTable chromeKey="store-plugins" rowKey="id" columns={pluginColumns} dataSource={plugins} pagination={false} />
                        </>
                      ),
                    },
                    {
                      key: 'drafts',
                      label: t('store.draftsTab', { n: drafts.filter((d) => d.status === 'pending').length }),
                      children: (
                        <>
                          <p style={{ color: '#666' }}>
                            {t('store.draftsIntro')}
                          </p>
                          <DataTable chromeKey="store-drafts" rowKey="id" columns={draftColumns} dataSource={drafts} pagination={false} />
                        </>
                      ),
                    },
                    {
                      key: 'templates',
                      label: t('store.templatesTab', { n: templates.length }),
                      children: <DataTable chromeKey="store-templates" rowKey="id" columns={templateColumns} dataSource={templates} pagination={false} />,
                    },
                  ]}
                />
              </>
            ),
          },
          {
            key: 'skills',
            label: t('store.skillsTab', { n: skills.length }),
            children: (
              <>
                <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
                  <span style={{ color: '#666' }}>
                    {t('store.skillsIntro')}
                  </span>
                  <Space>
                    <Button
                      icon={<DownloadOutlined />}
                      onClick={() =>
                        downloadTemplate('/harness/templates/agent-skill', 'release-agent-skill-template.zip')
                      }
                    >
                      {t('store.downloadSkillTemplate')}
                    </Button>
                    <Upload
                      accept=".zip,.json"
                      showUploadList={false}
                      customRequest={async (opt) => {
                        try {
                          const form = new FormData()
                          form.append('file', opt.file as File)
                          await postForm('/harness/components/upload', form)
                          message.success(t('store.skillShared'))
                          refreshHarness()
                          opt.onSuccess?.(undefined)
                        } catch (error) {
                          message.warning(t('store.skillUploadUnsupported'))
                          opt.onError?.(error as Error)
                        }
                      }}
                    >
                      <Button type="primary" icon={<UploadOutlined />}>{t('store.uploadSkill')}</Button>
                    </Upload>
                  </Space>
                </Space>
                {skills.length ? (
                  <DataTable chromeKey="store-skills" rowKey="key" columns={skillColumns} dataSource={skills} pagination={false} />
                ) : (
                  <Empty description={harnessFallback ? t('store.noSkillCatalogCompat') : t('store.noSkills')} />
                )}
              </>
            ),
          },
          {
            key: 'tools',
            label: t('store.toolsTab', { n: builtinTools.length + tools.length }),
            children: (
              <Tabs
                type="card"
                items={[
                  {
                    key: 'builtin-tools',
                    label: t('store.builtinToolsTab', { n: builtinTools.length }),
                    children: (
                      <>
                        <p style={{ color: '#666' }}>
                          {t('store.builtinToolsIntro')}
                        </p>
                        {builtinTools.length ? (
                          <DataTable
                            chromeKey="store-builtin-tools"
                            rowKey="name"
                            columns={builtinColumns}
                            dataSource={builtinTools}
                            pagination={false}
                          />
                        ) : (
                          <Empty description={builtinToolsQuery.isError ? t('store.toolsUnavailable') : t('store.noBuiltinTools')} />
                        )}
                      </>
                    ),
                  },
                  {
                    key: 'package-tools',
                    label: t('store.packageToolsTab', { n: tools.length }),
                    children: (
                      <>
                        <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
                          <span style={{ color: '#666' }}>
                            {t('store.packageToolsIntro')}
                          </span>
                          <Space>
                            <Button
                              icon={<DownloadOutlined />}
                              onClick={() =>
                                downloadTemplate('/harness/templates/agent-tool', 'release-agent-tool-template.zip')
                              }
                            >
                              {t('store.downloadToolTemplate')}
                            </Button>
                            {isAdmin ? (
                            <Upload
                              accept=".zip"
                              showUploadList={false}
                              customRequest={async (opt) => {
                                try {
                                  const form = new FormData()
                                  form.append('file', opt.file as File)
                                  await postForm('/harness/components/upload', form)
                                  message.success(t('store.toolUploaded'))
                                  refreshHarness()
                                  opt.onSuccess?.(undefined)
                                } catch (error) {
                                  message.warning(t('store.toolUploadUnsupported'))
                                  opt.onError?.(error as Error)
                                }
                              }}
                            >
                              <Button type="primary" icon={<UploadOutlined />}>{t('store.uploadTool')}</Button>
                            </Upload>
                            ) : null}
                          </Space>
                        </Space>
                        {tools.length ? (
                          <DataTable chromeKey="store-tools" rowKey="key" columns={toolColumns} dataSource={tools} pagination={false} scroll={{ x: 1100 }} />
                        ) : (
                          <Empty description={harnessFallback ? t('store.noToolCatalogCompat') : t('store.noPackageTools')} />
                        )}
                      </>
                    ),
                  },
                ]}
              />
            ),
          },
          {
            key: 'runtime',
            label: t('store.runtime'),
            children: (
              <>
                {runtimeQuery.isError ? (
                  <Alert
                    type="warning"
                    showIcon
                    message={t('store.runtimeUnavailable')}
                    description={t('store.runtimeUnavailableDesc')}
                  />
                ) : runtimeQuery.data ? (
                  <>
                    {runtimeQuery.data.isolation !== 'ready' && (
                      <Alert
                        type="warning"
                        showIcon
                        style={{ marginBottom: 16 }}
                        message={t('store.runnerUnavailable')}
                        description={runtimeQuery.data.isolation_error || t('store.runnerConfigHint')}
                      />
                    )}
                    <Descriptions bordered column={{ xs: 1, sm: 2, md: 3 }} style={{ marginBottom: 16 }}>
                      <Descriptions.Item label={t('common.status')}>
                        <Tag color={runtimeQuery.data.status === 'healthy' ? 'green' : 'orange'}>
                          {runtimeQuery.data.status === 'healthy' ? t('store.healthOk') : t('store.healthDegraded')}
                        </Tag>
                      </Descriptions.Item>
                      <Descriptions.Item label={t('store.isolation')}>
                        <Tag color={runtimeQuery.data.isolation === 'ready' ? 'green' : 'red'}>
                          {runtimeQuery.data.isolation === 'ready' ? t('store.ready') : t('store.unavailable')}
                        </Tag>
                      </Descriptions.Item>
                      <Descriptions.Item label={t('store.activeRuntimes')}>{runtimeQuery.data.active_runtimes ?? 0}</Descriptions.Item>
                      <Descriptions.Item label={t('store.loadedComponents')}>{runtimeQuery.data.loaded_components ?? components.length}</Descriptions.Item>
                      <Descriptions.Item label={t('store.byKind')} span={2}>
                        {Object.entries(runtimeQuery.data.by_kind || {}).map(([kind, count]) => (
                          <Tag key={kind}>{kind} · {count}</Tag>
                        ))}
                      </Descriptions.Item>
                    </Descriptions>
                  </>
                ) : null}
                <Card size="small" title={<Space><CloudServerOutlined />{t('store.lifecycleEvents')}</Space>}>
                  <DataTable
                    chromeKey="store-lifecycle"
                    size="small"
                    rowKey="id"
                    loading={eventsQuery.isLoading}
                    dataSource={eventsQuery.data || []}
                    columns={[
                      { title: t('store.component'), dataIndex: 'extension_key' },
                      { title: t('store.eventAction'), dataIndex: 'action', width: 100 },
                      { title: t('common.status'), width: 100, render: (_: unknown, row: HarnessLifecycleEvent) => <Tag color={row.success ? 'green' : 'red'}>{row.success ? t('status.success') : t('status.failed')}</Tag> },
                      { title: t('common.description'), dataIndex: 'message', ellipsis: true },
                      { title: t('store.actor'), dataIndex: 'actor_name', width: 100 },
                      { title: t('store.time'), dataIndex: 'created_at', width: 180 },
                    ]}
                    locale={{ emptyText: eventsQuery.isError ? t('store.eventsUnavailable') : t('store.noEvents') }}
                  />
                </Card>
              </>
            ),
          },
        ]}
      />
      <PluginDraftDrawer draftId={draftId} onClose={() => setDraftId(null)} />
      <Drawer
        title={resourceComponent ? t('store.resourceTitle', { name: componentName(resourceComponent) }) : t('store.skillResources')}
        open={!!resourceComponent}
        width={560}
        onClose={() => setResourceComponent(null)}
      >
        {resourceComponent && (
          <>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label={t('store.entrypoint')}>{resourceComponent.manifest.entrypoint || t('store.declarativeSkill')}</Descriptions.Item>
              <Descriptions.Item label={t('store.source')}>{resourceComponent.source_ref || resourceComponent.source || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('store.capabilities')}>{(resourceComponent.manifest.capabilities || []).join(t('store.listSep')) || '—'}</Descriptions.Item>
            </Descriptions>
            <Card size="small" title={<Space><ApiOutlined />{t('store.resourceDecl')}</Space>} style={{ marginTop: 16 }}>
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                {JSON.stringify(resourceComponent.manifest.metadata?.resources || resourceComponent.manifest.config_schema || {}, null, 2)}
              </pre>
            </Card>
          </>
        )}
      </Drawer>
    </Card>
  )
}
