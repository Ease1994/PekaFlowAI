import { useMemo, useState } from 'react'
import { Alert, Button, Input, Modal, Space, Spin, Tag, message } from 'antd'
import { ReloadOutlined, RollbackOutlined } from '@ant-design/icons'
import { get, post } from '@/api/client'
import type { Pipeline, Release } from '@/api/types'
import ReleaseGateFields from '@/components/ReleaseGateFields'
import { pipelineNeedsManifest, manifestInputRequired } from '@/utils/releaseGate'
import { envColor, envLabel } from '@/env'
import { useT } from '@/i18n'

type RiskAction = 'rollback' | 'rebuild'

interface RollbackStep {
  name: string
  plugin: string
}

/** 回滚预览里的一条部署。容器镜像会带上当前 / 上一版 tag，确认框只改 tag。 */
interface RollbackItem {
  id: number
  kind: string
  kind_label: string
  target: string
  summary: string
  current_image?: string
  previous_image?: string
  image_repo?: string
  current_tag?: string
  previous_tag?: string
  steps?: RollbackStep[]
}

interface RollbackPreview {
  can_rollback: boolean
  reason: string
  summary: string
  warnings: string[]
  rolling?: boolean
  /** 流水线构建号，弹窗标题用；和执行历史列表上的 #N 一致 */
  build_number?: number
  items: RollbackItem[]
}

interface Props {
  action: RiskAction
  releaseId: number
  /** 流水线构建号；弹窗标题优先用它，避免把全库发布主键当成构建号 */
  buildNumber?: number
  /** 所属分组是否要求审批；来自发布列表或流水线详情 */
  approvalRequired?: boolean
  allowBypass?: boolean
  groupType?: string
  /** 有详情时 Rebuild 可直接判断要不要出发布清单，少打一次流水线接口 */
  pipeline?: Pipeline
  disabled?: boolean
  size?: 'small' | 'middle' | 'large'
  title?: string
  text?: string
  onDone?: (release: Release) => void
}

const META: Record<RiskAction, { path: string; icon: React.ReactNode }> = {
  rollback: { path: 'rollback', icon: <RollbackOutlined /> },
  rebuild: { path: 'rebuild', icon: <ReloadOutlined /> },
}

const LABEL: React.CSSProperties = {
  fontSize: 12,
  color: '#64748b',
  letterSpacing: 0.4,
  marginBottom: 6,
}

const REPO: React.CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontSize: 13,
  color: '#334155',
  wordBreak: 'break-all',
}

/**
 * 回滚 / Rebuild 按钮。两者都会把变更推到目标环境，需要审批的分组先进审批；
 * 分组开启应急跳审后可勾选跳审并填写原因，跳审会记审计并通知审批人。
 */
export default function RiskActionButton({
  action,
  releaseId,
  buildNumber,
  approvalRequired,
  allowBypass,
  groupType,
  pipeline,
  disabled,
  size = 'small',
  title,
  text,
  onDone,
}: Props) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [bypass, setBypass] = useState(false)
  const [reason, setReason] = useState('')
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState<RollbackPreview | null>(null)
  const [previewing, setPreviewing] = useState(false)
  /** 每条容器部署要回到的 tag，打开预览时用上一版填上，人可以改成仓库里任意已有版本。 */
  const [imageTags, setImageTags] = useState<Record<number, string>>({})
  /** Rebuild 且流水线有增量包步骤时出清单框 */
  const [needManifest, setNeedManifest] = useState(false)
  const [manifest, setManifest] = useState('')

  const label = action === 'rollback' ? t('risk.rollback') : t('risk.rebuild')
  const meta = META[action]
  const dockerItems = useMemo(
    () => (preview?.items || []).filter((it) => it.kind === 'docker-image' && it.image_repo),
    [preview],
  )
  const otherItems = useMemo(
    () => (preview?.items || []).filter((it) => it.kind !== 'docker-image'),
    [preview],
  )
  const missingTag = dockerItems.some((it) => !(imageTags[it.id] || '').trim())

  const openDialog = async () => {
    setOpen(true)
    setNeedManifest(false)
    setManifest('')
    if (action === 'rebuild') {
      await loadRebuildManifest()
      return
    }
    setPreview(null)
    setImageTags({})
    setPreviewing(true)
    try {
      const data = await get<RollbackPreview>(`/releases/${releaseId}/rollback-preview`)
      setPreview(data)
      const tags: Record<number, string> = {}
      for (const it of data.items || []) {
        if (it.kind === 'docker-image') tags[it.id] = (it.previous_tag || '').trim()
      }
      setImageTags(tags)
    } finally {
      setPreviewing(false)
    }
  }

  /**
   * Rebuild 打开时：看流水线有没有增量包步骤，有则带上上次的 DEPLOY_MANIFEST。
   */
  const loadRebuildManifest = async () => {
    try {
      if (pipeline && !pipelineNeedsManifest(pipeline)) {
        setNeedManifest(false)
        return
      }
      const rel = await get<Release & { run_params_json?: string }>(`/releases/${releaseId}`)
      const metaPipe = pipeline || (await get<Pipeline>(`/pipelines/${rel.pipeline_id}`))
      const show = pipelineNeedsManifest(metaPipe)
      setNeedManifest(show)
      if (!show) return
      let previous = ''
      try {
        const params = JSON.parse(rel.run_params_json || '{}') as Record<string, unknown>
        previous = String(params.DEPLOY_MANIFEST || '')
      } catch {
        previous = ''
      }
      const preset = previous && previous !== '**' ? previous : metaPipe.deploy_manifest_default || ''
      setManifest(preset)
    } catch {
      setNeedManifest(false)
    }
  }

  const closeDialog = () => {
    setOpen(false)
    setBypass(false)
    setReason('')
    setPreview(null)
    setImageTags({})
    setNeedManifest(false)
    setManifest('')
  }

  const blocked = action === 'rollback' && (previewing || preview?.can_rollback === false)
  const shownNo = preview?.build_number || buildNumber || releaseId
  const blockManifest =
    action === 'rebuild' &&
    manifestInputRequired(needManifest, pipeline?.deploy_manifest_default || '', manifest)

  const run = async (payload?: Record<string, unknown>) => {
    setLoading(true)
    try {
      const body = { ...(payload || {}) }
      if (action === 'rollback' && dockerItems.length) {
        body.image_tags = Object.fromEntries(
          dockerItems.map((it) => [it.id, (imageTags[it.id] || '').trim()]),
        )
      }
      if (action === 'rebuild' && needManifest) body.deploy_manifest = manifest
      const release = await post<Release>(`/releases/${releaseId}/${meta.path}`, body)
      const newNo = release?.build_number || release?.id
      if (release?.status === 'pending') {
        message.success(t('risk.submitted', { label, n: newNo }))
      } else if (payload?.emergency_bypass) {
        message.warning(t('risk.bypassed', { label, n: newNo }))
      } else {
        message.success(t('risk.started', { label, n: newNo }))
      }
      closeDialog()
      onDone?.(release)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <Button
        size={size}
        icon={meta.icon}
        disabled={disabled}
        title={title}
        loading={loading && !open}
        onClick={openDialog}
      >
        {text ?? label}
      </Button>

      <Modal
        title={
          <Space>
            {action === 'rollback'
              ? t('risk.undoTitle', { n: shownNo })
              : t('risk.confirmTitle', { label, n: shownNo })}
            {groupType ? <Tag color={envColor(groupType)}>{t('execBtn.envTag', { env: envLabel(groupType) })}</Tag> : null}
          </Space>
        }
        open={open}
        onCancel={closeDialog}
        confirmLoading={loading}
        width={action === 'rollback' ? 560 : needManifest ? 560 : 520}
        okText={
          bypass
            ? t('risk.emergencyOk', { label })
            : approvalRequired
              ? t('execBtn.submitApproval')
              : t('risk.confirmOk', { label })
        }
        okButtonProps={{
          danger: bypass || action === 'rollback',
          disabled: (bypass && !reason.trim()) || blocked || missingTag || blockManifest,
        }}
        onOk={() =>
          run(
            bypass && allowBypass
              ? { emergency_bypass: true, emergency_bypass_reason: reason.trim() }
              : undefined,
          )
        }
      >
        {action === 'rollback' && (
          <div style={{ marginBottom: 16 }}>
            {previewing ? (
              <Spin size="small" tip={t('risk.previewing')}>
                <div style={{ height: 40 }} />
              </Spin>
            ) : blocked ? (
              <Alert type="error" showIcon message={t('risk.cannot')} description={preview?.reason} />
            ) : preview ? (
              <>
                <div style={{ fontSize: 15, fontWeight: 600, color: '#111827', marginBottom: 14 }}>
                  {t('risk.restore')}
                </div>
                {dockerItems.map((it) => (
                  <DockerRollbackCard
                    key={it.id}
                    item={it}
                    tag={imageTags[it.id] || ''}
                    showTarget={dockerItems.length > 1}
                    onTagChange={(value) => setImageTags((prev) => ({ ...prev, [it.id]: value }))}
                  />
                ))}
                {otherItems.map((it) => (
                  <div
                    key={it.id}
                    style={{
                      marginBottom: 12,
                      padding: '10px 14px',
                      borderRadius: 10,
                      background: '#f8fafc',
                      color: '#475569',
                      fontSize: 13,
                    }}
                  >
                    {it.kind_label}
                    {it.target ? ` · ${it.target}` : ''}
                  </div>
                ))}
                {preview.warnings.map((w, i) => (
                  <Alert key={i} type="error" showIcon style={{ marginTop: 8 }} message={w} />
                ))}
              </>
            ) : null}
          </div>
        )}
        {!approvalRequired ? (
          action === 'rollback' ? null : (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: needManifest ? 16 : 0 }}
              message={t('risk.repush', { label })}
              description={t('risk.noApproval')}
            />
          )
        ) : null}
        <ReleaseGateFields
          needManifest={action === 'rebuild' && needManifest}
          manifest={manifest}
          onManifestChange={setManifest}
          needApproval={!!approvalRequired}
          allowBypass={!!allowBypass}
          bypass={bypass}
          onBypassChange={setBypass}
          reason={reason}
          onReasonChange={setReason}
          kind={action}
        />
      </Modal>
    </>
  )
}

/**
 * 容器回滚对照：目前版本只读，回滚后的 tag 可改，一眼能看出能回到任意已推送版本。
 */
function DockerRollbackCard({
  item,
  tag,
  showTarget,
  onTagChange,
}: {
  item: RollbackItem
  tag: string
  showTarget: boolean
  onTagChange: (value: string) => void
}) {
  const t = useT()
  const repo = item.image_repo || ''
  const current = item.current_image || (item.current_tag ? `${repo}:${item.current_tag}` : '')
  const sameAsCurrent = !!item.current_tag && tag.trim() === item.current_tag
  return (
    <div
      style={{
        marginBottom: 12,
        padding: '16px 16px 14px',
        borderRadius: 12,
        border: '1px solid #e8edf3',
        background: '#fff',
      }}
    >
      {showTarget && item.target ? (
        <div style={{ ...LABEL, marginBottom: 10 }}>{item.target}</div>
      ) : null}
      <div style={LABEL}>{t('risk.currentVer')}</div>
      <div style={{ ...REPO, marginBottom: 16 }}>{current || '—'}</div>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
        <div style={LABEL}>{t('risk.afterVer')}</div>
        <div style={{ fontSize: 12, color: '#1677ff' }}>{t('risk.changeTag')}</div>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={{ ...REPO, color: '#64748b' }}>{repo}:</span>
        <Input
          value={tag}
          onChange={(e) => onTagChange(e.target.value.replace(/\s/g, ''))}
          placeholder={item.previous_tag || t('risk.tagExample')}
          size="large"
          style={{
            width: 148,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            fontWeight: 700,
            fontSize: 18,
          }}
        />
      </div>
      {sameAsCurrent ? (
        <div style={{ marginTop: 8, fontSize: 12, color: '#d97706' }}>{t('risk.sameVer')}</div>
      ) : (
        <div style={{ marginTop: 8, fontSize: 12, color: '#94a3b8' }}>
          {t('risk.anyTag')}
        </div>
      )}
    </div>
  )
}
