import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Switch,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
  RocketOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { get, post, postR, put, del } from '@/api/client'
import DataTable from '@/components/DataTable'
import type { Pipeline, Project } from '@/api/types'
import { formatDateTime, useT } from '@/i18n'
import { releaseStatusMeta } from '@/utils/releaseStatus'

const { Text, Paragraph } = Typography

interface DeployRequest {
  id: number
  project_id: number
  project_name: string
  pipeline_id: number | null
  pipeline_name: string
  /** 这条线是不是 Windows/IIS 增量包，决定清单是否必填 */
  uses_deploy_manifest: boolean
  title: string
  repo: string
  source_ref: string
  /** 列表接口不带正文（清单可能上千行），只在详情里返回 */
  changelog: string
  manifest: string
  manifest_count: number
  status: string
  status_label: string
  release_id: number | null
  /** 关联发布的实时进度，单子处于「发布中」时用它说明卡在哪一步 */
  release_status: string | null
  release_status_label: string | null
  release_build_number: number | null
  release_version: string | null
  release_started_at: string | null
  release_finished_at: string | null
  reject_reason: string
  created_by_name: string
  created_at: string | null
  can_release: boolean
  can_edit: boolean
  can_delete: boolean
  business_summary: string
  impact_scope: string
  iteration_tag: string
  planned_window: string
  audience: string
  need_user_notice: boolean
}

const STATUS_COLOR: Record<string, string> = {
  draft: 'default',
  submitted: 'processing',
  releasing: 'processing',
  released: 'success',
  release_failed: 'error',
  rejected: 'error',
  closed: 'default',
}

/** 单子状态码 → 界面句子。不用后端 status_label，否则切语言仍是中文。 */
const REQUEST_STATUS_I18N: Record<string, string> = {
  draft: 'deploy.statusDraft',
  submitted: 'deploy.statusSubmitted',
  releasing: 'deploy.statusReleasing',
  released: 'deploy.statusReleased',
  release_failed: 'deploy.statusFailed',
  rejected: 'deploy.statusRejected',
  closed: 'deploy.statusClosed',
}

/** 单子还能不能发：草稿、待发布，以及发失败了要重发的 */
const RELEASABLE = ['draft', 'submitted', 'release_failed']
/** 还能改计划内容。发布中/已发布不能改，否则计划和实际发出去的对不上。 */
const EDITABLE = ['draft', 'submitted', 'rejected', 'release_failed', 'closed']

const MANIFEST_PLACEHOLDER = `bin/*.dll
Areas/
Views/
Content/o2o-theme.css
!bin/*.pdb`

/** 把单子状态码翻成当前语言；未收录的码原样显示，避免空白。 */
function requestStatusText(
  status: string,
  translate: (key: string) => string,
): string {
  return REQUEST_STATUS_I18N[status] ? translate(REQUEST_STATUS_I18N[status]) : status
}

/**
 * 发布计划：在权限范围内选定流水线，先把这次要发什么写下来，再核对后执行。
 *
 * Windows 增量线必须写文件清单；前端静态、容器、K8s 等不读清单的线可以不填。
 */
export default function DeployRequests() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const t = useT()
  const [createOpen, setCreateOpen] = useState(false)
  /** 正在改的计划。null 表示新建。 */
  const [editing, setEditing] = useState<DeployRequest | null>(null)
  // 只记 id，内容从列表里现取，这样发布中的进度会跟着列表一起刷新
  const [detailId, setDetailId] = useState<number | null>(null)
  const [projectFilter, setProjectFilter] = useState<number | undefined>()
  const [page, setPage] = useState(1)
  const [form] = Form.useForm()

  // 换项目后条数会变，停在原页码可能直接是空表
  useEffect(() => {
    setPage(1)
  }, [projectFilter])

  const { data: requests = [], isFetching } = useQuery({
    queryKey: ['deploy-requests', projectFilter],
    queryFn: () =>
      get<DeployRequest[]>('/deploy-requests', projectFilter ? { project_id: projectFilter } : {}),
    // 有单子正在发布时自动刷新，不用手点
    refetchInterval: (q) =>
      q.state.data?.some((r) => r.status === 'releasing') ? 5000 : false,
  })
  const { data: projects = [] } = useQuery({
    queryKey: ['projects'],
    queryFn: () => get<Project[]>('/projects'),
  })

  // 列表接口不带清单/日志正文（避免上百张单子把响应撑爆），详情抽屉必须再拉一次完整单子。
  // 之前抽屉直接拿列表数据，发布清单就变成空白。
  const { data: detail, isFetching: detailLoading } = useQuery({
    queryKey: ['deploy-request', detailId],
    queryFn: () => get<DeployRequest>(`/deploy-requests/${detailId}`),
    enabled: detailId != null,
    refetchInterval: (q) =>
      q.state.data?.status === 'releasing' ? 5000 : false,
  })

  // 新建/修改表单里选了项目才去查它的流水线。列出当前用户可见的全部线，不限于增量包。
  const selectedProject = Form.useWatch('project_id', form)
  const selectedPipelineId = Form.useWatch('pipeline_id', form)
  const { data: pipelines = [] } = useQuery({
    queryKey: ['pipelines', 'plan', selectedProject],
    queryFn: () => get<Pipeline[]>('/pipelines', { project_id: selectedProject }),
    enabled: !!selectedProject,
  })
  /** 当前选中的流水线是不是 Windows 增量：只有这类才强制填清单。 */
  const selectedPipe = pipelines.find((p) => p.id === selectedPipelineId)
  const needsManifest = !!(
    selectedPipe?.uses_pack_incremental || selectedPipe?.uses_deploy_manifest
  )

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['deploy-requests'] })
    queryClient.invalidateQueries({ queryKey: ['deploy-request'] })
  }

  const createMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      editing
        ? put(`/deploy-requests/${editing.id}`, values)
        : post('/deploy-requests', values),
    onSuccess: () => {
      message.success(editing ? t('deploy.saved') : t('deploy.submitted'))
      setCreateOpen(false)
      setEditing(null)
      form.resetFields()
      refresh()
    },
  })
  const deleteMutation = useMutation({
    mutationFn: (id: number) => del(`/deploy-requests/${id}`),
    onSuccess: () => {
      message.success(t('deploy.deleted'))
      setDetailId(null)
      refresh()
    },
  })

  const releaseMutation = useMutation({
    mutationFn: (req: DeployRequest) =>
      postR<{ release_id: number; status?: string; error?: string }>(`/deploy-requests/${req.id}/release`),
    onSuccess: (res, req) => {
      const data = res.data
      if (res.message && res.message !== 'ok' && (data?.status === 'failed' || data?.error)) {
        message.error(res.message, 8)
      } else {
        message.success(res.message && res.message !== 'ok' ? res.message : t('deploy.started'))
      }
      setDetailId(null)
      refresh()
      if (req.pipeline_id && data?.release_id) {
        navigate(`/executions/${req.pipeline_id}/${data.release_id}`)
      }
    },
  })

  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason: string }) =>
      post(`/deploy-requests/${id}/reject`, { reason }),
    onSuccess: () => {
      message.success(t('deploy.rejected'))
      setDetailId(null)
      refresh()
    },
  })

  const reopenMutation = useMutation({
    mutationFn: (id: number) => post(`/deploy-requests/${id}/reopen`),
    onSuccess: () => {
      message.success(t('deploy.resubmitted'))
      setDetailId(null)
      refresh()
    },
  })

  const confirmRelease = async (req: DeployRequest) => {
    // 列表里没有清单正文，增量线确认前单独取一次详情来预览
    let manifest = req.manifest
    const showManifest = req.uses_deploy_manifest
    if (showManifest && !manifest) {
      try {
        manifest = (await get<DeployRequest>(`/deploy-requests/${req.id}`)).manifest
      } catch {
        manifest = t('deploy.manifestLoadFail')
      }
    }
    Modal.confirm({
      title: t('deploy.confirmReleaseTitle', { title: req.title }),
      width: 560,
      content: (
        <div>
          <Paragraph style={{ marginBottom: 8 }}>
            {showManifest
              ? t('deploy.confirmReleaseBody', { pipeline: req.pipeline_name })
              : t('deploy.confirmReleaseBodyPlain', { pipeline: req.pipeline_name })}
          </Paragraph>
          {showManifest && (
            <pre
              style={{
                background: '#fafafa',
                padding: 8,
                maxHeight: 180,
                overflow: 'auto',
                fontSize: 12,
              }}
            >
              {manifest}
            </pre>
          )}
        </div>
      ),
      okText: t('deploy.confirmReleaseOk'),
      cancelText: t('common.cancel'),
      onOk: () => releaseMutation.mutateAsync(req),
    })
  }

  /** 打开修改弹窗。列表没有清单正文，必须再拉详情。 */
  const openEdit = async (row: DeployRequest) => {
    const full = await get<DeployRequest>(`/deploy-requests/${row.id}`)
    setEditing(full)
    form.setFieldsValue(full)
    setCreateOpen(true)
  }

  const confirmReject = (req: DeployRequest) => {
    let reason = ''
    Modal.confirm({
      title: t('deploy.rejectTitle', { title: req.title }),
      content: (
        <Input.TextArea
          rows={3}
          placeholder={t('deploy.rejectPlaceholder')}
          onChange={(e) => {
            reason = e.target.value
          }}
        />
      ),
      okText: t('deploy.rejectOk'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: () => rejectMutation.mutateAsync({ id: req.id, reason }),
    })
  }

  const columns = [
    {
        title: t('deploy.colTitle'),
      dataIndex: 'title',
      render: (v: string, r: DeployRequest) => (
        <a onClick={() => setDetailId(r.id)}>{v}</a>
      ),
    },
      { title: t('deploy.colProject'), dataIndex: 'project_name', width: 140 },
    {
        title: t('deploy.colPipeline'),
      dataIndex: 'pipeline_name',
      width: 160,
        render: (v: string) => v || <Text type="secondary">{t('deploy.notSelected')}</Text>,
    },
    {
      title: t('deploy.colFiles'),
      dataIndex: 'manifest_count',
      width: 90,
      render: (v: number) => v ?? 0,
    },
      { title: t('deploy.colSubmitter'), dataIndex: 'created_by_name', width: 110 },
    {
        title: t('common.status'),
      dataIndex: 'status',
      width: 130,
      render: (v: string, r: DeployRequest) => (
        <>
            <Tag color={STATUS_COLOR[v] || 'default'}>{requestStatusText(v, t)}</Tag>
            {v === 'releasing' && r.release_status && (
              <div style={{ fontSize: 11, color: '#999', marginTop: 2 }}>
                {releaseStatusMeta(r.release_status).text}
              </div>
            )}
        </>
      ),
    },
    {
        title: t('deploy.colTime'),
        dataIndex: 'created_at',
        width: 160,
        render: (v: string | null) => formatDateTime(v) || '-',
    },
    {
        title: t('common.action'),
        key: 'actions',
      width: 320,
      render: (_: unknown, r: DeployRequest) => (
        <Space>
          {r.can_release && RELEASABLE.includes(r.status) && (
            <Button
              size="small"
              type="primary"
              icon={<RocketOutlined />}
              onClick={() => confirmRelease(r)}
            >
                {r.status === 'release_failed' ? t('deploy.rerelease') : t('deploy.release')}
            </Button>
          )}
          {r.can_edit && EDITABLE.includes(r.status) && (
            <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>
              {t('common.edit')}
            </Button>
          )}
          <Button
            size="small"
            icon={<FileTextOutlined />}
            onClick={() => setDetailId(r.id)}
          >
              {t('deploy.detail')}
          </Button>
          {r.can_delete && (
            <Popconfirm
              title={t('deploy.deleteConfirm')}
              onConfirm={() => deleteMutation.mutate(r.id)}
            >
              <Button size="small" danger icon={<DeleteOutlined />} loading={deleteMutation.isPending}>
                {t('common.delete')}
              </Button>
            </Popconfirm>
          )}
          {r.release_id && r.pipeline_id && (
            <Button
              size="small"
              onClick={() => navigate(`/executions/${r.pipeline_id}/${r.release_id}`)}
            >
                {t('deploy.execDetail')}
            </Button>
          )}
        </Space>
      ),
    },
  ]

  const projectOptions = useMemo(
    () => projects.map((p) => ({ value: p.id, label: p.name })),
    [projects],
  )

  return (
    <div>
      <Card
        title={t("deploy.title")}
        extra={
          <Space>
            <Select
              allowClear
              placeholder={t("deploy.filterProject")}
              style={{ width: 180 }}
              options={projectOptions}
              value={projectFilter}
              onChange={setProjectFilter}
            />
            <Button icon={<ReloadOutlined />} onClick={refresh} loading={isFetching}>
              {t("common.refresh")}
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setEditing(null)
                form.resetFields()
                setCreateOpen(true)
              }}
            >
              {t("deploy.create")}
            </Button>
          </Space>
        }
      >
        <Alert
          type="info"
          showIcon
          message={t("deploy.alertTitle")}
          description={t("deploy.alertDesc")}
          style={{ marginBottom: 16 }}
        />
        <DataTable
          chromeKey="deploy-requests"
          rowKey="id"
          columns={columns}
          dataSource={requests}
          pagination={{ current: page, onChange: setPage }}
          locale={{ emptyText: t("deploy.empty") }}
        />
      </Card>

      <Modal
        title={editing ? t("deploy.editTitle") : t("deploy.createTitle")}
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false)
          setEditing(null)
          form.resetFields()
        }}
        onOk={() => form.validateFields().then((v) => createMutation.mutateAsync(v))}
        confirmLoading={createMutation.isPending}
        okText={editing ? t("common.save") : t("common.submit")}
        cancelText={t("common.cancel")}
        width={720}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item label={t("deploy.colProject")} name="project_id" rules={[{ required: true, message: t("deploy.pickProjectRequired") }]}>
            <Select
              placeholder={t("deploy.pickProject")}
              options={projectOptions}
              showSearch
              optionFilterProp="label"
              onChange={() => form.setFieldValue('pipeline_id', undefined)}
            />
          </Form.Item>

          <Form.Item
            label={t("deploy.pipeline")}
            name="pipeline_id"
            rules={[{ required: true, message: t("deploy.pickPipelineRequired") }]}
            extra={
              selectedProject && pipelines.length === 0
                ? t("deploy.pipelineExtraNone")
                : t("deploy.pipelineExtra")
            }
          >
            <Select
              placeholder={selectedProject ? t("deploy.pickPipeline") : t("deploy.pickProjectFirst")}
              disabled={!selectedProject}
              options={pipelines.map((p) => ({
                value: p.id,
                label:
                  p.uses_pack_incremental || p.uses_deploy_manifest
                    ? `${p.name} · ${t('deploy.incrementalTag')}`
                    : p.name,
              }))}
              showSearch
              optionFilterProp="label"
              notFoundContent={t("deploy.pipelineNotFound")}
            />
          </Form.Item>

          <Form.Item label={t("deploy.colTitle")} name="title" rules={[{ required: true, message: t("deploy.titleRequired") }]}>
            <Input placeholder={t("deploy.titlePlaceholder")} />
          </Form.Item>

          <Form.Item label={t("deploy.sourceRef")} name="source_ref" extra={t("deploy.sourceRefExtra")}>
            <Input placeholder={t("deploy.sourceRefPlaceholder")} />
          </Form.Item>

          <Form.Item
            label={t("deploy.manifest")}
            name="manifest"
            rules={
              needsManifest
                ? [{ required: true, message: t("deploy.manifestRequired") }]
                : []
            }
            extra={needsManifest ? t("deploy.manifestExtra") : t("deploy.manifestSkipExtra")}
          >
            <Input.TextArea
              rows={7}
              placeholder={needsManifest ? MANIFEST_PLACEHOLDER : t("deploy.manifestOptionalPlaceholder")}
            />
          </Form.Item>

          <Form.Item label={t("deploy.changelog")} name="changelog" extra={t("deploy.changelogExtra")}>
            <Input.TextArea rows={4} placeholder={t("deploy.changelogPlaceholder")} />
          </Form.Item>

          <Form.Item
            label={t("deploy.business")}
            name="business_summary"
            extra={t("deploy.businessExtra")}
          >
            <Input.TextArea rows={3} placeholder={t("deploy.businessPlaceholder")} />
          </Form.Item>
          <Form.Item label={t("deploy.impact")} name="impact_scope">
            <Input placeholder={t("deploy.impactPlaceholder")} />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t("deploy.iteration")} name="iteration_tag" style={{ flex: 1 }}>
              <Input placeholder={t("deploy.iterationPlaceholder")} />
            </Form.Item>
            <Form.Item label={t("deploy.window")} name="planned_window" style={{ flex: 1 }}>
              <Input placeholder={t("deploy.windowPlaceholder")} />
            </Form.Item>
          </div>
          <Form.Item label={t("deploy.audienceNotice")} style={{ marginBottom: 0 }}>
            <div className="rp-field-row">
              <Form.Item name="audience" style={{ flex: 1, marginBottom: 0 }}>
                <Input placeholder={t("deploy.audiencePlaceholder")} />
              </Form.Item>
              <Form.Item name="need_user_notice" valuePropName="checked" style={{ marginBottom: 0 }}>
                <Switch checkedChildren={t("deploy.noticeOn")} unCheckedChildren={t("deploy.noticeOff")} />
              </Form.Item>
            </div>
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={detail?.title || t('deploy.detail')}
        open={detailId != null}
        onClose={() => setDetailId(null)}
        width={640}
        extra={
          detail && (
            <Space>
              {detail.can_edit && EDITABLE.includes(detail.status) && (
                <Button
                  icon={<EditOutlined />}
                  onClick={() => {
                    setDetailId(null)
                    openEdit(detail)
                  }}
                >
                  {t('common.edit')}
                </Button>
              )}
              {detail.can_delete && (
                <Popconfirm
                  title={t('deploy.deleteConfirm')}
                  onConfirm={() => deleteMutation.mutate(detail.id)}
                >
                  <Button danger icon={<DeleteOutlined />} loading={deleteMutation.isPending}>
                    {t('common.delete')}
                  </Button>
                </Popconfirm>
              )}
              {detail.status === 'rejected' && detail.can_edit && (
                <Button
                  loading={reopenMutation.isPending}
                  onClick={() => reopenMutation.mutateAsync(detail.id)}
                >
                  {t('deploy.reopen')}
                </Button>
              )}
              {detail.can_release && RELEASABLE.includes(detail.status) && (
                <>
                  {detail.status !== 'release_failed' && (
                    <Button danger loading={rejectMutation.isPending} onClick={() => confirmReject(detail)}>
                      {t('deploy.rejectOk')}
                    </Button>
                  )}
                  {/* 连点会重复触发发布，同一个发布单能跑出好几条发布记录 */}
                  <Button
                    type="primary"
                    icon={<RocketOutlined />}
                    loading={releaseMutation.isPending}
                    onClick={() => confirmRelease(detail)}
                  >
                    {detail.status === 'release_failed' ? t('deploy.rerelease') : t('deploy.release')}
                  </Button>
                </>
              )}
            </Space>
          )
        }
      >
        {detailLoading && !detail ? (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
          </div>
        ) : detail ? (
          <>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t("common.status")}>
                <Tag color={STATUS_COLOR[detail.status] || 'default'}>{requestStatusText(detail.status, t)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("deploy.colProject")}>{detail.project_name}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.colPipeline")}>
                {detail.pipeline_name || t('deploy.notSelected')}
              </Descriptions.Item>
              <Descriptions.Item label={t("deploy.sourceVersion")}>{detail.source_ref || t("deploy.defaultBranch")}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.colSubmitter")}>{detail.created_by_name}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.iteration")}>{detail.iteration_tag || "—"}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.window")}>{detail.planned_window || "—"}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.impact")}>{detail.impact_scope || "—"}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.audience")}>{detail.audience || "—"}</Descriptions.Item>
              <Descriptions.Item label={t("deploy.notifyUser")}>
                {detail.need_user_notice ? t('deploy.needNotice') : t('deploy.noNeedNotice')}
              </Descriptions.Item>
              {detail.reject_reason && (
                <Descriptions.Item label={t("deploy.rejectReason")}>
                  <Text type="danger">{detail.reject_reason}</Text>
                </Descriptions.Item>
              )}
            </Descriptions>

            {detail.release_id && (
              <Descriptions
                column={1}
                size="small"
                bordered
                title={t("deploy.thisRelease")}
                style={{ marginBottom: 16 }}
                extra={
                  detail.pipeline_id && (
                    <Button
                      size="small"
                      onClick={() =>
                        navigate(`/executions/${detail.pipeline_id}/${detail.release_id}`)
                      }
                    >
                      {t('deploy.viewSteps')}
                    </Button>
                  )
                }
              >
                <Descriptions.Item label={t("deploy.buildNo")}>
                  #{detail.release_build_number ?? detail.release_id}
                  {detail.release_version && (
                    <Text type="secondary" style={{ marginLeft: 8 }}>
                      {detail.release_version}
                    </Text>
                  )}
                </Descriptions.Item>
                <Descriptions.Item label={t("deploy.execStatus")}>
                  {detail.release_status ? releaseStatusMeta(detail.release_status).text : '—'}
                </Descriptions.Item>
                <Descriptions.Item label={t("deploy.startedAt")}>
                  {formatDateTime(detail.release_started_at) || t('deploy.notStarted')}
                </Descriptions.Item>
                <Descriptions.Item label={t("deploy.finishedAt")}>
                  {formatDateTime(detail.release_finished_at) || t('deploy.inProgress')}
                </Descriptions.Item>
              </Descriptions>
            )}

            {detail.uses_deploy_manifest || detail.manifest ? (
              <>
            <Text strong>{t('deploy.manifest')}</Text>
            <pre
              style={{
                background: '#fafafa',
                padding: 12,
                borderRadius: 4,
                fontSize: 12,
                marginTop: 8,
                whiteSpace: 'pre-wrap',
              }}
            >
              {detail.manifest || t('deploy.notFilled')}
            </pre>
              </>
            ) : null}

            <Text strong>{t('deploy.business')}</Text>
            <pre
              style={{
                background: '#fafafa',
                padding: 12,
                borderRadius: 4,
                fontSize: 12,
                marginTop: 8,
                whiteSpace: 'pre-wrap',
              }}
            >
              {detail.business_summary || t('deploy.notFilled')}
            </pre>

            <Text strong>{t('deploy.changelog')}</Text>
            <pre
              style={{
                background: '#fafafa',
                padding: 12,
                borderRadius: 4,
                fontSize: 12,
                marginTop: 8,
                whiteSpace: 'pre-wrap',
              }}
            >
              {detail.changelog || t('deploy.notFilled')}
            </pre>
          </>
        ) : null}
      </Drawer>
    </div>
  )
}
