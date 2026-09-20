import { useState } from 'react'
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { del, get, post } from '@/api/client'
import type { LintFinding, Pipeline, PluginDraft } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useT } from '@/i18n'
import { releaseStatusMeta } from '@/utils/releaseStatus'

const { Text } = Typography

interface Props {
  draftId: number | null
  onClose: () => void
}

/**
 * 插件草稿审阅：看完整源码和体检结果，可挑一条流水线试跑，最后发布或驳回。
 * 发布只是让它进插件仓库（未安装状态），要流水线能用还得再点安装。
 */
export default function PluginDraftDrawer({ draftId, onClose }: Props) {
  const t = useT()
  const queryClient = useQueryClient()
  const isAdmin = !!useAuthStore((s) => s.user?.is_admin)
  const navigate = useNavigate()
  const [trialOpen, setTrialOpen] = useState(false)
  const [trialForm] = Form.useForm()
  const [acknowledged, setAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)

  /** 体检级别：颜色固定，句子按当前语言现取。 */
  const levelMeta = (level: string) => {
    if (level === 'error') return { color: 'error', text: t('draft.error') }
    if (level === 'high') return { color: 'volcano', text: t('draft.high') }
    if (level === 'warn') return { color: 'gold', text: t('draft.warn') }
    return { color: 'default', text: level }
  }

  const { data: draft, isLoading } = useQuery({
    queryKey: ['plugin-draft', draftId],
    queryFn: () => get<PluginDraft>(`/store/plugin-drafts/${draftId}`),
    enabled: !!draftId,
  })

  const { data: pipelines = [] } = useQuery({
    queryKey: ['pipelines'],
    queryFn: () => get<Pipeline[]>('/pipelines'),
    enabled: trialOpen,
  })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['plugin-drafts'] })
    queryClient.invalidateQueries({ queryKey: ['plugin-draft', draftId] })
    queryClient.invalidateQueries({ queryKey: ['plugins'] })
  }

  const findings: LintFinding[] = draft?.lint?.findings || []
  const highCount = draft?.lint?.high_count || 0
  const errorCount = draft?.lint?.error_count || 0
  const pending = draft?.status === 'pending'

  const handleTrial = async () => {
    const values = await trialForm.validateFields()
    setBusy(true)
    try {
      const release = await post<{ id: number; pipeline_id: number }>(
        `/store/plugin-drafts/${draftId}/trial`,
        {
          pipeline_id: values.pipeline_id,
          agent_tag: values.agent_tag || 'linux',
          params: values.params ? JSON.parse(values.params) : {},
        },
      )
      setTrialOpen(false)
      refresh()
      message.success(t('draft.trialSubmitted'))
      navigate(`/executions/${release.pipeline_id}/${release.id}`)
    } finally {
      setBusy(false)
    }
  }

  const handlePublish = async () => {
    setBusy(true)
    try {
      await post(`/store/plugin-drafts/${draftId}/publish`, { acknowledge_high: acknowledged })
      message.success(t('draft.published'))
      refresh()
      onClose()
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!draftId) return
    setBusy(true)
    try {
      await del(`/store/plugin-drafts/${draftId}`)
      message.success(t('draft.deletedDraft'))
      refresh()
      onClose()
    } finally {
      setBusy(false)
    }
  }

  const handleReject = () => {
    let comment = ''
    Modal.confirm({
      title: t('draft.rejectTitle'),
      content: (
        <Input.TextArea
          rows={3}
          placeholder={t('draft.rejectPh')}
          onChange={(e) => {
            comment = e.target.value
          }}
        />
      ),
      okText: t('deploy.rejectOk'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        if (!comment.trim()) {
          message.error(t('draft.needRejectReason'))
          throw new Error('no comment')
        }
        await post(`/store/plugin-drafts/${draftId}/reject`, { comment: comment.trim() })
        message.success(t('deploy.statusRejected'))
        refresh()
        onClose()
      },
    })
  }

  return (
    <>
      <Drawer
        title={
          draft
            ? t('draft.titleN', { id: draft.id, name: draft.display_name || draft.name })
            : t('draft.title')
        }
        open={!!draftId}
        onClose={onClose}
        width={860}
        extra={
          <Space>
            {pending ? (
              <>
                <Button onClick={() => setTrialOpen(true)}>{t('draft.trial')}</Button>
                {isAdmin ? (
                  <>
                    <Button danger onClick={handleReject}>
                      {t('deploy.rejectOk')}
                    </Button>
                    <Button
                      type="primary"
                      loading={busy}
                      disabled={errorCount > 0 || (highCount > 0 && !acknowledged)}
                      onClick={handlePublish}
                    >
                      {t('draft.publishRepo')}
                    </Button>
                  </>
                ) : null}
              </>
            ) : null}
            <Popconfirm title={t('draft.deleteForever')} onConfirm={handleDelete}>
              <Button danger loading={busy}>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        {isLoading || !draft ? (
          <div style={{ textAlign: 'center', padding: 60 }}>
            <Spin />
          </div>
        ) : (
          <>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t('draft.agentWarn')}
              description={t('draft.agentWarnDesc')}
            />

            <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('draft.ident')}>
                <code>{draft.name}</code>
              </Descriptions.Item>
              <Descriptions.Item label={t('common.version')}>{draft.version}</Descriptions.Item>
              <Descriptions.Item label={t('draft.category')}>{draft.category}</Descriptions.Item>
              <Descriptions.Item label={t('draft.language')}>{draft.language}</Descriptions.Item>
              <Descriptions.Item label={t('draft.entry')} span={2}>
                <code>{draft.entrypoint}</code>
              </Descriptions.Item>
              <Descriptions.Item label={t('draft.source')}>
                {draft.source === 'ai' ? <Tag color="purple">{t('draft.aiDraft')}</Tag> : <Tag>{t('draft.manual')}</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label={t('draft.trialLabel')}>
                {draft.trial_release_id ? (
                  <Tag color={draft.trial_status === 'success' ? 'green' : draft.trial_status === 'failed' ? 'red' : 'default'}>
                    {t('draft.trialRel', {
                      id: draft.trial_release_id,
                      status: draft.trial_status ? releaseStatusMeta(draft.trial_status).text : t('common.unknown'),
                    })}
                  </Tag>
                ) : (
                  <Text type="secondary">{t('draft.notTried')}</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t('draft.intent')} span={2}>
                {draft.intent || <Text type="secondary">—</Text>}
              </Descriptions.Item>
            </Descriptions>

            <Tabs
              size="small"
              items={[
                {
                  key: 'lint',
                  label: t('draft.lintN', { n: findings.length }),
                  children: findings.length === 0 ? (
                    <Empty description={t('draft.noIssues')} />
                  ) : (
                    <Space direction="vertical" style={{ width: '100%' }}>
                      {findings.map((f, idx) => {
                        const meta = levelMeta(f.level)
                        return (
                          <Alert
                            key={`${f.code}-${idx}`}
                            type={f.level === 'error' ? 'error' : f.level === 'high' ? 'warning' : 'info'}
                            showIcon
                            message={
                              <Space size={6}>
                                <Tag color={meta.color}>{meta.text}</Tag>
                                {f.message}
                              </Space>
                            }
                            description={f.where ? <code>{f.where}</code> : undefined}
                          />
                        )
                      })}
                      {highCount > 0 && pending && (
                        <Checkbox
                          checked={acknowledged}
                          onChange={(e) => setAcknowledged(e.target.checked)}
                        >
                          {t('draft.ackHigh', { n: highCount })}
                        </Checkbox>
                      )}
                    </Space>
                  ),
                },
                {
                  key: 'files',
                  label: t('draft.filesN', { n: Object.keys(draft.files || {}).length }),
                  children: (
                    <Tabs
                      tabPosition="left"
                      size="small"
                      items={[
                        {
                          key: '__task_json__',
                          label: 'task.json',
                          children: <CodeBlock content={draft.task_json || ''} />,
                        },
                        ...Object.entries(draft.files || {}).map(([path, content]) => ({
                          key: path,
                          label: path,
                          children: <CodeBlock content={content} />,
                        })),
                      ]}
                    />
                  ),
                },
              ]}
            />

            {draft.status !== 'pending' && (
              <Alert
                style={{ marginTop: 16 }}
                type={draft.status === 'published' ? 'success' : 'error'}
                showIcon
                message={draft.status === 'published' ? t('draft.publishedAlert') : t('draft.rejectedAlert')}
                description={draft.review_comment || undefined}
              />
            )}
          </>
        )}
      </Drawer>

      <Modal
        title={t('draft.trial')}
        open={trialOpen}
        onCancel={() => setTrialOpen(false)}
        onOk={handleTrial}
        confirmLoading={busy}
        okText={t('draft.startTrial')}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t('draft.trialAlert')}
          description={t('draft.trialDesc')}
        />
        <Form form={trialForm} layout="vertical" initialValues={{ agent_tag: 'linux', params: '{}' }}>
          <Form.Item
            name="pipeline_id"
            label={t('draft.trialPipe')}
            rules={[{ required: true, message: t('draft.pickPipe') }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder={t('draft.trialPipePh')}
              options={pipelines.map((p) => ({
                value: p.id,
                label: t('draft.pipeOpt', { name: p.name, id: p.id }),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="agent_tag"
            label={t('draft.agentTag')}
            tooltip={t('draft.agentTagTip')}
          >
            <Input placeholder="linux" />
          </Form.Item>
          <Form.Item
            name="params"
            label={t('draft.paramsJson')}
            rules={[
              {
                validator: (_, v) => {
                  if (!v || !v.trim()) return Promise.resolve()
                  try {
                    JSON.parse(v)
                    return Promise.resolve()
                  } catch {
                    return Promise.reject(new Error(t('draft.badJson')))
                  }
                },
              },
            ]}
          >
            <Input.TextArea rows={4} style={{ fontFamily: 'monospace' }} placeholder='{"message": "hello"}' />
          </Form.Item>
        </Form>
      </Modal>
    </>
  )
}

function CodeBlock({ content }: { content: string }) {
  return (
    <pre
      style={{
        background: '#1e1e1e',
        color: '#d4d4d4',
        padding: 12,
        borderRadius: 4,
        fontSize: 12,
        maxHeight: 480,
        overflow: 'auto',
        margin: 0,
      }}
    >
      {content}
    </pre>
  )
}
