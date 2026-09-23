import { useEffect, useState } from 'react'
import { Card, Tag, Tooltip, Button, Space, Modal, Form, Input, Popconfirm, Radio, Select, message, Alert, Typography } from 'antd'
import DataTable from '@/components/DataTable'
import {
  CloudServerOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  DisconnectOutlined,
  PlusOutlined,
  CopyOutlined,
  ReloadOutlined,
  DownloadOutlined,
  ConsoleSqlOutlined,
  LoadingOutlined,
  RedoOutlined,
} from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { get, del, post, postR, patch, getBlob } from '@/api/client'
import { copyText } from '@/utils/clipboard'
import type { BuildAgent } from '@/api/types'
import { envInstallArgs, envLabel, envOptions, envSelectOptions } from '@/env'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, useT } from '@/i18n'

const { Paragraph, Text } = Typography

// 平台地址：默认取当前浏览器访问的 host（用户用 IP/域名访问前端，就生成对应 IP/域名的命令）
// 端口固定 8080（后端）；若走 nginx 反代可改为 80/域名，由用户在弹窗手动覆盖
const DEFAULT_SERVER_URL = `${window.location.protocol}//${window.location.hostname}:8080`

const TAG_OPTIONS = [
  'linux', 'maven', 'docker', 'gradle',
  'windows', 'dotnet', 'iis',
  'macos', 'xcode', 'node',
]

type RunMode = 'script' | 'foreground' | 'background'

export default function Agents() {
  const t = useT()
  const queryClient = useQueryClient()
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  const [open, setOpen] = useState(false)
  /** 卸完后再装：弹窗预填这一台，名称锁定，命令会接到原来那条登记上 */
  const [reinstallTarget, setReinstallTarget] = useState<BuildAgent | null>(null)
  const [form] = Form.useForm()
  const [mode, setMode] = useState<RunMode>('script')
  // 命令区是在 Form 外面渲染的，表单改了不会自然带动它重算
  const [, bumpForm] = useState(0)

  // 只看构建机；生产部署节点在「节点管理」页，两者机制相同但职责完全不同
  const { data: agents = [] } = useQuery({
    queryKey: ['agents', 'builder'],
    queryFn: () => get<BuildAgent[]>('/agents?role=builder'),
    refetchInterval: 10000,
  })

  // 接入凭证：只在打开接入弹窗时取，避免列表页把它挂在内存里
  const { data: enroll } = useQuery({
    queryKey: ['agent-enroll-token'],
    queryFn: () => get<{ token: string }>('/agents/enroll-token'),
    enabled: open,
  })
  const enrollToken = enroll?.token || ''

  const rotateToken = async () => {
    Modal.confirm({
      title: t('agents.rotateTokenTitle'),
      content: t('agents.rotateTokenBody'),
      okText: t('agents.rotate'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await post('/agents/enroll-token/rotate')
        queryClient.invalidateQueries({ queryKey: ['agent-enroll-token'] })
        message.success(t('agents.rotated'))
      },
    })
  }

  const downloadJar = async () => {
    try {
      const blob = await getBlob('/agents/download')
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'deploy-agent.jar'
      a.click()
      URL.revokeObjectURL(url)
    } catch {
      message.error(t('agents.downloadFail'))
    }
  }

  const handleDelete = async (id: number) => {
    await del(`/agents/${id}`)
    queryClient.invalidateQueries({ queryKey: ['agents', 'builder'] })
  }

  const handleUninstall = async (r: BuildAgent) => {
    const res = await postR(`/agents/${r.id}/uninstall`)
    queryClient.invalidateQueries({ queryKey: ['agents', 'builder'] })
    message.success(res.message || t('agents.uninstallQueued'))
  }

  /**
   * 打开重新安装弹窗。名称和环境沿用原登记，到机器上跑命令后会清掉卸载状态重新上线。
   */
  const openReinstall = (r: BuildAgent) => {
    setReinstallTarget(r)
    setMode('script')
    setOpen(true)
  }

  const closeInstallModal = () => {
    setOpen(false)
    setReinstallTarget(null)
  }

  // destroyOnClose 后表单是新挂的，必须等弹窗打开再写入预填值
  useEffect(() => {
    if (!open || !reinstallTarget) {
      return
    }
    let tags: string[] = []
    try {
      const parsed = JSON.parse(reinstallTarget.tags || '[]')
      if (Array.isArray(parsed)) {
        tags = parsed.map((x) => String(x)).filter(Boolean)
      }
    } catch {
      tags = []
    }
    const os = reinstallTarget.os === 'windows' || reinstallTarget.os === 'macos' ? reinstallTarget.os : 'linux'
    form.setFieldsValue({
      name: reinstallTarget.name,
      os,
      env: reinstallTarget.env || 'prod',
      tags,
      workspace: '',
      serverUrl: DEFAULT_SERVER_URL,
    })
    bumpForm((n) => n + 1)
  }, [open, reinstallTarget, form])

  /** 版本落后时的按钮说明：卡住了要解释原因，升级中则说明会自己换。 */
  const upgradeHint = (r: BuildAgent) => {
    if (!r.outdated) {
      return t('agents.alreadyCurrent')
    }
    if (r.upgrade_error) {
      return t('agents.upgradeFail', { error: r.upgrade_error })
    }
    if (r.effective_status !== 'online') {
      return t('agents.offlineUpgrade')
    }
    return t('agents.upgradeHint')
  }

  /**
   * 催构建机再下一遍新 jar。
   * 换包失败后 Agent 会熔断，不再理会 should_upgrade；这个接口会带 force_upgrade，
   * 让它立刻重试。守护进程本身坏了的话，重试也换不上，需要到机器上重跑安装脚本。
   */
  const handleUpgrade = (r: BuildAgent) => {
    Modal.confirm({
      title: t('agents.nudgeUpgradeTitle', { name: r.name }),
      content: r.upgrade_error
        ? t('agents.nudgeUpgradeRetry', { error: r.upgrade_error })
        : t('agents.nudgeUpgradeBody', { from: r.agent_version || '', to: r.latest_version || '' }),
      okText: t('agents.retryUpgrade'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        await post(`/agents/${r.id}/upgrade`, {})
        message.success(t('agents.nudged'))
        queryClient.invalidateQueries({ queryKey: ['agents', 'builder'] })
      },
    })
  }

  // 构建机环境决定它能领哪种流水线的构建任务：必须和环境分组 type 全等。
  // 改动本身会进审计
  const changeEnv = (agent: BuildAgent, env: string) => {
    const label = envLabel(env)
    Modal.confirm({
      title: t('agents.markEnvTitle', { name: agent.name, env: label }),
      content: t('agents.markEnvBody', { env: label }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        await patch(`/agents/${agent.id}`, { env })
        queryClient.invalidateQueries({ queryKey: ['agents', 'builder'] })
        message.success(t('common.updated'))
      },
    })
  }

  // 命令实时从表单派生（不再需要「生成启动命令」按钮）
  const renderCommand = (): { cmd: string; note: string | null; os: string } | null => {
    const v = form.getFieldsValue()
    if (!v.name) return null
    const tags = (v.tags || []).join(',')
    // 平台地址：表单可编辑，默认取当前浏览器 host（虚拟机/其他机器要连到这台主机，不能是 localhost）
    const serverUrl = (v.serverUrl || DEFAULT_SERVER_URL).replace(/\/+$/, '')
    // 标签为空时省略 --tags，避免生成空参数
    // 接入凭证只有首次注册要带，之后 Agent 用落盘的凭据续期
    const enrollArg = enrollToken ? ` --enroll-token ${enrollToken}` : ''
    const workspace = (v.workspace || '').trim()
    const wsArg = workspace ? ` --workspace "${workspace}"` : ''
    // 环境只在这台机器首次接入平台时生效，之后以列表里的设置为准
    const envArgs = envInstallArgs(v.env)
    const base = `java -jar deploy-agent.jar --server ${serverUrl} --name ${v.name}${tags ? ` --tags ${tags}` : ''}${wsArg}${envArgs.cli}${enrollArg}`
    const os = v.os || 'linux'
    const reinstallNote = reinstallTarget ? t('agents.reinstallNote') + '\n' : ''

    if (mode === 'script') {
      // 脚本自己从平台下载 jar，不用先把 jar 和脚本传到机器上
      const tagArg = tags ? ` -Tags ${tags}` : ''
      const shTags = tags ? `TAGS=${tags} ` : ''
      // 显式带 Server：万一机器上留着旧脚本没被覆盖，也不会拿错平台地址
      if (os === 'windows') {
        return {
          cmd:
            `iwr "${serverUrl}/api/v1/agents/install-script?role=builder" -OutFile install-agent.ps1\n` +
            `.\\install-agent.ps1 -Server ${serverUrl} -Name ${v.name}${tagArg}${envArgs.ps}` +
            `${workspace ? ` -Workspace "${workspace}"` : ''}` +
            `${enrollToken ? ` -EnrollToken ${enrollToken}` : ''}`,
          note: reinstallNote + t('agents.scriptNoteWin'),
          os,
        }
      }
      return {
        cmd:
          `curl -fsSL "${serverUrl}/api/v1/agents/install-script?role=builder-linux" -o install-agent.sh\n` +
          `SERVER=${serverUrl} ${shTags}${envArgs.shPrefix}NAME=${v.name}` +
          `${workspace ? ` WORKSPACE=${workspace}` : ''}` +
          `${enrollToken ? ` ENROLL_TOKEN=${enrollToken}` : ''} bash install-agent.sh`,
        note: reinstallNote + t('agents.scriptNoteUnix'),
        os,
      }
    }

    if (mode === 'foreground') {
      return { cmd: base, note: reinstallNote + t('agents.foregroundNote'), os }
    }

    if (os === 'windows') {
      return {
        cmd: `start /B ${base} > agent.log 2>&1`,
        note: reinstallNote + t('agents.bgNoteWin'),
        os,
      }
    }
    // Linux / macOS（nohup 通用）
    return {
      cmd: `nohup ${base} > agent.log 2>&1 &\necho "${t('agents.bgEcho')}"`,
      note: reinstallNote + t('agents.bgNoteUnix'),
      os,
    }
  }

  const copyCommand = async () => {
    const result = renderCommand()
    if (!result) {
      message.warning(t('agents.needName'))
      return
    }
    await copyText(result.cmd, t('agents.cmdCopied'))
  }

  const columns = [
    {
      title: t('common.name'),
      dataIndex: 'name',
      render: (v: string) => <Space><CloudServerOutlined />{v}</Space>,
    },
    { title: t('common.host'), dataIndex: 'host' },
    {
      title: t('common.os'),
      dataIndex: 'os',
      render: (v: string) =>
        v === 'linux' ? <Tag color="blue">🐧 Linux</Tag>
        : v === 'windows' ? <Tag color="purple">🪟 Windows</Tag>
        : <Tag color="orange">🍎 macOS</Tag>,
    },
    {
      title: t('common.environment'),
      dataIndex: 'env',
      width: 110,
      render: (v: string, r: BuildAgent) => (
        <Tooltip title={t('agents.onlyBuildEnv', { env: envLabel(v) })}>
          <Select
            size="small"
            value={v || undefined}
            placeholder={t('common.unlabeled')}
            style={{ width: 96 }}
            onChange={(next) => changeEnv(r, next)}
            options={envSelectOptions(agents.map((a) => a.env))}
            disabled={!isAdmin}
          />
        </Tooltip>
      ),
    },
    {
      title: t('common.tags'),
      dataIndex: 'tags',
      render: (v: string) => {
        try {
          const tags = JSON.parse(v)
          return tags.map((tag: string) => <Tag key={tag}>{tag}</Tag>)
        } catch {
          return v
        }
      },
    },
    {
      title: t('common.status'),
      dataIndex: 'effective_status',
      render: (v: string, r: BuildAgent) => {
        // 后端 effective_status 已根据 last_heartbeat 推算：online 但超时 → offline
        const tag =
          v === 'uninstalled' ? <Tag>{t('agents.uninstalled')}</Tag>
          : v === 'uninstalling' ? <Tag color="warning">{t('agents.uninstalling')}</Tag>
          : v === 'online' ? <Tag color="success">{t('agents.online')}</Tag>
          : <Tag>{t('agents.offline')}</Tag>
        const hb = r.last_heartbeat
          ? formatDateTime(r.last_heartbeat)
          : t('agents.neverHeartbeat')
        return (
          <Space direction="vertical" size={0}>
            {tag}
            <span style={{ fontSize: 11, color: '#999' }}>{t('agents.heartbeat', { time: hb })}</span>
          </Space>
        )
      },
    },
    {
      title: t('common.version'),
      dataIndex: 'agent_version',
      render: (v: string, r: BuildAgent) => {
        if (!v) {
          return <Tag color="warning">{t('agents.unknownOld')}</Tag>
        }
        return (
          <Space direction="vertical" size={0}>
            <Text code style={{ fontSize: 12 }}>
              {v}
            </Text>
            {r.outdated && r.upgrade_error && (
              <Tooltip title={t('agents.upgradeStuckTip', { error: r.upgrade_error || '' })}>
                <Tag color="error">{t('agents.upgradeStuck', { version: r.latest_version || '' })}</Tag>
              </Tooltip>
            )}
            {r.outdated && !r.upgrade_error && (
              <Tooltip title={upgradeHint(r)}>
                <Tag color={r.effective_status === 'online' ? 'processing' : 'default'}>
                  {r.effective_status === 'online' ? t('agents.upgradingAuto') : t('agents.pendingUpgrade')} → {r.latest_version}
                </Tag>
              </Tooltip>
            )}
            {r.upgrade_error && (
              <Text type="danger" style={{ fontSize: 11, maxWidth: 280 }} ellipsis>
                {r.upgrade_error}
              </Text>
            )}
          </Space>
        )
      },
    },
    {
      title: t('common.action'),
      width: 280,
      render: (_: unknown, r: BuildAgent) =>
        isAdmin ? (
        <Space wrap>
          {r.uninstalled ? null : (
          <Tooltip title={upgradeHint(r)}>
            <span>
              <Button
                size="small"
                icon={r.upgrading ? <LoadingOutlined /> : <CloudUploadOutlined />}
                disabled={!r.outdated || r.effective_status !== 'online'}
                onClick={() => handleUpgrade(r)}
              >
                {r.upgrading ? t('agents.upgrading') : r.upgrade_stalled ? t('agents.retryUpgrade') : t('common.upgrade')}
              </Button>
            </span>
          </Tooltip>
          )}
          {r.uninstalled ? null : (
            <Popconfirm
              title={t('agents.uninstallTitle', { name: r.name })}
              description={t('agents.uninstallBody')}
              okText={t('agents.uninstall')}
              okButtonProps={{ danger: true }}
              cancelText={t('common.cancel')}
              onConfirm={() => handleUninstall(r)}
            >
              <Button size="small" danger icon={<DisconnectOutlined />} disabled={!!r.uninstall_requested}>
                {r.uninstall_requested ? t('agents.uninstalling') : t('agents.uninstall')}
              </Button>
            </Popconfirm>
          )}
          {r.uninstalled ? (
            <Button size="small" icon={<RedoOutlined />} onClick={() => openReinstall(r)}>
              {t('agents.reinstall')}
            </Button>
          ) : null}
          <Tooltip title={r.can_delete ? t('agents.uninstalledHint') : t('agents.deleteNeedUninstall')}>
            <span>
              <Popconfirm
                title={t('agents.deleteTitle', { name: r.name })}
                description={t('agents.deleteDesc')}
                okText={t('common.delete')}
                okButtonProps={{ danger: true }}
                cancelText={t('common.cancel')}
                onConfirm={() => handleDelete(r.id)}
              >
                <Button size="small" danger icon={<DeleteOutlined />} disabled={!r.can_delete}>
                  {t('common.delete')}
                </Button>
              </Popconfirm>
            </span>
          </Tooltip>
        </Space>
        ) : null,
    },
  ]

  return (
    <div>
      <Card
        title={t('agents.title')}
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setReinstallTarget(null)
              form.resetFields()
              setOpen(true)
            }}
          >
            {t('agents.addAgent')}
          </Button>
        }
      >
        <DataTable chromeKey="agents" rowKey="id" columns={columns} dataSource={agents} pagination={false} />
      </Card>

      {/* 新增构建机弹窗 */}
      <Modal
        title={
          reinstallTarget
            ? t('agents.reinstallTitle', { name: reinstallTarget.name })
            : t('agents.addAgent')
        }
        open={open}
        onCancel={closeInstallModal}
        footer={null}
        width={720}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          message={t('agents.jarAlertTitle')}
          description={
            <>
              {t('agents.sameJarLead')}
              <b>{t('agents.sameJarBold')}</b>
              {t('agents.sameJarMid')}
              <code>--name</code>/<code>--tags</code>/<code>--server</code>
              {t('agents.sameJarTail')}
            </>
          }
          style={{ marginBottom: 16 }}
        />

        <Alert
          type="warning"
          showIcon
          message={t('agents.tokenAlertTitle')}
          description={
            <>
              {t('agents.tokenAlertLead')}
              <b>{t('agents.tokenAlertBold')}</b>
              {t('agents.tokenAlertMid')}
              <code>~/.release-agent/enrolled/</code>
              {t('agents.tokenAlertTail')}
            </>
          }
          style={{ marginBottom: 16 }}
        />

        <Form
          form={form}
          layout="vertical"
          initialValues={{ os: 'linux', name: 'my-agent', env: 'prod' }}
          onValuesChange={() => bumpForm((n) => n + 1)}
        >
          <Form.Item label={t('agents.nameLabel')} name="name" rules={[{ required: true }]}>
            <Input placeholder={t('agents.namePh')} disabled={!!reinstallTarget} />
          </Form.Item>

          <Form.Item label={t('agents.osLabel')} name="os" rules={[{ required: true }]}>
            <Radio.Group disabled={!!reinstallTarget}>
              <Radio value="linux">🐧 Linux</Radio>
              <Radio value="windows">🪟 Windows</Radio>
              <Radio value="macos">🍎 macOS</Radio>
            </Radio.Group>
          </Form.Item>

          <Form.Item
            label={t('common.environment')}
            name="env"
            extra={t('agents.envExtra')}
          >
            <Select options={envOptions()} disabled={!!reinstallTarget} />
          </Form.Item>

          <Form.Item label={t('agents.tagsLabel')} name="tags">
            <Select
              mode="multiple"
              placeholder={t('agents.tagsPh')}
              options={TAG_OPTIONS.map((tag) => ({ value: tag, label: tag }))}
            />
          </Form.Item>

          <Form.Item
            label={t('agents.workspaceLabel')}
            name="workspace"
            extra={t('agents.workspaceExtra')}
          >
            <Input
              placeholder={
                form.getFieldValue('os') === 'windows'
                  ? t('agents.wsPhWin')
                  : t('agents.wsPhUnix')
              }
            />
          </Form.Item>
        </Form>

        {/* 命令区：根据操作系统 + 运行模式实时派生 */}
        {(() => {
          const result = renderCommand()
          if (!result) {
            return (
              <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                {t('agents.needNameForCmd')}
              </Text>
            )
          }
          return (
            <>
              <Radio.Group
                value={mode}
                onChange={(e) => setMode(e.target.value)}
                style={{ marginBottom: 8 }}
                size="small"
              >
                <Radio.Button value="script">{t('agents.installScript')}</Radio.Button>
                <Radio.Button value="background">{t('agents.manualBg')}</Radio.Button>
                <Radio.Button value="foreground">{t('agents.manualFg')}</Radio.Button>
              </Radio.Group>
              {mode === 'background' && (
                <Paragraph style={{ marginBottom: 4 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {result.os === 'windows'
                      ? t('agents.bgHintWin')
                      : t('agents.bgHintUnix')}
                  </Text>
                </Paragraph>
              )}
              <Paragraph style={{ marginBottom: 4 }}>
                <pre
                  style={{
                    background: '#1e1e1e',
                    color: '#d4d4d4',
                    padding: 12,
                    borderRadius: 6,
                    fontSize: 12,
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {result.cmd}
                </pre>
              </Paragraph>
              {result.note && (
                <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                  {result.note}
                </Text>
              )}
            </>
          )
        })()}

        <Space style={{ marginTop: 12 }} wrap>
          <Button icon={<CopyOutlined />} onClick={copyCommand}>{t('agents.copyCmd')}</Button>
          <Button icon={<DownloadOutlined />} onClick={downloadJar}>
            {t('agents.downloadJar')}
          </Button>
          {isAdmin ? (
            <Button icon={<ReloadOutlined />} onClick={rotateToken} danger>
              {t('agents.rotateTokenTitle')}
            </Button>
          ) : null}
          <Button
            icon={<ReloadOutlined />}
            onClick={() => queryClient.invalidateQueries({ queryKey: ['agents', 'builder'] })}
          >
            {t('agents.refreshList')}
          </Button>
        </Space>

        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
          <b>{t('agents.steps')}</b>
          {t('agents.stepsBody')}
          <br />
          {t('agents.workspaceChange')}
        </Text>
      </Modal>
    </div>
  )
}
