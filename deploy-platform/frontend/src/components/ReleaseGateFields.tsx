import { Alert, Checkbox, Form, Input, Space } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { MANIFEST_PLACEHOLDER } from '@/utils/releaseGate'
import { useT } from '@/i18n'

type BypassKind = 'execute' | 'rebuild' | 'rollback'

/**
 * 执行 / 保存并执行 / Rebuild 共用的发布清单和应急跳审。
 * 有增量包步骤才出清单；需要审批时才出跳审。
 */
export default function ReleaseGateFields({
  needManifest,
  manifest,
  onManifestChange,
  needApproval,
  allowBypass,
  allowSelfApproval,
  bypass,
  onBypassChange,
  reason,
  onReasonChange,
  kind = 'execute',
}: {
  needManifest: boolean
  manifest: string
  onManifestChange: (value: string) => void
  needApproval: boolean
  allowBypass: boolean
  allowSelfApproval?: boolean
  bypass: boolean
  onBypassChange: (value: boolean) => void
  reason: string
  onReasonChange: (value: string) => void
  kind?: BypassKind
}) {
  const t = useT()
  const action = kind === 'rollback' ? t('pipe.actionRollback') : kind === 'rebuild' ? 'Rebuild' : t('pipe.actionRelease')
  return (
    <>
      {needManifest && (
        <Form layout="vertical" style={{ marginBottom: needApproval ? 16 : 0 }}>
          <Form.Item
            label={t("pipe.manifest")}
            extra={t("pipe.manifestExtra")}
          >
            <Input.TextArea
              rows={6}
              value={manifest}
              onChange={(e) => onManifestChange(e.target.value)}
              placeholder={MANIFEST_PLACEHOLDER}
            />
          </Form.Item>
        </Form>
      )}

      {needApproval && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={
            kind === 'execute'
              ? t('pipe.groupApprovalOn')
              : t('pipe.groupApprovalAction', { action })
          }
          description={
            kind === 'execute'
              ? allowSelfApproval
                ? t('pipe.pendingSelf')
                : t('pipe.pendingOthers')
              : t('pipe.pendingAction', { action })
          }
        />
      )}

      {!needApproval ? null : allowBypass ? (
        <>
          <Checkbox checked={bypass} onChange={(e) => onBypassChange(e.target.checked)}>
            <Space size={4}>
              <ThunderboltOutlined style={{ color: '#fa8c16' }} />
              {kind === 'execute'
                ? t('pipe.bypassRelease')
                : t('pipe.bypassRollback')}
            </Space>
          </Checkbox>
          {bypass && (
            <div style={{ marginTop: 12 }}>
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 8 }}
                message={t("pipe.bypassAudit")}
              />
              <Input.TextArea
                rows={3}
                value={reason}
                onChange={(e) => onReasonChange(e.target.value)}
                placeholder={
                  kind === 'execute'
                    ? t('pipe.bypassReasonRelease')
                    : t('pipe.bypassReasonAction', { action })
                }
              />
            </div>
          )}
        </>
      ) : (
        <Alert
          type="warning"
          showIcon
          message={t("pipe.bypassOff")}
          description={t("pipe.bypassOffHint")}
        />
      )}
    </>
  )
}
