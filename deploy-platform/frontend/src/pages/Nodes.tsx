import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Divider,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd'
import {
  ApartmentOutlined,
  CloudUploadOutlined,
  CopyOutlined,
  DeleteOutlined,
  DesktopOutlined,
  DownloadOutlined,
  DisconnectOutlined,
  EditOutlined,
  LoadingOutlined,
  PlusOutlined,
  RedoOutlined,
  ReloadOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { del, get, getBlob, patch, post, postForm, postR } from '@/api/client'
import DataTable from '@/components/DataTable'
import { copyText } from '@/utils/clipboard'
import type { BuildAgent, NodeGroup } from '@/api/types'
import {
  envInstallArgs,
  envLabel,
  envOptions,
  envSelectOptions,
  skipNodePushApproval,
} from '@/env'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime, useT } from '@/i18n'

const { Paragraph, Text } = Typography

const DEFAULT_SERVER_URL = `${window.location.protocol}//${window.location.hostname}:8080`

/** 节点名称会进 sudo env / systemd。拦空格和分号，中文名称放行。 */
const NODE_NAME_UNSAFE = /[\s"'`$;&|<>\\/]/

/** POSIX 单引号：把值原样交给 env，避免 NAME=web RUN_USER=root 这种拆分。 */
function shSingleQuote(value: string): string {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

/** PowerShell 单引号：内部单引号写成两个。 */
function psSingleQuote(value: string): string {
  return `'${String(value).replace(/'/g, "''")}'`
}

type RunMode = 'script' | 'manual'

/**
 * 节点管理：装了节点 Agent 的生产服务器。
 *
 * 节点不是构建机——它不编译、不跑脚本、不拉代码，只执行发布动作（发文件、启停 IIS），
 * 而且所有写操作都被限制在允许目录内。目录安装时声明，之后可在列表里改，心跳下发给 Agent。
 */
export default function Nodes() {
  const t = useT()
  const queryClient = useQueryClient()
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  const [open, setOpen] = useState(false)
  /** 弹窗正在改的已有节点；空表示新增。重跑安装必须用原来的名称，否则会登记成另一台。 */
  const [editingNode, setEditingNode] = useState<BuildAgent | null>(null)
  const [savingNode, setSavingNode] = useState(false)
  const [form] = Form.useForm()
  const [mode, setMode] = useState<RunMode>('script')
  // 两套安装方式差别不小：Windows 走 PowerShell + IIS，Linux 走 systemd + sudoers 白名单
  const [os, setOs] = useState<'windows' | 'linux'>('windows')
  // 表单实时派生启动命令，改任意一项都要重算
  const [, forceRender] = useState(0)

  // 机器一多，列表就得能筛能翻，否则几百行全渲染出来页面直接卡住
  const [keyword, setKeyword] = useState('')
  const [groupFilter, setGroupFilter] = useState<number>()
  const [envFilter, setEnvFilter] = useState<string>()
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [groupPanelOpen, setGroupPanelOpen] = useState(false)
  const [assignOpen, setAssignOpen] = useState(false)
  const [assignGroupId, setAssignGroupId] = useState<number>()
  /** 加入弹窗里待创建的分组名；创建后只选中，不自动把机器加进去。 */
  const [newGroupName, setNewGroupName] = useState('')
  const [creatingGroup, setCreatingGroup] = useState(false)
  const [editingGroup, setEditingGroup] = useState<NodeGroup | null>(null)
  const [groupForm] = Form.useForm()
  const [pathsNode, setPathsNode] = useState<BuildAgent | null>(null)
  const [pathsText, setPathsText] = useState('')
  const [savingPaths, setSavingPaths] = useState(false)

  const { data: nodes = [] } = useQuery({
    queryKey: ['agents', 'node'],
    queryFn: () => get<BuildAgent[]>('/agents?role=node'),
    refetchInterval: 10000,
  })

  const { data: groups = [] } = useQuery({
    queryKey: ['node-groups'],
    queryFn: () => get<NodeGroup[]>('/node-groups'),
  })

  const { data: enroll } = useQuery({
    queryKey: ['agent-enroll-token'],
    queryFn: () => get<{ token: string }>('/agents/enroll-token'),
    enabled: open,
  })
  const enrollToken = enroll?.token || ''

  const { data: jdkLinux, refetch: refetchJdkLinux } = useQuery({
    queryKey: ['agents-jdk-linux'],
    queryFn: () =>
      get<{ ready: boolean; filename: string; size_bytes: number; persistent: boolean }>(
        '/agents/jdk-linux/status',
      ),
  })

  /** 上传 Linux 节点 JDK 包。file: 用户选的 tar.gz。 */
  const uploadJdkLinux = async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    await postForm('/agents/jdk-linux', form, 600000)
    message.success(t('nodes.jdkSaved'))
    refetchJdkLinux()
    return false
  }

  const jdkUploadButton = (
    <Upload
      accept=".tar.gz"
      showUploadList={false}
      beforeUpload={(file) => {
        uploadJdkLinux(file as unknown as File)
        return false
      }}
    >
      <Button icon={<UploadOutlined />}>{t('nodes.uploadJdk')}</Button>
    </Upload>
  )

  useEffect(() => {
    if (!open || !editingNode) return
    form.setFieldsValue({
      name: editingNode.name,
      serverUrl: DEFAULT_SERVER_URL,
      env: editingNode.env || 'prod',
      allowPaths: (editingNode.allow_paths || []).join('\n'),
      allowServices: (editingNode.allow_services || []).join('\n'),
      allowIis: '',
    })
    forceRender((n) => n + 1)
  }, [open, editingNode, form])

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['agents', 'node'] })
  const refreshGroups = () => {
    queryClient.invalidateQueries({ queryKey: ['node-groups'] })
    refresh()
  }

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return nodes.filter((n) => {
      if (groupFilter && !(n.groups || []).some((g) => g.id === groupFilter)) return false
      // 老节点可能没有 env 值，按后端默认的 prod 算，别让它们在筛选里凭空消失
      if (envFilter && (n.env || 'prod') !== envFilter) return false
      if (!kw) return true
      return (
        (n.name || '').toLowerCase().includes(kw) ||
        (n.host || '').toLowerCase().includes(kw) ||
        (n.groups || []).some((g) => g.name.toLowerCase().includes(kw))
      )
    })
  }, [nodes, keyword, groupFilter, envFilter])

  const envCount = useMemo(() => {
    const m: Record<string, number> = {}
    for (const n of nodes) {
      const e = n.env || 'prod'
      m[e] = (m[e] || 0) + 1
    }
    return m
  }, [nodes])

  // 批量进出组：几百台机器时这是唯一还能用的操作方式
  const applyAssign = async (join: boolean) => {
    const target = groups.find((g) => g.id === assignGroupId)
    if (!target) {
      message.warning(t('nodes.pickGroupFirst'))
      return
    }
    const current = new Set(target.agent_ids)
    selectedIds.forEach((id) => (join ? current.add(id) : current.delete(id)))
    await patch(`/node-groups/${target.id}`, { agent_ids: [...current] })
    setAssignOpen(false)
    setSelectedIds([])
    refreshGroups()
    message.success(join ? t('nodes.joinedGroup', { name: target.name }) : t('nodes.leftGroup', { name: target.name }))
  }

  /**
   * 在加入弹窗里新建空分组并选中。
   * 不自动入组：新建组还没有下发授权，误触加入会把生产节点先绑上去，以后有人给这个组授权范围已经铺开了。
   */
  const createAssignGroup = async () => {
    const name = newGroupName.trim()
    if (!name) {
      message.warning(t('nodes.needGroupName'))
      return
    }
    setCreatingGroup(true)
    try {
      const created = await post<NodeGroup>('/node-groups', { name })
      setNewGroupName('')
      queryClient.setQueryData<NodeGroup[]>(['node-groups'], (old) => {
        const list = old || []
        if (list.some((g) => g.id === created.id)) return list
        return [...list, created]
      })
      setAssignGroupId(created.id)
      refreshGroups()
      message.success(t('nodes.groupCreatedJoin', { name: created.name }))
    } finally {
      setCreatingGroup(false)
    }
  }

  const saveGroup = async () => {
    const v = await groupForm.validateFields()
    if (editingGroup) {
      await patch(`/node-groups/${editingGroup.id}`, { name: v.name, description: v.description })
      message.success(t('nodes.saved'))
    } else {
      await post('/node-groups', { name: v.name, description: v.description })
      message.success(t('common.created'))
    }
    setEditingGroup(null)
    groupForm.resetFields()
    refreshGroups()
  }

  const removeGroup = async (g: NodeGroup) => {
    const res = await del<{ revoked: number }>(`/node-groups/${g.id}`)
    refreshGroups()
    message.success(
      res?.revoked ? t('nodes.deletedWithRevokes', { n: res.revoked }) : t('nodes.deleted'),
    )
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
      message.error(t('nodes.downloadFailed'))
    }
  }

  const rotateToken = () => {
    Modal.confirm({
      title: t('nodes.rotateTitle'),
      content: t('nodes.rotateBody'),
      okText: t('nodes.rotateOk'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await post('/agents/enroll-token/rotate')
        queryClient.invalidateQueries({ queryKey: ['agent-enroll-token'] })
        message.success(t('nodes.rotated'))
      },
    })
  }

  /** 把多行/逗号分隔的目录或服务名拆成数组。 */
  const splitLines = (raw: unknown) =>
    String(raw || '')
      .split(/[\n;,]+/)
      .map((s) => s.trim())
      .filter(Boolean)

  /** 打开新增弹窗：清空表单，按 Windows 起步，需要时再切 Linux。 */
  const openCreate = () => {
    setEditingNode(null)
    setOs('windows')
    setMode('script')
    form.resetFields()
    form.setFieldsValue({ serverUrl: DEFAULT_SERVER_URL, env: 'prod' })
    setOpen(true)
  }

  /**
   * 打开已有节点的修改弹窗。
   * 名称不能改：它是这台机器在平台上的身份，systemd 启动参数和凭据文件都按这个名字。
   * 目录和环境可以立刻保存，随心跳生效；服务名单写在机器上的 sudoers 里，必须重跑安装脚本。
   */
  const openEdit = (node: BuildAgent) => {
    setEditingNode(node)
    setOs(node.os === 'linux' ? 'linux' : 'windows')
    setMode('script')
    form.setFieldsValue({
      name: node.name,
      serverUrl: DEFAULT_SERVER_URL,
      env: node.env || 'prod',
      allowPaths: (node.allow_paths || []).join('\n'),
      allowServices: (node.allow_services || []).join('\n'),
      allowIis: '',
    })
    setOpen(true)
  }

  const closeNodeModal = () => {
    setOpen(false)
    setEditingNode(null)
  }

  /** 把弹窗里改过的环境和允许目录写回平台。服务名单只出现在安装命令里，保存改不了 sudoers。 */
  const saveEditingNode = async () => {
    if (!editingNode) return
    const v = await form.validateFields()
    const paths = splitLines(v.allowPaths)
    setSavingNode(true)
    try {
      await patch(`/agents/${editingNode.id}`, {
        env: v.env,
        allow_paths: paths,
      })
      message.success(t('nodes.savedPathsHeartbeat'))
      refresh()
    } finally {
      setSavingNode(false)
    }
  }

  const handleDelete = (node: BuildAgent) => {
    if (!node.can_delete) {
      message.warning(t('nodes.deleteNeedUninstall'))
      return
    }
    Modal.confirm({
      title: t('nodes.removeTitle', { name: node.name }),
      content: t('nodes.removeBody'),
      okText: t('nodes.remove'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await del(`/agents/${node.id}`)
        refresh()
        message.success(t('nodes.removed'))
      },
    })
  }

  const handleUninstall = async (node: BuildAgent) => {
    const res = await postR(`/agents/${node.id}/uninstall`)
    refresh()
    message.success(res.message || t('nodes.uninstallQueued'))
  }

  const handleConfirmUninstalled = (node: BuildAgent) => {
    Modal.confirm({
      title: t('nodes.confirmUninstalledTitle', { name: node.name }),
      content: t('nodes.confirmUninstalledBody'),
      okText: t('nodes.confirmUninstalled'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await post(`/agents/${node.id}/uninstall/confirm`)
        refresh()
        message.success(t('nodes.uninstalledHint'))
      },
    })
  }

  const renderCommand = (): { cmd: string; note: string } | null => {
    const v = form.getFieldsValue()
    if (!v.name || !v.allowPaths) return null
    const serverUrl = (v.serverUrl || DEFAULT_SERVER_URL).replace(/\/+$/, '')
    const paths = splitLines(v.allowPaths)
    const services = splitLines(v.allowServices)
    const iis = splitLines(v.allowIis)
    const envArgs = envInstallArgs(v.env)
    const envNote = skipNodePushApproval(envArgs.value)
      ? '\n' + t('nodes.noteSkipApproval', { env: envLabel(envArgs.value) })
      : '\n' + t('nodes.noteNeedApproval', { env: envLabel(envArgs.value) })
    const rerunNote = !editingNode
      ? ''
      : '\n' + (editingNode.uninstalled ? t('nodes.reinstallNote') : t('nodes.rerunNote'))
    // 只在用户写明时带进安装命令；空着让脚本推导 release-backup，不能当成整盘可写
    const backupRoot = String(v.backupRoot || '').trim()

    if (os === 'linux') {
      // Linux 节点必须 root 装：要写 systemd unit 和 sudoers 白名单。
      // Agent 本身跑在普通用户下，root 只是安装期需要
      const svcEnv = services.length ? ` ALLOW_SERVICES=${shSingleQuote(services.join(','))}` : ''
      const tokenEnv = enrollToken ? ` ENROLL_TOKEN=${shSingleQuote(enrollToken)}` : ''
      const backupEnv = backupRoot ? ` BACKUP_ROOT=${shSingleQuote(backupRoot)}` : ''
      if (mode === 'script') {
        return {
          cmd:
            `curl -fsSL "${serverUrl}/api/v1/agents/install-script?role=node-linux" -o install-node.sh\n` +
            // 走 sudo env 而不是 sudo VAR=x：后者要看 sudoers 的 env_reset / SETENV 脸色，
            // 有的机器上变量根本传不进去。丢了 SERVER 或 ALLOW_PATHS 脚本会当场报错，
            // 但丢了 ALLOW_SERVICES 是静默的——装完看着成功，到发布时才发现服务停不动
            `sudo env SERVER=${shSingleQuote(serverUrl)} NAME=${shSingleQuote(v.name)} ALLOW_PATHS=${shSingleQuote(paths.join(','))}` +
            ` INSTALL_DIR=$(pwd)` +
            ` ENV=${shSingleQuote(envArgs.value)}${svcEnv}${backupEnv}${tokenEnv} bash install-node.sh`,
          note: t('nodes.linuxScriptNote') + envNote + rerunNote,
        }
      }
      const enrollArg = enrollToken ? ` --enroll-token ${shSingleQuote(enrollToken)}` : ''
      const svcArg = services.length ? ` --allow-services ${shSingleQuote(services.join(';'))}` : ''
      const backupArg = backupRoot ? ` --backup-root ${shSingleQuote(backupRoot)}` : ''
      return {
        cmd:
          `java -jar deploy-agent.jar --server ${shSingleQuote(serverUrl)} --name ${shSingleQuote(v.name)}` +
          ` --role node${envArgs.cli} --allow-paths ${shSingleQuote(paths.join(';'))}${svcArg}${backupArg}${enrollArg}`,
        note: t('nodes.linuxManualNote') + envNote + rerunNote,
      }
    }

    if (mode === 'script') {
      // 脚本自己从平台下载 jar，平台地址已烤进脚本，不用人肉传文件
      const tokenArg = enrollToken ? ` -EnrollToken ${psSingleQuote(enrollToken)}` : ''
      const iisArg = iis.length ? ` -AllowIis ${psSingleQuote(iis.join(','))}` : ''
      const backupArg = backupRoot ? ` -BackupRoot ${psSingleQuote(backupRoot)}` : ''
      return {
        cmd:
          // 显式带 -Server：万一机器上留着旧脚本没被覆盖，也不会拿错平台地址
          `iwr "${serverUrl}/api/v1/agents/install-script?role=node" -OutFile install-node.ps1\n` +
          `.\\install-node.ps1 -Server ${psSingleQuote(serverUrl)} -Name ${psSingleQuote(v.name)} -AllowPaths ${psSingleQuote(paths.join(','))}` +
          `${iisArg}${backupArg}${envArgs.ps}${tokenArg}`,
        note: t('nodes.windowsScriptNote') + envNote + rerunNote,
      }
    }

    // 多个目录用分号分隔，和 Windows 的 PATH 习惯一致。手动方式走 cmd，不能用 PowerShell 单引号。
    const enrollArg = enrollToken ? ` --enroll-token "${String(enrollToken).replace(/"/g, '')}"` : ''
    const iisArg = iis.length ? ` --allow-iis "${iis.join(';')}"` : ''
    const backupArg = backupRoot ? ` --backup-root "${backupRoot.replace(/"/g, '')}"` : ''
    const base =
      `java -jar deploy-agent.jar --server ${serverUrl} --name ${v.name}` +
      ` --role node${envArgs.cli} --allow-paths "${paths.join(';')}"${iisArg}${backupArg}${enrollArg}`
    return {
      cmd: base,
      note: t('nodes.windowsManualNote') + envNote + rerunNote,
    }
  }

  const copyCommand = async () => {
    const result = renderCommand()
    if (!result) {
      message.warning(t('nodes.needNameAndPaths'))
      return
    }
    await copyText(
      result.cmd,
      editingNode?.uninstalled
        ? t('nodes.copiedInstall')
        : editingNode
          ? t('nodes.copiedRerun')
          : t('nodes.copiedInstall'),
    )
  }

  const upgradeHint = (r: BuildAgent) => {
    if (!r.outdated) {
      return t('nodes.alreadyCurrent')
    }
    if (r.upgrade_error) {
      return t('nodes.autoUpgradeFailed', { error: r.upgrade_error })
    }
    if (r.effective_status !== 'online') {
      return t('nodes.offlineNoCmd')
    }
    return t('nodes.autoUpgradeHint')
  }

  // 版本落后的节点本来就会自己升，这里是「自动升级卡住了想再推一把」的手动重试
  const handleUpgrade = (r: BuildAgent) => {
    Modal.confirm({
      title: t('nodes.nudgeUpgradeTitle', { name: r.name }),
      content: r.upgrade_error
        ? t('nodes.upgradeRetryBody', { error: r.upgrade_error })
        : t('nodes.upgradeBody', { from: r.agent_version || '', to: r.latest_version || '' }),
      okText: t('nodes.retryUpgrade'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        await post(`/agents/${r.id}/upgrade`, {})
        message.success(t('nodes.upgradeNudged'))
        refresh()
      },
    })
  }

  // 环境决定往这台机器传文件要不要审批，改动本身也会进审计
  const changeEnv = (node: BuildAgent, env: string) => {
    const skip = skipNodePushApproval(env)
    Modal.confirm({
      title: t('nodes.markEnvTitle', { name: node.name, env: envLabel(env) }),
      content: skip
        ? t('nodes.markEnvSkip', { env: envLabel(env) })
        : t('nodes.markEnvApprove', { env: envLabel(env) }),
      okText: t('common.confirm'),
      okButtonProps: { danger: skip },
      cancelText: t('common.cancel'),
      onOk: async () => {
        await patch(`/agents/${node.id}`, { env })
        refresh()
        message.success(t('common.updated'))
      },
    })
  }

  const openAllowPaths = (node: BuildAgent) => {
    setPathsNode(node)
    setPathsText((node.allow_paths || []).join('\n'))
  }

  const saveAllowPaths = async () => {
    if (!pathsNode) return
    const paths = pathsText
      .split(/[\n;,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    setSavingPaths(true)
    try {
      await patch(`/agents/${pathsNode.id}`, { allow_paths: paths })
      setPathsNode(null)
      refresh()
      message.success(t('nodes.savedPathsOnline'))
    } finally {
      setSavingPaths(false)
    }
  }

  const copyEnrollToken = async () => {
    if (!enrollToken) {
      message.warning(t('nodes.noEnrollToken'))
      return
    }
    await copyText(enrollToken, t('nodes.enrollCopied'))
  }

  const columns = [
    {
      title: t('nodes.colName'),
      dataIndex: 'name',
      render: (v: string) => (
        <Space>
          <DesktopOutlined />
          {v}
        </Space>
      ),
    },
    { title: t('nodes.colHost'), dataIndex: 'host' },
    {
      title: t('nodes.colGroups'),
      dataIndex: 'groups',
      width: 200,
      render: (v: { id: number; name: string }[] = []) =>
        v.length === 0 ? (
          <Tooltip title={t('nodes.ungroupedHint')}>
            <Tag>{t('project.ungrouped')}</Tag>
          </Tooltip>
        ) : (
          <Space size={4} wrap>
            {v.map((g) => (
              <Tag key={g.id} color="geekblue">
                {g.name}
              </Tag>
            ))}
          </Space>
        ),
    },
    {
      title: t('common.os'),
      dataIndex: 'os',
      render: (v: string) =>
        v === 'windows' ? <Tag color="purple">🪟 Windows</Tag> : <Tag color="blue">{v}</Tag>,
    },
    {
      title: t('common.environment'),
      dataIndex: 'env',
      width: 130,
      render: (v: string, r: BuildAgent) => (
        <Tooltip title={skipNodePushApproval(v) ? t('nodes.skipApprovalTip') : t('nodes.needApprovalTip')}>
          <Select
            size="small"
            value={v || undefined}
            placeholder={t('common.unlabeled')}
            style={{ width: 96 }}
            onChange={(next) => changeEnv(r, next)}
            options={envSelectOptions(nodes.map((n) => n.env))}
            disabled={!isAdmin}
          />
        </Tooltip>
      ),
    },
    {
      title: t('nodes.allowPaths'),
      dataIndex: 'allow_paths',
      render: (v: string[] = [], r: BuildAgent) => (
        <Space align="start">
          {v.length === 0 ? (
            <Tag color="error">{t('nodes.noPaths')}</Tag>
          ) : (
            <Space direction="vertical" size={0}>
              {v.map((p) => (
                <Text key={p} code style={{ fontSize: 12 }}>
                  {p}
                </Text>
              ))}
            </Space>
          )}
          <Tooltip title={t('nodes.pathsHeartbeatTip')}>
            {isAdmin ? (
              <Button size="small" type="link" icon={<EditOutlined />} onClick={() => openAllowPaths(r)}>
                {t('nodes.editPaths')}
              </Button>
            ) : null}
          </Tooltip>
        </Space>
      ),
    },
    {
      title: t('nodes.allowServices'),
      dataIndex: 'allow_services',
      render: (v: string[] = [], r: BuildAgent) => {
        if (r.os === 'windows') {
          return (
            <Tooltip title={t('nodes.iisByPathTip')}>
              <Text type="secondary">{t('nodes.iisByPath')}</Text>
            </Tooltip>
          )
        }
        return v.length === 0 ? (
          <Tooltip title={t('nodes.noServicesTip')}>
            <Tag>{t('nodes.notConfigured')}</Tag>
          </Tooltip>
        ) : (
          <Space direction="vertical" size={0}>
            {v.map((s) => (
              <Text key={s} code style={{ fontSize: 12 }}>
                {s}
              </Text>
            ))}
          </Space>
        )
      },
    },
    {
      title: t('common.status'),
      dataIndex: 'effective_status',
      render: (v: string, r: BuildAgent) => {
        const hb = r.last_heartbeat
          ? formatDateTime(r.last_heartbeat)
          : t('nodes.neverHeartbeat')
        return (
          <Space direction="vertical" size={0}>
            {v === 'uninstalled' ? <Tag>{t('nodes.uninstalled')}</Tag>
              : v === 'uninstalling' ? <Tag color="warning">{t('nodes.uninstalling')}</Tag>
              : v === 'online' ? <Tag color="success">{t('nodes.online')}</Tag>
              : <Tag>{t('nodes.offline')}</Tag>}
            <span style={{ fontSize: 11, color: '#999' }}>{t('nodes.heartbeatAt', { time: hb })}</span>
          </Space>
        )
      },
    },
    {
      title: t('common.version'),
      dataIndex: 'agent_version',
      render: (v: string, r: BuildAgent) => {
        if (!v) {
          return <Tag color="warning">{t('nodes.unknownOldAgent')}</Tag>
        }
        return (
          <Space direction="vertical" size={0}>
            <Text code style={{ fontSize: 12 }}>
              {v}
            </Text>
            {r.upgrading && <Tag color="processing">{t('nodes.autoUpgrading', { version: r.latest_version || '' })}</Tag>}
            {r.upgrade_stalled && (
              <Tooltip title={r.upgrade_error || t('nodes.upgradeOfflineTip')}>
                <Tag color="error">{t('nodes.upgradeStalled', { version: r.latest_version || '' })}</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: t('common.action'),
      width: 320,
      render: (_: unknown, r: BuildAgent) =>
        isAdmin ? (
        <Space wrap>
          {r.uninstalled ? null : (
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
            {t('nodes.modify')}
          </Button>
          )}
          {r.uninstalled ? null : (
          <Tooltip title={upgradeHint(r)}>
            <span>
              <Button
                size="small"
                icon={r.upgrading ? <LoadingOutlined /> : <CloudUploadOutlined />}
                disabled={!r.outdated || r.effective_status !== 'online'}
                onClick={() => handleUpgrade(r)}
              >
                {r.upgrading ? t('nodes.upgrading') : r.upgrade_stalled ? t('nodes.retryUpgrade') : t('common.upgrade')}
              </Button>
            </span>
          </Tooltip>
          )}
          {r.uninstalled ? null : (
            <Popconfirm
              title={t('nodes.uninstallTitle', { name: r.name })}
              description={t('nodes.uninstallBody')}
              okText={t('nodes.uninstall')}
              okButtonProps={{ danger: true }}
              cancelText={t('common.cancel')}
              onConfirm={() => handleUninstall(r)}
            >
              <Button size="small" danger icon={<DisconnectOutlined />} disabled={!!r.uninstall_requested}>
                {r.uninstall_requested ? t('nodes.uninstalling') : t('nodes.uninstall')}
              </Button>
            </Popconfirm>
          )}
          {r.uninstall_requested && !r.uninstalled ? (
            <Button size="small" onClick={() => handleConfirmUninstalled(r)}>
              {t('nodes.confirmUninstalled')}
            </Button>
          ) : null}
          {r.uninstalled ? (
            <Button size="small" icon={<RedoOutlined />} onClick={() => openEdit(r)}>
              {t('nodes.reinstall')}
            </Button>
          ) : null}
          <Tooltip title={r.can_delete ? t('nodes.uninstalledHint') : t('nodes.deleteNeedUninstall')}>
            <span>
              <Button size="small" danger icon={<DeleteOutlined />} disabled={!r.can_delete} onClick={() => handleDelete(r)}>
                {t('nodes.remove')}
              </Button>
            </span>
          </Tooltip>
        </Space>
        ) : null,
    },
  ]

  return (
    <div>
      <Card
        title={t('nodes.title')}
        extra={
          <Space>
            <Button icon={<ApartmentOutlined />} onClick={() => setGroupPanelOpen(true)}>
              {t('nodes.manageGroups')}
            </Button>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              {t('common.refresh')}
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
              {t('nodes.addNode')}
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          message={t('nodes.alertTitle')}
          description={
            <>
              {t('nodes.alertDescBefore')}
              <b>{t('nodes.builderMachines')}</b>
              {t('nodes.alertDescAfter')}
            </>
          }
          style={{ marginBottom: 16 }}
        />

        {jdkLinux && !jdkLinux.ready ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('nodes.noJdkTitle')}
            description={t('nodes.noJdkDesc')}
            action={jdkUploadButton}
          />
        ) : null}

        <Space style={{ marginBottom: 12 }} wrap>
          <Input.Search
            allowClear
            placeholder={t('nodes.searchPlaceholder')}
            style={{ width: 260 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
          />
          <Select
            allowClear
            placeholder={t('nodes.filterGroup')}
            style={{ width: 200 }}
            value={groupFilter}
            onChange={setGroupFilter}
            options={groups.map((g) => ({
              value: g.id,
              label: t('nodes.groupWithCount', { name: g.name, n: g.member_count }),
            }))}
          />
          <Select
            allowClear
            placeholder={t('nodes.filterEnv')}
            style={{ width: 170 }}
            value={envFilter}
            onChange={setEnvFilter}
            options={envSelectOptions(Object.keys(envCount)).map((o) => ({
              value: o.value,
              label: t('nodes.envWithCount', { name: o.label, n: envCount[o.value] || 0 }),
            }))}
          />
          {isAdmin && selectedIds.length > 0 && (
            <>
              <Text type="secondary">{t('nodes.selectedN', { n: selectedIds.length })}</Text>
              <Button
                type="primary"
                icon={<ApartmentOutlined />}
                onClick={() => {
                  setAssignGroupId(undefined)
                  setNewGroupName('')
                  setAssignOpen(true)
                }}
              >
                {t('nodes.assignGroups')}
              </Button>
              <Button onClick={() => setSelectedIds([])}>{t('nodes.clearSelection')}</Button>
            </>
          )}
        </Space>

        <DataTable
          chromeKey="nodes"
          rowKey="id"
          columns={columns}
          dataSource={filtered}
          rowSelection={
            isAdmin
              ? {
                  selectedRowKeys: selectedIds,
                  onChange: (keys) => setSelectedIds(keys as number[]),
                }
              : undefined
          }
          // 几百台时不分页会把整张表一次渲染出来，页面会卡死
          pagination={{
            showTotal: (n) => t('nodes.totalMachines', { n }),
          }}
          locale={{
            emptyText: nodes.length ? t('nodes.emptyMatch') : t('nodes.emptyHint'),
          }}
        />
      </Card>

      <Modal
        title={t('nodes.assignTitle', { n: selectedIds.length })}
        open={assignOpen}
        onCancel={() => setAssignOpen(false)}
        footer={[
          <Button key="cancel" onClick={() => setAssignOpen(false)}>
            {t('common.cancel')}
          </Button>,
          <Button key="remove" danger disabled={!assignGroupId} onClick={() => applyAssign(false)}>
            {t('nodes.leaveGroup')}
          </Button>,
          <Button
            key="add"
            type="primary"
            disabled={!assignGroupId}
            onClick={() => applyAssign(true)}
          >
            {t('nodes.joinGroup')}
          </Button>,
        ]}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('nodes.assignAlertTitle')}
          description={t('nodes.assignAlertDesc')}
        />
        <Select
          style={{ width: '100%' }}
          placeholder={t('nodes.selectGroup')}
          value={assignGroupId}
          onChange={setAssignGroupId}
          options={groups.map((g) => ({
            value: g.id,
            label: t('nodes.groupCurrentCount', { name: g.name, n: g.member_count }),
          }))}
          notFoundContent={<Empty description={t('nodes.noGroupsCreate')} />}
          dropdownRender={(menu) => (
            <>
              {menu}
              {isAdmin ? (
                <>
                  <Divider style={{ margin: '8px 0' }} />
                  <Space.Compact
                    style={{ width: '100%', padding: '0 8px 8px' }}
                    onMouseDown={(e) => e.preventDefault()}
                  >
                    <Input
                      placeholder={t('nodes.newGroupPlaceholder')}
                      value={newGroupName}
                      onChange={(e) => setNewGroupName(e.target.value)}
                      onKeyDown={(e) => e.stopPropagation()}
                      onPressEnter={() => createAssignGroup()}
                    />
                    <Button type="primary" loading={creatingGroup} onClick={createAssignGroup}>
                      {t('nodes.createNew')}
                    </Button>
                  </Space.Compact>
                </>
              ) : null}
            </>
          )}
        />
      </Modal>

      <Modal
        title={t('nodes.groupsTitle')}
        open={groupPanelOpen}
        onCancel={() => {
          setGroupPanelOpen(false)
          setEditingGroup(null)
          groupForm.resetFields()
        }}
        footer={null}
        width={640}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('nodes.groupsAlertTitle')}
          description={t('nodes.groupsAlertDesc')}
        />

        <Form form={groupForm} layout="inline" style={{ marginBottom: 16 }}>
          {isAdmin ? (
            <>
              <Form.Item name="name" rules={[{ required: true, message: t('nodes.nameRequired') }]}>
                <Input placeholder={t('nodes.groupNamePlaceholder')} style={{ width: 200 }} />
              </Form.Item>
              <Form.Item name="description">
                <Input placeholder={t('nodes.descOptional')} style={{ width: 200 }} />
              </Form.Item>
              <Form.Item>
                <Space>
                  <Button type="primary" onClick={saveGroup}>
                    {editingGroup ? t('nodes.saveEdits') : t('nodes.createGroup')}
                  </Button>
                  {editingGroup && (
                    <Button
                      onClick={() => {
                        setEditingGroup(null)
                        groupForm.resetFields()
                      }}
                    >
                      {t('nodes.cancelEdit')}
                    </Button>
                  )}
                </Space>
              </Form.Item>
            </>
          ) : null}
        </Form>

        <List
          bordered
          dataSource={groups}
          locale={{ emptyText: t('nodes.noGroups') }}
          renderItem={(g) => (
            <List.Item
              actions={
                isAdmin
                  ? [
                <Button
                  key="edit"
                  size="small"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setEditingGroup(g)
                    groupForm.setFieldsValue({ name: g.name, description: g.description })
                  }}
                >
                  {t('nodes.rename')}
                </Button>,
                <Popconfirm
                  key="del"
                  title={t('nodes.deleteGroupTitle', { name: g.name })}
                  description={t('nodes.deleteGroupBody')}
                  okText={t('common.delete')}
                  okButtonProps={{ danger: true }}
                  cancelText={t('common.cancel')}
                  onConfirm={() => removeGroup(g)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>
                    {t('common.delete')}
                  </Button>
                </Popconfirm>,
                  ]
                : undefined
              }
            >
              <List.Item.Meta
                title={
                  <Space>
                    <Tag color="geekblue">{g.name}</Tag>
                    <Text type="secondary">{t('nodes.nMachines', { n: g.member_count })}</Text>
                  </Space>
                }
                description={g.description || <Text type="secondary">{t('nodes.noDescription')}</Text>}
              />
            </List.Item>
          )}
        />
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
          {t('nodes.membersHint')}
        </Text>
      </Modal>

      <Modal
        title={
          editingNode?.uninstalled
            ? t('nodes.reinstallTitle', { name: editingNode.name })
            : editingNode
              ? t('nodes.editNodeTitle', { name: editingNode.name })
              : t('nodes.addNode')
        }
        open={open}
        onCancel={closeNodeModal}
        footer={null}
        width={760}
        destroyOnClose
      >
        {editingNode?.uninstalled ? (
          <Alert
            type="info"
            showIcon
            message={t('nodes.reinstall')}
            description={t('nodes.reinstallNote')}
            style={{ marginBottom: 16 }}
          />
        ) : editingNode && (editingNode.effective_status !== 'online' || editingNode.upgrade_stalled) ? (
          <Alert
            type="error"
            showIcon
            message={t('nodes.offlineOrStalled')}
            description={t('nodes.offlineOrStalledDesc')}
            style={{ marginBottom: 16 }}
          />
        ) : null}
        <Alert
          type="warning"
          showIcon
          message={t('nodes.pathsBoundaryTitle')}
          description={
            <>
              {t('nodes.pathsBoundaryBefore')}
              <b>{t('nodes.pathsBoundaryBold')}</b>
              {t('nodes.pathsBoundaryAfter')}
              {editingNode ? (
                <>
                  <br />
                  {t('nodes.pathsBoundaryEdit')}
                </>
              ) : null}
            </>
          }
          style={{ marginBottom: 16 }}
        />

        <Radio.Group
          value={os}
          disabled={!!editingNode}
          onChange={(e) => {
            setOs(e.target.value)
            forceRender((n) => n + 1)
          }}
          style={{ marginBottom: 16 }}
        >
          <Radio.Button value="windows">{t('nodes.windowsIis')}</Radio.Button>
          <Radio.Button value="linux">{t('nodes.linuxSystemd')}</Radio.Button>
        </Radio.Group>

        {os === 'linux' && jdkLinux && !jdkLinux.ready ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('nodes.uploadJdkFirst')}
            description={t('nodes.uploadJdkFirstDesc')}
            action={jdkUploadButton}
          />
        ) : null}

        <Form
          key={editingNode ? `edit-${editingNode.id}` : 'create'}
          form={form}
          layout="vertical"
          initialValues={
            editingNode
              ? {
                  name: editingNode.name,
                  serverUrl: DEFAULT_SERVER_URL,
                  env: editingNode.env || 'prod',
                  allowPaths: (editingNode.allow_paths || []).join('\n'),
                  allowServices: (editingNode.allow_services || []).join('\n'),
                }
              : { serverUrl: DEFAULT_SERVER_URL, env: 'prod' }
          }
          onValuesChange={() => forceRender((n) => n + 1)}
        >
          <Form.Item
            label={t('nodes.nameUnique')}
            name="name"
            extra={editingNode ? t('nodes.nameIdentity') : undefined}
            rules={[
              { required: true, message: t('nodes.nameRequired') },
              {
                validator: (_, value) => {
                  const name = String(value || '')
                  if (!name) return Promise.resolve()
                  if (name.length > 64) return Promise.reject(new Error(t('nodes.nameMax')))
                  if (NODE_NAME_UNSAFE.test(name)) {
                    return Promise.reject(new Error(t('nodes.nameUnsafe')))
                  }
                  return Promise.resolve()
                },
              },
            ]}
          >
            <Input
              disabled={!!editingNode}
              placeholder={os === 'linux' ? t('nodes.namePhLinux') : t('nodes.namePhWindows')}
            />
          </Form.Item>

          <Form.Item label={t('nodes.serverUrl')} name="serverUrl" rules={[{ required: true }]}>
            <Input placeholder={DEFAULT_SERVER_URL} />
          </Form.Item>

          <Form.Item
            label={t('common.environment')}
            name="env"
            extra={
              <>
                {t('nodes.envExtraBefore')}
                <b>{t('nodes.envExtraBold')}</b>
                {t('nodes.envExtraAfter')}
              </>
            }
          >
            <Select options={envOptions()} />
          </Form.Item>

          <Form.Item
            label={t('nodes.allowPathsOnePerLine')}
            name="allowPaths"
            rules={[{ required: true }]}
            extra={t('nodes.allowPathsExtra')}
          >
            <Input.TextArea
              rows={3}
              placeholder={
                os === 'linux'
                  ? '/var/www/o2o\n/opt/app/api'
                  : 'D:\\wwwroot\\o2o\nD:\\wwwroot\\api'
              }
            />
          </Form.Item>

          {/* 整机备份位置只在安装命令里生效；空着由脚本按安装目录推导，不能理解成「整盘可写」。 */}
          <Form.Item
            label={t('nodes.backupRoot')}
            name="backupRoot"
            extra={t('nodes.backupRootExtra')}
          >
            <Input
              placeholder={os === 'linux' ? t('nodes.backupRootPhLinux') : t('nodes.backupRootPhWindows')}
            />
          </Form.Item>

          {os === 'windows' && (
            <Form.Item
              label={t('nodes.allowIis')}
              name="allowIis"
              extra={
                <>
                  {t('nodes.allowIisExtra1')}
                  <Text code>{t('nodes.siteName')}</Text>
                  {' / '}
                  <Text code>{t('nodes.apppoolName')}</Text>
                  {t('nodes.allowIisExtra2')}
                </>
              }
            >
              <Input.TextArea rows={3} placeholder={t('nodes.allowIisPlaceholder')} />
            </Form.Item>
          )}

          {os === 'linux' && (
            <Form.Item
              label={t('nodes.allowServicesLabel')}
              name="allowServices"
              extra={
                <>
                  {t('nodes.linuxServicesExtra1')}
                  <b>{t('nodes.linuxServicesExtraBold')}</b>
                  {t('nodes.linuxServicesExtra2')}
                  <br />
                  {t('nodes.linuxServicesDocker1')}
                  <Text code>{t('nodes.dockerContainerSyntax')}</Text>
                  {t('nodes.linuxServicesDocker2')}
                  <Text code>docker</Text>
                  {t('nodes.linuxServicesDocker3')}
                  <Text code>docker.service</Text>
                  {t('nodes.linuxServicesDocker4')}
                </>
              }
              rules={[
                {
                  validator: (_, value) => {
                    // 裸类型名按「省略类型即 systemd」的规则会变成 systemd:docker，
                    // 也就是允许停掉整个 Docker，机器上所有容器跟着下线。
                    // 装完才发现和本意差这么远太晚了，在这儿就拦住
                    const bad = String(value || '')
                      .split(/[\n;,]+/)
                      .map((s) => s.trim().toLowerCase())
                      .filter((s) => s === 'docker' || s === 'systemd')
                    if (bad.length) {
                      return Promise.reject(
                        new Error(t('nodes.dockerAmbiguous', { name: bad[0] })),
                      )
                    }
                    return Promise.resolve()
                  },
                },
              ]}
            >
              <Input.TextArea rows={3} placeholder={'systemd:nginx\nsystemd:tomcat\ndocker:web'} />
            </Form.Item>
          )}
        </Form>

        {(() => {
          const result = renderCommand()
          if (!result) {
            return (
              <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                {t('nodes.fillToGenerate')}
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
                <Radio.Button value="script">{t('nodes.installScript')}</Radio.Button>
                <Radio.Button value="manual">{t('nodes.manualCmd')}</Radio.Button>
              </Radio.Group>
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
              <Text type="secondary" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                {result.note}
              </Text>
            </>
          )
        })()}

        <Space style={{ marginTop: 12 }} wrap>
          {editingNode ? (
            <Button type="primary" loading={savingNode} onClick={saveEditingNode}>
              {t('nodes.saveToPlatform')}
            </Button>
          ) : null}
          <Button type={editingNode ? 'default' : 'primary'} icon={<CopyOutlined />} onClick={copyCommand}>
            {editingNode?.uninstalled
              ? t('nodes.reinstall')
              : editingNode
                ? t('nodes.copyRerun')
                : t('nodes.copyCmd')}
          </Button>
          <Button icon={<CopyOutlined />} onClick={copyEnrollToken}>
            {t('nodes.copyEnroll')}
          </Button>
          <Button icon={<DownloadOutlined />} onClick={downloadJar}>
            {t('nodes.downloadAgent')}
          </Button>
          {isAdmin ? (
            <Button icon={<ReloadOutlined />} onClick={rotateToken} danger>
              {t('nodes.rotateTitle')}
            </Button>
          ) : null}
          <Button icon={<ReloadOutlined />} onClick={refresh}>
            {t('nodes.refreshList')}
          </Button>
        </Space>

        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 12 }}>
          {os === 'linux' ? (
            <>
              <b>{t('nodes.steps')}</b>
              {t('nodes.linuxStep')}
              {editingNode ? t('nodes.linuxRerunHint') : ''}
              <br />
              {t('nodes.linuxServicePlugin')}
            </>
          ) : (
            <>
              <b>{t('nodes.steps')}</b>
              {t('nodes.windowsStepBefore')}
              <b>{t('nodes.windowsAdmin')}</b>
              {t('nodes.windowsStepAfter')}
            </>
          )}
        </Text>
      </Modal>

      <Modal
        title={pathsNode ? t('nodes.editPathsTitleNamed', { name: pathsNode.name }) : t('nodes.editPathsTitle')}
        open={!!pathsNode}
        onCancel={() => setPathsNode(null)}
        onOk={saveAllowPaths}
        confirmLoading={savingPaths}
        okText={t('common.save')}
        destroyOnClose
      >
        <Input.TextArea
          rows={5}
          value={pathsText}
          onChange={(e) => setPathsText(e.target.value)}
          placeholder={'D:\\wwwroot\\o2o\nD:\\wwwroot\\api'}
        />
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          {t('nodes.pathsModalHint')}
        </Text>
      </Modal>
    </div>
  )
}
