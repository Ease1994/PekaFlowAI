import { useState } from 'react'
import { Alert, Button, Form, Input, Modal, Select, Space, Tag, message } from 'antd'
import { RocketOutlined } from '@ant-design/icons'
import { useQueryClient } from '@tanstack/react-query'
import { get, postR } from '@/api/client'
import type { Pipeline, Release } from '@/api/types'
import type { StartParam } from '@/components/pipeline/RunPipelineFields'
import ReleaseGateFields from '@/components/ReleaseGateFields'
import { pipelineNeedsManifest, manifestInputRequired } from '@/utils/releaseGate'
import { envColor, envLabel } from '@/env'
import { useT } from '@/i18n'

interface Props {
  pipeline?: Pipeline
  pipelineId?: number | string
  type?: 'primary' | 'default'
  size?: 'small' | 'middle' | 'large'
  text?: string
  onDone?: (release: Release) => void
}

const MULTI_TYPES = new Set(['checkbox', 'multiple', 'multiselect', 'checkbox-group'])

/**
 * 触发一次发布。流水线定义了「执行时显示」的变量就先弹参数面板；
 * 含提取增量包步骤时一并填写发布清单；步骤未写死文件时清单必填，空着会拒绝执行。
 * 生产分组会进入审批，分组开启应急跳审后可勾选跳审并填写原因。
 */
export default function ExecuteReleaseButton({
  pipeline,
  pipelineId,
  type = 'primary',
  size,
  text,
  onDone,
}: Props) {
  const qc = useQueryClient()
  const t = useT()
  const [open, setOpen] = useState(false)
  const [bypass, setBypass] = useState(false)
  const [reason, setReason] = useState('')
  // 本次执行填写的发布清单；空串表示沿用流水线步骤里的配置
  const [manifest, setManifest] = useState('')
  const [needManifest, setNeedManifest] = useState(false)
  const [loading, setLoading] = useState(false)
  const [startParams, setStartParams] = useState<StartParam[]>([])
  const [params, setParams] = useState<Record<string, string>>({})
  const [needApproval, setNeedApproval] = useState(!!pipeline?.approval_required)
  const [canBypass, setCanBypass] = useState(!!pipeline?.allow_emergency_bypass)
  const [allowSelfApproval, setAllowSelfApproval] = useState(!!pipeline?.allow_self_approval)
  const [envType, setEnvType] = useState(pipeline?.group_type)

  const id = pipelineId ?? pipeline?.id

  const run = async (
    payload?: Record<string, unknown>,
    extras?: { includeManifest?: boolean; manifestText?: string; skipStartParams?: boolean },
  ) => {
    if (!id) return
    setLoading(true)
    try {
      const body: Record<string, unknown> = { ...(payload || {}) }
      const skipStartParams = extras?.skipStartParams
      if (!skipStartParams && startParams.length) body.run_params = params
      // 有增量插件才带清单；空字符串也传，后端据此决定沿用步骤还是拒绝占位未填。
      // 清单开关走参数而不是只读 state：点执行后若一键放行，setState 还没刷完，不能误带上一次的框。
      const includeManifest = extras?.includeManifest ?? needManifest
      const manifestText = extras?.manifestText ?? manifest
      if (includeManifest) body.deploy_manifest = manifestText
      const res = await postR<Release>(
        `/pipelines/${id}/execute`,
        Object.keys(body).length ? body : undefined,
      )
      const release = res.data
      const num = release?.build_number || release?.id
      if (release?.status === 'failed' || (res.message && res.message !== 'ok' && res.message.includes(t('pipe.startFailedToken')))) {
        message.error(res.message && res.message !== 'ok' ? res.message : t('pipe.publishStartFailed'), 8)
      } else if (release?.status === 'pending') message.success(t('pipe.submittedPending', { n: num }))
      else if (payload?.emergency_bypass) message.warning(t('pipe.emergencyRan', { n: num }))
      else message.success(t('execBtn.triggered'))
      setOpen(false)
      setBypass(false)
      setReason('')
      setManifest('')
      if (release) onDone?.(release)
    } finally {
      setLoading(false)
    }
  }

  const handleClick = async () => {
    if (!id) return
    setLoading(true)
    let list: StartParam[] = []
    // 详情里通常已有能力位；只有 pipelineId 时再拉一次，避免无清单流水线被误弹框
    let meta: Pipeline | undefined = pipeline
    try {
      list = await qc.fetchQuery({
        queryKey: ['pipeline-start-params', id],
        queryFn: () => get<StartParam[]>(`/pipelines/${id}/start-params`),
        staleTime: 30000,
      })
      // 审批 / 跳审 / 清单以详情为准，不能只信列表上可能缺的字段，否则会一键放行、弹窗也不出跳审
      meta = await qc.fetchQuery({
        queryKey: ['pipeline', id],
        queryFn: () => get<Pipeline>(`/pipelines/${id}`),
        staleTime: 30000,
      })
    } catch {
      // 读不到启动参数（无权限等）不该挡住执行，按无参数处理
      list = []
    } finally {
      setLoading(false)
    }
    const showManifest = pipelineNeedsManifest(meta)
    const approval = !!meta?.approval_required
    setNeedApproval(approval)
    setCanBypass(!!meta?.allow_emergency_bypass)
    setAllowSelfApproval(!!meta?.allow_self_approval)
    setEnvType(meta?.group_type || pipeline?.group_type)
    setNeedManifest(showManifest)
    setManifest(meta?.deploy_manifest_default || '')
    setStartParams(list)
    setParams(Object.fromEntries(list.map((p) => [p.name, p.default_value ?? ''])))
    // 既不用填参数、又不用审批、又没有增量清单，就保持一键执行
    if (!list.length && !approval && !showManifest) {
      void run(undefined, { includeManifest: false, skipStartParams: true })
      return
    }
    setOpen(true)
  }

  const okText = needApproval ? (bypass ? t('execBtn.emergencyRun') : t('execBtn.submitApproval')) : t('execBtn.run')
  const blockManifest = manifestInputRequired(
    needManifest,
    pipeline?.deploy_manifest_default || '',
    manifest,
  )

  return (
    <>
      <Button type={type} size={size} icon={<RocketOutlined />} loading={loading && !open} onClick={handleClick}>
        {text || t('execBtn.run')}
      </Button>

      <Modal
        title={
          <Space>
            {t('execBtn.startTitle')}
            {envType ? (
              <Tag color={envColor(envType)}>{t('execBtn.envTag', { env: envLabel(envType) })}</Tag>
            ) : null}
          </Space>
        }
        open={open}
        onCancel={() => setOpen(false)}
        confirmLoading={loading}
        okText={okText}
        okButtonProps={{ danger: bypass, disabled: (bypass && !reason.trim()) || blockManifest }}
        onOk={() =>
          run(
            bypass && canBypass
              ? { emergency_bypass: true, emergency_bypass_reason: reason.trim() }
              : undefined,
            { includeManifest: needManifest, manifestText: manifest },
          )
        }
      >
        {startParams.length > 0 && (
          <Form layout="vertical" style={{ marginBottom: needApproval || needManifest ? 16 : 0 }}>
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={t('pipe.runParams')}
              description={t('pipe.runParamsHint')}
            />
            {startParams.map((p) => {
              const value = params[p.name] ?? ''
              const setValue = (v: string) => setParams((prev) => ({ ...prev, [p.name]: v }))
              const options = (p.options || []).map((o) => ({ label: o, value: o }))
              return (
                <Form.Item key={p.name} label={p.name} extra={p.description || undefined}>
                  {MULTI_TYPES.has((p.type || '').toLowerCase()) && options.length ? (
                    <Select
                      mode="multiple"
                      options={options}
                      value={value ? value.split(',').filter(Boolean) : []}
                      onChange={(vals: string[]) => setValue(vals.join(','))}
                    />
                  ) : options.length ? (
                    <Select options={options} value={value || undefined} onChange={setValue} />
                  ) : (
                    <Input
                      value={value}
                      onChange={(e) => setValue(e.target.value)}
                      placeholder={p.default_value || ''}
                    />
                  )}
                </Form.Item>
              )
            })}
          </Form>
        )}

        <ReleaseGateFields
          needManifest={needManifest}
          manifest={manifest}
          onManifestChange={setManifest}
          needApproval={needApproval}
          allowBypass={canBypass}
          allowSelfApproval={allowSelfApproval}
          bypass={bypass}
          onBypassChange={setBypass}
          reason={reason}
          onReasonChange={setReason}
          kind="execute"
        />
      </Modal>
    </>
  )
}
