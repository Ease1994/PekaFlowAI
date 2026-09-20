import { useEffect } from 'react'
import { Form, Input, Radio, Select, Checkbox, InputNumber, Alert } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import type { Pipeline, Project } from '@/api/types'
import { useT } from '@/i18n'

export interface StartParam {
  name: string
  type: string
  default_value: string
  description: string
  options: string[]
}

const MULTI_TYPES = new Set(['checkbox', 'multiple', 'multiselect', 'checkbox-group'])

/**
 * 运行流水线插件表单（对齐蓝鲸 SubPipelineExec）：
 * 项目 / 流水线动态下拉，启动子流水线参数（多选除外均为输入框）。
 */
export default function RunPipelineFields({
  value,
  onChange,
  currentPipelineId,
  envCode,
}: {
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  currentPipelineId?: number
  /** 当前流水线环境码，子流水线必须同环境，生产和测试不能互调 */
  envCode?: string
}) {
  const t = useT()
  const projectId = (value.projectId as number | string | undefined) ?? undefined
  const pipelineId = (value.pipelineId as number | string | undefined) ?? undefined
  const runMode = String(value.runMode || 'sync')
  const params = (value.params as Record<string, unknown>) || {}

  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
  })

  const { data: pipelines = [] } = useQuery({
    queryKey: ['pipelines', projectId],
    queryFn: () => get<Pipeline[]>('/pipelines', { project_id: Number(projectId) }),
    enabled: !!projectId,
  })

  const { data: startParams = [], isError } = useQuery({
    queryKey: ['pipeline-start-params', pipelineId],
    queryFn: () => get<StartParam[]>(`/pipelines/${pipelineId}/start-params`),
    enabled: !!pipelineId,
  })

  useEffect(() => {
    if (!startParams.length) return
    const next = { ...params }
    let changed = false
    for (const p of startParams) {
      if (next[p.name] === undefined) {
        next[p.name] = p.default_value ?? ''
        changed = true
      }
    }
    if (changed) onChange({ ...value, params: next })
    // 仅在选中流水线或参数定义变化时补默认值
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId, startParams])

  const set = (patch: Record<string, unknown>) => onChange({ ...value, ...patch })
  const setParam = (name: string, v: unknown) => set({ params: { ...params, [name]: v } })

  const want = (envCode || '').trim().toLowerCase()
  const pipelineOptions = pipelines
    .filter((p) => {
      if (p.id === currentPipelineId) return false
      if (!want) return true
      return (p.group_type || '').trim().toLowerCase() === want
    })
    .map((p) => ({
      value: p.id,
      label: `${p.name} (#${p.id})`,
    }))

  return (
    <>
      <Form.Item label={<>{t('pipe.project')} <span style={{ color: '#ff4d4f' }}>*</span></>}>
        <Select
          value={projectId != null && projectId !== '' ? Number(projectId) : undefined}
          placeholder={t("pipe.pickProject")}
          showSearch
          optionFilterProp="label"
          options={projects.map((p) => ({ value: p.id, label: `${p.name}（${p.code}）` }))}
          onChange={(id) => set({ projectId: id, pipelineId: undefined, params: {} })}
          style={{ width: '100%' }}
        />
      </Form.Item>
      <Form.Item label={<>{t('pipe.pipeline')} <span style={{ color: '#ff4d4f' }}>*</span></>}>
        <Select
          value={pipelineId != null && pipelineId !== '' ? Number(pipelineId) : undefined}
          placeholder={want ? t('pipe.pickSameEnv', { want }) : t('pipe.pickPipeline')}
          showSearch
          optionFilterProp="label"
          options={pipelineOptions}
          onChange={(id) => set({ pipelineId: id, params: {} })}
          disabled={!projectId}
          style={{ width: '100%' }}
        />
      </Form.Item>
      {want ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("pipe.envIsolated")}
        />
      ) : null}
      <Form.Item label={t("pipe.runMode")}>
        <Radio.Group value={runMode} onChange={(e) => set({ runMode: e.target.value })}>
          <Radio value="sync">{t("pipe.runSync")}</Radio>
          <Radio value="async">{t("pipe.runAsync")}</Radio>
        </Radio.Group>
      </Form.Item>
      {runMode === 'sync' && (
        <Form.Item label={t("pipe.pollInterval")} extra={t("pipe.pollHint")}>
          <InputNumber
            min={1}
            max={600}
            value={(value.pollInterval as number) ?? 10}
            onChange={(n) => set({ pollInterval: n ?? 10 })}
            style={{ width: '100%' }}
          />
        </Form.Item>
      )}
      {runMode === 'sync' && (
        <Form.Item label={t("pipe.outNs")} extra={t("pipe.outNsHint")}>
          <Input
            value={(value.outputNamespace as string) ?? 'sub_pipeline_'}
            onChange={(e) => set({ outputNamespace: e.target.value })}
            placeholder="sub_pipeline_"
          />
        </Form.Item>
      )}
      {runMode === 'sync' && (
        <Form.Item label={t("pipe.outVars")} extra={t("pipe.outVarsHint")}>
          <Input
            value={(value.outputVars as string) ?? ''}
            onChange={(e) => set({ outputVars: e.target.value })}
            placeholder="parameter1,parameter2"
          />
        </Form.Item>
      )}
      {isError && pipelineId && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t("pipe.noStartParams")} />
      )}
      {startParams.length > 0 && (
        <Form.Item label={t("pipe.childParams")} extra={t("pipe.childParamsHint")}>
          <div style={{ border: '1px solid #f0f0f0', borderRadius: 6, padding: 12 }}>
            {startParams.map((p) => {
              const v = params[p.name] !== undefined ? params[p.name] : p.default_value
              if (MULTI_TYPES.has((p.type || '').toLowerCase())) {
                const selected = String(v || '')
                  .split(',')
                  .map((x) => x.trim())
                  .filter(Boolean)
                return (
                  <Form.Item key={p.name} label={p.name} extra={p.description || undefined}>
                    <Checkbox.Group
                      options={(p.options || []).map((o) => ({ label: o, value: o }))}
                      value={selected}
                      onChange={(vals) => setParam(p.name, vals.join(','))}
                    />
                  </Form.Item>
                )
              }
              return (
                <Form.Item key={p.name} label={p.name} extra={p.description || undefined}>
                  <Input
                    value={v == null ? '' : String(v)}
                    onChange={(e) => setParam(p.name, e.target.value)}
                    placeholder={p.default_value || ''}
                  />
                </Form.Item>
              )
            })}
          </div>
        </Form.Item>
      )}
    </>
  )
}
