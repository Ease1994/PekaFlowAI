import { useState, useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Drawer, Form, Input, Select, Button, Space, Divider, Tabs, Alert } from 'antd'
import { DeleteOutlined, CopyOutlined } from '@ant-design/icons'
import type { GraphStage, GraphJob, GraphStep, Plugin, Variable, BuildAgent, Repository } from '@/api/types'
import { get } from '@/api/client'
import { agentDisplay } from '@/utils/agentLabel'
import { envLabel, machineEnv } from '@/env'
import VariableReference from './VariableReference'
import PluginFormRenderer from './PluginFormRenderer'
import { useT } from '@/i18n'

export type ConfigTarget =
  | { type: 'stage'; stage: GraphStage; index: number }
  | { type: 'job'; job: GraphJob; stageIndex: number; jobIndex: number }
  | { type: 'step'; step: GraphStep; stageIndex: number; jobIndex: number; stepIndex: number }

interface ConfigDrawerProps {
  target: ConfigTarget | null
  plugins: Plugin[]
  /** 流水线级全局变量（用于"引用变量"弹窗） */
  variables?: Variable[]
  /** 流水线所属项目 ID（用于加载代码库下拉等） */
  projectId?: number
  /** 当前流水线 ID（run-pipeline 排除自身） */
  pipelineId?: number
  envCode?: string
  onClose: () => void
  onSaveStage?: (stage: GraphStage) => void
  onSaveJob?: (job: GraphJob) => void
  onSaveStep?: (step: GraphStep) => void
  onDeleteStage?: () => void
  onDeleteJob?: () => void
  onDeleteStep?: () => void
  onDuplicateStep?: () => void
  /** 步骤的「重选」插件按钮被点击 —— 父组件决定如何打开 PluginSelector */
  onReselectPlugin?: () => void
}

/**
 * 配置侧边栏（对应蓝盾右侧 Drawer）
 * - Stage：名称、触发器
 * - Job：ID、agent、资源、超时、依赖
 * - Step：插件、参数(JSON)、Content(代码)、高级选项
 */
export default function ConfigDrawer({
  target,
  plugins,
  variables,
  projectId,
  pipelineId,
  envCode,
  onClose,
  onSaveStage,
  onSaveJob,
  onSaveStep,
  onDeleteStage,
  onDeleteJob,
  onDeleteStep,
  onDuplicateStep,
  onReselectPlugin,
}: ConfigDrawerProps) {
  const [variableRefOpen, setVariableRefOpen] = useState(false)
  const t = useT()

  // 项目代码库列表（git-checkout 下拉用）
  const { data: repos = [] } = useQuery({
    queryKey: ['repositories', projectId],
    queryFn: () => get<Repository[]>('/repositories', { project_id: projectId }),
    enabled: !!projectId,
  })

  if (!target) return null

  const title =
    target.type === 'stage'
      ? t('pipe.configStage')
      : target.type === 'job'
      ? t('pipe.configJob', { name: agentDisplay(target.job.agent).text, id: target.job.id })
      : target.type === 'step'
      ? target.step.name || target.step.display_name || target.step.plugin
      : ''

  return (
    <>
      <Drawer
        title={title}
        open={!!target}
        onClose={onClose}
        width={520}
        destroyOnClose
        extra={
          <Space>
            <Button type="link" onClick={() => setVariableRefOpen(true)}>
              {t('pipe.refVar')}
            </Button>
            {target.type === 'stage' && onDeleteStage && (
              <Button danger icon={<DeleteOutlined />} onClick={onDeleteStage}>
                {t('pipe.deleteStage')}
              </Button>
            )}
            {target.type === 'job' && onDeleteJob && (
              <Button danger icon={<DeleteOutlined />} onClick={onDeleteJob}>
                {t('pipe.deleteJob')}
              </Button>
            )}
            {target.type === 'step' && onDuplicateStep && (
              <Button icon={<CopyOutlined />} onClick={onDuplicateStep}>
                {t('pipe.copyStep')}
              </Button>
            )}
            {target.type === 'step' && onDeleteStep && (
              <Button danger icon={<DeleteOutlined />} onClick={onDeleteStep}>
                {t('pipe.deleteStep')}
              </Button>
            )}
          </Space>
        }
      >
        {/* key 保证在不同 Stage/Job/Step 之间切换时表单状态被重建，不会串到上一个目标 */}
        {target.type === 'stage' && (
          <StageForm
            key={`stage-${target.index}`}
            stage={target.stage}
            onChange={(s) => onSaveStage?.(s)}
          />
        )}
        {target.type === 'job' && (
          <JobForm
            key={`job-${target.stageIndex}-${target.jobIndex}`}
            job={target.job}
            onOpenVariableRef={() => setVariableRefOpen(true)}
            onChange={(j) => onSaveJob?.(j)}
            envCode={envCode}
          />
        )}
        {target.type === 'step' && (
          <StepForm
            key={`step-${target.stageIndex}-${target.jobIndex}-${target.stepIndex}`}
            step={target.step}
            plugins={plugins}
            repositories={repos}
            pipelineId={pipelineId}
            projectId={projectId}
            envCode={envCode}
            onReselectPlugin={onReselectPlugin}
            onOpenVariableRef={() => setVariableRefOpen(true)}
            onChange={(s) => onSaveStep?.(s)}
          />
        )}
      </Drawer>

      <VariableReference
        open={variableRefOpen}
        onClose={() => setVariableRefOpen(false)}
        variables={variables}
      />
    </>
  )
}

// ============ Stage 配置 ============
function StageForm({ stage, onChange }: { stage: GraphStage; onChange: (s: GraphStage) => void }) {
  const t = useT()
  const [form] = Form.useForm()

  useEffect(() => {
    form.setFieldsValue({ name: stage.name })
  }, [stage.id, stage.name, form])

  return (
    <Form
      form={form}
      layout="vertical"
      onValuesChange={(_, all) => onChange({ ...stage, name: all.name || stage.name })}
    >
      <Form.Item label={t("pipe.stageName")} name="name" rules={[{ required: true }]}>
        <Input placeholder={t("pipe.stageNamePh")} />
      </Form.Item>
      <Divider>{t('pipe.triggersTab')}</Divider>
      <Alert
        type="info"
        showIcon
        message={t("pipe.stageTriggerHint")}
        style={{ marginBottom: 16 }}
      />
    </Form>
  )
}

// ============ Job 配置 ============
function JobForm({
  job,
  envCode,
  onOpenVariableRef,
  onChange,
}: {
  job: GraphJob
  envCode?: string
  onOpenVariableRef?: () => void
  onChange: (j: GraphJob) => void
}) {
  const t = useT()
  const [form] = Form.useForm()

  // 从构建机管理动态加载（部署节点不在此列，它们由部署插件按步骤指定）
  const { data: agents = [] } = useQuery({
    queryKey: ['agents', 'builder'],
    queryFn: () => get<BuildAgent[]>('/agents?role=builder'),
    refetchInterval: 30000,
  })

  const osIcon = (os: string) => (os === 'linux' ? '🐧' : os === 'windows' ? '🪟' : '🍎')

  const parseTags = (raw: string): string[] => {
    try {
      const v = JSON.parse(raw || '[]')
      return Array.isArray(v) ? v.map(String).filter(Boolean) : []
    } catch {
      return (raw || '').split(',').map((s) => s.trim()).filter(Boolean)
    }
  }

  // 指定具体机器时值记成 agent:<id>，调度直接按 ID 派发。早先只能靠标签间接指定，
  // 于是一台机器会因为有几个标签而在列表里出现好几次，没打标签的又根本选不了。
  const machineOptions = useMemo(
    () => {
      const want = envCode ? machineEnv(envCode) : ''
      const currentId = job.agent?.startsWith('agent:') ? job.agent.slice(6) : ''
      return agents
        .filter((a) => {
          if (!want) return true
          if (String(a.id) === currentId) return true
          return machineEnv(a.env) === want
        })
        .map((a) => {
          const online = (a.effective_status ?? a.status) === 'online'
          const envBit = envLabel(a.env || 'prod')
          return {
            value: `agent:${a.id}`,
            label: `${online ? '🟢' : '⚪'} ${a.name}（${envBit} · ${osIcon(a.os)} ${a.os}${online ? '' : t('pipe.agentOffline')}）`,
          }
        })
    },
    [agents, envCode, job.agent, t],
  )

  // 标签只保留人工打的：Agent 没配标签时会自动按系统打一个，那和下面的「按环境匹配」
  // 完全重复，列出来只会让人以为是两种不同的东西。
  // 只列本环境机器上的标签——跨环境的标签选了也会在领取时对不上，等于卡死。
  const agentTagOptions = useMemo(() => {
    const osNames = new Set(['linux', 'windows', 'macos', 'any'])
    const want = envCode ? machineEnv(envCode) : ''
    const current = job.agent && !job.agent.startsWith('agent:') && !osNames.has(job.agent)
      ? job.agent
      : ''
    const byTag = new Map<string, { names: string[]; online: number }>()
    for (const a of agents) {
      if (want && machineEnv(a.env) !== want) continue
      for (const tag of parseTags(a.tags)) {
        if (osNames.has(tag.toLowerCase())) continue
        const hit = byTag.get(tag) || { names: [], online: 0 }
        hit.names.push(a.name)
        if ((a.effective_status ?? a.status) === 'online') hit.online += 1
        byTag.set(tag, hit)
      }
    }
    if (current && !byTag.has(current)) {
      byTag.set(current, { names: [t('pipe.currentMismatch')], online: 0 })
    }
    return Array.from(byTag.entries()).map(([tag, info]) => ({
      value: tag,
      label: `🏷 ${tag}（${info.names.join('、')}）`,
    }))
  }, [agents, envCode, job.agent, t])

  useEffect(() => {
    form.setFieldsValue({ id: job.id, name: job.name, agent: job.agent })
  }, [job.id, job.name, job.agent, form])

  // 图标要按选中的值反查：值可能是 agent:<id>、系统名或标签，不能直接当系统名用
  const iconFor = (value: string): string => {
    if (value.startsWith('agent:')) {
      const id = Number(value.slice(6))
      const hit = agents.find((a) => a.id === id)
      return hit ? osIcon(hit.os) : '🖥️'
    }
    return ['linux', 'windows', 'macos'].includes(value) ? osIcon(value) : '🖥️'
  }

  const buildJob = (formVals: Record<string, unknown>): GraphJob => {
    const agent = (formVals.agent as string) || job.agent
    return {
      ...job,
      id: (formVals.id as string) || job.id,
      name: (formVals.name as string) || job.name,
      agent,
      agent_icon: iconFor(agent),
    }
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button type="link" onClick={onOpenVariableRef}>
          {t('pipe.refVar')}
        </Button>
      </div>

      <Form
        form={form}
        layout="vertical"
        onValuesChange={(_, all) => onChange(buildJob(all))}
      >
        <Form.Item label="Job ID" name="id" rules={[{ required: true }]}>
          <Input placeholder={t("pipe.jobNamePh")} />
        </Form.Item>
        <Form.Item label={t("pipe.jobName")} name="name" rules={[{ required: true }]}>
          <Input placeholder={t("pipe.jobNameExample")} />
        </Form.Item>
        <Form.Item
          label={t("pipe.buildResource")}
          name="agent"
          rules={[{ required: true }]}
          tooltip={t("pipe.buildResourceHint")}
        >
          <Select
            placeholder={t("pipe.pickAgent")}
            showSearch
            optionFilterProp="label"
          >
            <Select.OptGroup label={t('pipe.specifiedAgents', { n: machineOptions.length })}>
              {machineOptions.map((m) => (
                <Select.Option key={m.value} value={m.value} label={m.label}>
                  {m.label}
                </Select.Option>
              ))}
            </Select.OptGroup>
            <Select.OptGroup label={t("pipe.matchByOs")}>
              <Select.Option value="linux" label={t("pipe.matchLinux")}>
                {t("pipe.matchLinux")}
              </Select.Option>
              <Select.Option value="windows" label={t("pipe.matchWindows")}>
                {t("pipe.matchWindows")}
              </Select.Option>
              <Select.Option value="macos" label={t("pipe.matchMacos")}>
                {t("pipe.matchMacos")}
              </Select.Option>
              <Select.Option value="any" label={t("pipe.matchAny")}>
                {t("pipe.matchAny")}
              </Select.Option>
            </Select.OptGroup>
            {agentTagOptions.length > 0 && (
              <Select.OptGroup label={t("pipe.matchByTag")}>
                {agentTagOptions.map((t) => (
                  <Select.Option key={`tag-${t.value}`} value={t.value} label={t.label}>
                    {t.label}
                  </Select.Option>
                ))}
              </Select.OptGroup>
            )}
          </Select>
        </Form.Item>
        {agents.length === 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("pipe.noAgents")}
            description={t("pipe.noAgentsHint")}
          />
        )}
        {!!envCode && agents.length > 0 && machineOptions.length === 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('pipe.noEnvAgents', { env: envLabel(envCode) })}
            description={t("pipe.noEnvAgentsHint")}
          />
        )}
        <Alert
          type="info"
          showIcon
          message={t("pipe.closeThenSave")}
          style={{ marginTop: 8 }}
        />
      </Form>
    </>
  )
}

// ============ Step 配置 ============
function StepForm({
  step,
  plugins,
  repositories,
  pipelineId,
  projectId,
  envCode,
  onReselectPlugin,
  onOpenVariableRef,
  onChange,
}: {
  step: GraphStep
  plugins: Plugin[]
  repositories?: Repository[]
  pipelineId?: number
  projectId?: number
  envCode?: string
  onReselectPlugin?: () => void
  onOpenVariableRef?: () => void
  onChange: (s: GraphStep) => void
}) {
  const t = useT()
  const [form] = Form.useForm()
  const currentPlugin = plugins.find((p) => p.name === step.plugin)
  // 步骤名称：已填写的优先，否则用插件显示名
  const defaultName = (step.name || '').trim() || (currentPlugin?.display_name || step.display_name || step.plugin || '').trim()
  const [withText, setWithText] = useState(() =>
    step.with ? JSON.stringify(step.with, null, 2) : '{}'
  )
  // 解析后的结构化对象（供 PluginFormRenderer 受控使用）
  const parsedWith = useMemo<Record<string, unknown>>(() => {
    try {
      return withText.trim() ? JSON.parse(withText) : {}
    } catch {
      return {}
    }
  }, [withText])

  useEffect(() => {
    const fallback = (currentPlugin?.display_name || step.display_name || step.plugin || '').trim()
    const name = (step.name || '').trim() || fallback
    form.setFieldsValue({ name, plugin: step.plugin })
    setWithText(step.with ? JSON.stringify(step.with, null, 2) : '{}')
    // 旧流水线 name 为空时，打开抽屉就把插件名写进本地 graph，保存后不再空着
    if (!(step.name || '').trim() && name) {
      onChange({ ...step, name })
    }
  }, [step.plugin, step.order, form])

  // 构造最终对象：合并 step、当前参数（with）、可选覆盖；写入父级本地 graph
  const buildStep = (
    overrides: Partial<GraphStep> = {},
    nextWithText = withText,
  ): GraphStep => {
    let nextWith: Record<string, unknown> = {}
    try {
      nextWith = nextWithText.trim() ? JSON.parse(nextWithText) : {}
    } catch {
      nextWith = { _raw: nextWithText }
    }
    return { ...step, ...overrides, with: nextWith }
  }

  const pushChange = (
    overrides: Partial<GraphStep> = {},
    nextWithText = withText,
  ) => {
    onChange(buildStep(overrides, nextWithText))
  }

  return (
    <Tabs
      items={[
        {
          key: 'basic',
          label: t('pipe.basics'),
          children: (
            <Form
              form={form}
              layout="vertical"
              onValuesChange={(_, all) => pushChange({ name: all.name || '' })}
              initialValues={{ name: defaultName }}
            >
              <Form.Item label={t("pipe.stepName")} name="name">
                <Input placeholder={t("pipe.stepNameHint")} />
              </Form.Item>
              <Form.Item label="Step ID">
                <Input value={`step-${step.order}`} disabled />
              </Form.Item>
              <Form.Item label={t("pipe.plugin")}>
                <Input
                  value={currentPlugin?.display_name || step.plugin}
                  addonAfter={
                    <a onClick={() => onReselectPlugin?.()} style={{ cursor: 'pointer' }}>
                      {t('pipe.reselect')}
                    </a>
                  }
                  readOnly
                />
              </Form.Item>
              <Form.Item label={t("pipe.version")}>
                <Input value={currentPlugin?.version || '1.latest'} readOnly />
              </Form.Item>
              <Form.Item label={t("pipe.description")}>
                <Input.TextArea
                  value={currentPlugin?.description || t('pipe.noPluginDesc')}
                  readOnly
                  autoSize={{ minRows: 1, maxRows: 3 }}
                />
              </Form.Item>
              <Alert
                type="info"
                showIcon
                message={t("pipe.rememberThenSave")}
              />
            </Form>
          ),
        },
        {
          key: 'params',
          label: t('pipe.params'),
          children: (
            <>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
                <span style={{ fontSize: 12, color: '#666' }}>
                  {currentPlugin
                    ? t('pipe.paramsSchemaHint', { name: currentPlugin.display_name })
                    : t('pipe.paramsJsonHint')}
                </span>
                <Button type="link" onClick={onOpenVariableRef}>
                  {t('pipe.refVar')}
                </Button>
              </div>

              {currentPlugin ? (
                <PluginFormRenderer
                  plugin={currentPlugin}
                  value={parsedWith}
                  onChange={(nv) => {
                    const text = JSON.stringify(nv, null, 2)
                    setWithText(text)
                    pushChange({}, text)
                  }}
                  repositories={repositories}
                  pipelineId={pipelineId}
                  projectId={projectId}
                  envCode={envCode}
                />
              ) : (
                <Input.TextArea
                  value={withText}
                  onChange={(e) => {
                    const text = e.target.value
                    setWithText(text)
                    pushChange({}, text)
                  }}
                  rows={12}
                  style={{ fontFamily: 'monospace', fontSize: 12 }}
                  placeholder='{"branch": "master", "namespace": "cop-prod"}'
                />
              )}
              <Alert
                type="info"
                showIcon
                message={t("pipe.paramsRemember")}
                style={{ marginTop: 12 }}
              />
            </>
          ),
        },
      ]}
    />
  )
}

export { VariableEditor } from './VariableEditor'
