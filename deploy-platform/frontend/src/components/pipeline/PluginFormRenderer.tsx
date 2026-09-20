import { useMemo } from 'react'
import { Form, Input, Radio, Select, Checkbox, Switch, InputNumber } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import type { BuildAgent, Credential, Plugin, Repository } from '@/api/types'
import { envLabel, machineEnv } from '@/env'
import RunPipelineFields from './RunPipelineFields'
import { useT } from '@/i18n'

interface PluginFormRendererProps {
  plugin?: Plugin
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  /** 项目代码库列表（git-checkout 步骤的「代码库」下拉会读取此源） */
  repositories?: Repository[]
  /** 当前正在编辑的流水线（run-pipeline 下拉排除自身） */
  pipelineId?: number
  /** 流水线所属项目，凭证下拉按项目+全局过滤 */
  projectId?: number
  /** 流水线环境码，节点下拉只列出同环境的机器 */
  envCode?: string
  /** 用于在字段标签旁显示「引用变量」入口（后续可扩展） */
  onOpenVariableRef?: (key: string) => void
}

/**
 * 通用插件表单渲染器 —— 蓝盾风格。
 *
 * 根据 plugin.config_schema (DSL) 自动渲染不同控件：
 *   - text/textarea/password：Input / Input.TextArea / Input.Password
 *   - number：InputNumber
 *   - radio：Radio.Group
 *   - select：Select（下拉）
 *   - checkbox：复选框（label 在 field.label 上）
 *   - switch：开关
 *   - code：深色主题代码编辑器（Input.TextArea 模拟）
 *   - visibleWhen / inferTrueIf / clearOnOff：把少用字段藏在开关后面，关掉时清掉隐藏值
 *
 * Schema 格式：
 * {
 *   "fields": [
 *     {"key":"x", "label":"X", "type":"text", "required":true, "default":"", "placeholder":""},
 *     {"key":"y", "label":"Y", "type":"radio", "options":[{"value":"a","label":"A"}], "default":"a"},
 *     ...
 *   ]
 * }
 */
export default function PluginFormRenderer({ plugin, value, onChange, repositories, pipelineId, projectId, envCode }: PluginFormRendererProps) {
  const schema = useMemo(() => {
    if (!plugin?.config_schema) return { fields: [] }
    try {
      return JSON.parse(plugin.config_schema) as { fields: PluginField[] }
    } catch {
      return { fields: [] }
    }
  }, [plugin])

  const fields = schema.fields || []

  if (plugin?.name === 'run-pipeline') {
    return (
      <Form layout="vertical" colon={false}>
        <RunPipelineFields value={value} onChange={onChange} currentPipelineId={pipelineId} envCode={envCode} />
      </Form>
    )
  }

  if (fields.length === 0) {
    return (
      <div style={{ padding: 12, color: '#999', textAlign: 'center' }}>
        该插件暂无配置项（或未定义 schema）
      </div>
    )
  }

  const getField = (field: PluginField): unknown => {
    // 关联文本还在时，开关必须视为打开；否则隐藏字段会留下 YAML 里的旧路径，执行时仍会用。
    if (field.inferTrueIf) {
      const other = value[field.inferTrueIf]
      if (other !== undefined && other !== null && String(other).trim() !== '') return true
    }
    if (value[field.key] !== undefined) return value[field.key]
    return field.default
  }

  /** 开关关掉时清掉只在打开后才有意义的字段，避免隐藏值继续进 YAML。 */
  const setField = (key: string, v: unknown) => {
    const field = fields.find((f) => f.key === key)
    const next: Record<string, unknown> = { ...value, [key]: v }
    if (field?.clearOnOff && v === false) {
      for (const k of field.clearOnOff) next[k] = ''
    }
    onChange(next)
  }

  return (
    <Form layout="vertical" colon={false}>
      {fields.map((field) => {
        if (!isFieldVisible(field, fields, getField)) return null
        const v = getField(field)
        return (
          <FieldRow
            key={field.key}
            field={field}
            value={v}
            onChange={(nv) => setField(field.key, nv)}
            plugin={plugin}
            repositories={repositories}
            projectId={projectId}
            envCode={envCode}
          />
        )
      })}
    </Form>
  )
}

interface PluginField {
  key: string
  label: string
  type: string
  required?: boolean
  default?: unknown
  options?: { value: string | number | boolean; label: string }[]
  placeholder?: string
  help?: string
  rows?: number
  /** select 类型专属：选项来自平台数据而非写死的 options。node=部署节点，credential=镜像仓库凭证，ssh-credential=SSH 登录凭证 */
  source?: string
  /** group 类型专属：子字段 */
  children?: PluginField[]
  /**
   * 仅当其它字段当前值等于这里的期望时才渲染。
   * 用来把「多数情况不用填」的项藏在开关后面，避免占着必填位。
   */
  visibleWhen?: Record<string, string | number | boolean>
  /**
   * 开关从开到关时一并清空的字段。隐藏后如果还把旧路径留在 YAML 里，执行时仍会生效。
   */
  clearOnOff?: string[]
  /**
   * 本字段尚未写入时：若该 key 已有非空值，则本开关视为 true。
   * 用来打开老流水线里已经填过的自定义项，而不必改 YAML。
   */
  inferTrueIf?: string
}

/**
 * 按 visibleWhen 决定字段是否出现。引用的字段走 getField，以便带上 default / inferTrueIf。
 */
function isFieldVisible(
  field: PluginField,
  fields: PluginField[],
  read: (field: PluginField) => unknown,
): boolean {
  if (!field.visibleWhen) return true
  return Object.entries(field.visibleWhen).every(([key, expect]) => {
    const ref = fields.find((f) => f.key === key)
    const actual = ref ? read(ref) : undefined
    return actual === expect
  })
}

function FieldRow({
  field,
  value,
  onChange,
  plugin,
  repositories,
  envCode,
  projectId,
}: {
  field: PluginField
  value: unknown
  onChange: (next: unknown) => void
  plugin?: Plugin
  repositories?: Repository[]
  envCode?: string
  /** 流水线所属项目，凭证下拉按项目 + 全局过滤 */
  projectId?: number
}) {
  const t = useT()
  const labelExtra = field.required ? <span style={{ color: '#ff4d4f' }}> *</span> : null

  // 部署类插件的「目标节点」下拉：只有用到时才发请求
  const needNodes = field.source === 'node' || (field.children || []).some((c) => c.source === 'node')
  const { data: nodes = [], isLoading: nodesLoading } = useQuery({
    queryKey: ['agents', 'node'],
    queryFn: () => get<BuildAgent[]>('/agents?role=node'),
    enabled: needNodes,
    staleTime: 30000,
  })

  // 镜像仓库凭证 / SSH 凭证：项目凭证 + 全局凭证。
  const needCreds =
    field.source === 'credential' ||
    field.source === 'ssh-credential' ||
    (field.children || []).some((c) => c.source === 'credential' || c.source === 'ssh-credential')
  const { data: credentials = [], isLoading: credsLoading } = useQuery({
    queryKey: ['credentials', projectId ?? 'none'],
    queryFn: () =>
      get<(Credential & { project_name?: string })[]>(
        '/credentials',
        projectId ? { project_id: projectId } : { scope: 'all' },
      ),
    enabled: needCreds,
    staleTime: 30000,
  })
  const registryCreds = credentials.filter((c) => c.type !== 'ssh')
  /** SSH 发送文件：私钥或账号密码，Token 不能登录 SSH。 */
  const sshCreds = credentials.filter((c) => c.type === 'ssh' || c.type === 'password')

  let control: React.ReactNode = null
  switch (field.type) {
    case 'text':
      control = <Input value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={field.placeholder} />
      break
    case 'password':
      control = <Input.Password value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={field.placeholder} />
      break
    case 'textarea':
      control = (
        <Input.TextArea
          value={(value as string) ?? ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          rows={field.rows ?? 3}
        />
      )
      break
    case 'number':
      control = (
        <InputNumber
          value={value as number | null}
          onChange={(nv) => onChange(nv)}
          placeholder={field.placeholder}
          style={{ width: '100%' }}
        />
      )
      break
    case 'radio':
      control = (
        <Radio.Group value={value} onChange={(e) => onChange(e.target.value)}>
          {(field.options || []).map((o) => (
            <Radio key={String(o.value)} value={o.value}>
              {o.label}
            </Radio>
          ))}
        </Radio.Group>
      )
      break
    case 'select': {
      // 特殊字段：git-checkout 步骤的「代码库」下拉 → 读项目代码库（按 alias）
      const isCodeBaseField = plugin?.name === 'git-checkout' && field.key === 'repoName'
      let options = (field.options || []).map((o) => ({ value: o.value, label: o.label }))
      if (isCodeBaseField && repositories) {
        options = repositories.map((r) => ({ value: r.alias, label: `${r.alias}${r.name ? `（${r.name}）` : ''}` }))
      } else if (field.source === 'node') {
        const want = envCode ? machineEnv(envCode) : ''
        const current = value != null ? String(value) : ''
        options = nodes
          .filter((n) => {
            if (!want) return true
            if (String(n.id) === current) return true
            return machineEnv(n.env) === want
          })
          .map((n) => ({
            value: n.id,
            label: `${n.name}${n.host ? `（${n.host}）` : ''} · ${envLabel(n.env)}${n.effective_status === 'online' ? '' : ` · ${t('pipe.offline')}`}`,
          }))
      } else if (field.source === 'credential') {
        options = registryCreds.map((c) => ({
          value: c.id,
          label: `${c.name}${c.project_id ? '' : `（${t('pipe.global')}）`} · ${c.type === 'password' ? t('pipe.password') : t('pipe.token')}`,
        }))
      } else if (field.source === 'ssh-credential') {
        options = sshCreds.map((c) => ({
          value: c.id,
          label: `${c.name}${c.project_id ? '' : `（${t('pipe.global')}）`} · ${c.type === 'ssh' ? t('pipe.sshKey') : t('pipe.password')}`,
        }))
      }
      // 凭证 id 是数字；YAML 里可能被存成字符串。代码库别名等其它 select 必须保持原样。
      const credSelect = field.source === 'credential' || field.source === 'ssh-credential'
      const selectValue =
        credSelect
          ? (value === undefined || value === null || value === ''
              ? undefined
              : Number(value))
          : (value as string | number | undefined)
      control = (
        <Select
          value={
            credSelect
              ? (Number.isFinite(selectValue as number) ? selectValue : undefined)
              : selectValue
          }
          onChange={(nv) => onChange(nv)}
          options={options}
          placeholder={field.placeholder}
          loading={(field.source === 'node' && nodesLoading) || (credSelect && credsLoading)}
          notFoundContent={
            field.source === 'node'
              ? (nodes.length === 0
                  ? t('pipe.noNodes')
                  : envCode
                    ? t('pipe.noEnvNodes', { env: envLabel(envCode) })
                    : t('pipe.noNodePick'))
              : field.source === 'credential'
                ? t('pipe.noCredToken')
                : field.source === 'ssh-credential'
                  ? t('pipe.noCredSsh')
                  : undefined
          }
          allowClear
          showSearch
          optionFilterProp="label"
          style={{ width: '100%' }}
        />
      )
      break
    }
    case 'checkbox':
      control = (
        <Checkbox checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)}>
          {field.label}
        </Checkbox>
      )
      break
    case 'switch':
      control = (
        <Switch checked={Boolean(value)} onChange={(nv) => onChange(nv)} />
      )
      break
    case 'code':
      control = (
        <Input.TextArea
          value={(value as string) ?? ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          rows={field.rows ?? 12}
          style={{
            fontFamily: 'monospace',
            fontSize: 12,
            background: '#1e1e1e',
            color: '#d4d4d4',
          }}
        />
      )
      break
    case 'group': {
      // 折叠组：children 字段递归渲染
      const children = (field.children || []) as PluginField[]
      const groupValues = (value as Record<string, unknown>) || {}
      control = (
        <div
          style={{
            border: '1px solid #d9d9d9',
            borderRadius: 4,
            overflow: 'hidden',
          }}
        >
          <details>
            <summary
              style={{
                padding: '8px 12px',
                background: '#fafafa',
                cursor: 'pointer',
                fontWeight: 500,
                userSelect: 'none',
                listStyle: 'none',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <span>{field.label}</span>
              <span style={{ color: '#999' }}>▾</span>
            </summary>
            <div style={{ padding: 12 }}>
              {children.map((child) => {
                const cv = groupValues[child.key] !== undefined ? groupValues[child.key] : child.default
                return (
                  <FieldRow
                    key={child.key}
                    field={child}
                    value={cv}
                    onChange={(nv) => onChange({ ...groupValues, [child.key]: nv })}
                    plugin={plugin}
                    repositories={repositories}
                    projectId={projectId}
                    envCode={envCode}
                  />
                )
              })}
            </div>
          </details>
        </div>
      )
      break
    }
    default:
      control = <Input value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} />
  }

  // checkbox 自带 label，这里不再重复
  if (field.type === 'checkbox') {
    return <Form.Item>{control}</Form.Item>
  }

  // 必填项为空时当场标红，否则要等到执行失败才知道漏填
  const missing =
    field.required &&
    field.type !== 'group' &&
    (value === undefined || value === null || value === '')

  return (
    <Form.Item
      label={<>{field.label}{labelExtra}</>}
      validateStatus={missing ? 'error' : undefined}
      help={missing ? t('pipe.requiredField') : field.help}
    >
      {control}
    </Form.Item>
  )
}
