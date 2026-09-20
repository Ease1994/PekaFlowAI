import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Calendar,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Tag,
  Typography,
  message,
} from 'antd'
import DataTable from '@/components/DataTable'
import {
  AuditOutlined,
  CheckOutlined,
  CloseOutlined,
  ReloadOutlined,
  ScheduleOutlined,
  SoundOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import type { Dayjs } from 'dayjs'
import { get, post, postR } from '@/api/client'
import { envColor, envLabel, groupDisplayName } from '@/env'
import { useAuthStore } from '@/stores/auth'
import { useT } from '@/i18n'
import { releaseStatusMeta } from '@/utils/releaseStatus'

const { Text, Paragraph } = Typography

interface Blocker {
  kind: string
  label: string
  owner_ids: number[]
}

interface ReleaseCard {
  id: number
  pipeline_id: number
  pipeline_name: string
  project_id: number | null
  project_name: string
  group_name: string
  group_type: string
  build_number: number
  version: string
  status: string
  operator_name: string
  business_summary: string
  impact_scope: string
  iteration_tag: string
  planned_window: string
  audience: string
  need_user_notice: boolean
  announced_at: string
  blocker: Blocker
  created_at: string
}

interface PlannedItem {
  id: number
  title: string
  project_name: string
  pipeline_name: string
  business_summary: string
  iteration_tag: string
  planned_window: string
  status: string
}

interface Stability {
  project_id: number
  project_name: string
  days: number
  total: number
  success: number
  failed: number
  rolled_back: number
  success_rate: number
  plain: string
}

interface Decision {
  id: number
  status: string
  reviewer: string
  release: ReleaseCard
}

interface Workbench {
  projects: { id: number; name: string; pm_enabled: boolean; is_pm: boolean }[]
  in_progress: ReleaseCard[]
  planned: PlannedItem[]
  recent: ReleaseCard[]
  calendar: { id: string; date: string; title: string; env: string; status: string; window: string; kind: string; release_id?: number; pipeline_id?: number }[]
  stability: Stability[]
  pending_mine: Decision[]
  is_pm: boolean
}

/** 工作台状态标签。pending 同时覆盖待确认和待审批；其余码走 status.*。 */
function StatusTag({ status }: { status: string }) {
  const t = useT()
  if (status === 'pending') return <Tag color="orange">{t('pm.pending')}</Tag>
  const s = releaseStatusMeta(status)
  return <Tag color={s.color}>{s.text}</Tag>
}

type CalItem = NonNullable<Workbench['calendar']>[number]

function dayMarkColor(items: CalItem[]): string | undefined {
  if (!items.length) return undefined
  const statuses = items.map((i) => i.status)
  if (statuses.some((s) => s === 'failed' || s === 'rejected')) return '#ff4d4f'
  if (statuses.some((s) => ['running', 'queued', 'pending', 'assigned', 'rolling_back'].includes(s))) {
    return '#fa8c16'
  }
  if (statuses.some((s) => s === 'success' || s === 'rolled_back')) return '#52c41a'
  return '#1677ff'
}

export default function PmWorkbench() {
  const t = useT()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const user = useAuthStore((s) => s.user)
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') ? Number(params.get('project')) : undefined
  const [decide, setDecide] = useState<{ id: number; approved: boolean } | null>(null)
  const [comment, setComment] = useState('')
  const [announce, setAnnounce] = useState<ReleaseCard | null>(null)
  const [announceText, setAnnounceText] = useState('')

  const { data, isFetching } = useQuery({
    queryKey: ['pm-workbench', projectId],
    queryFn: () => get<Workbench>('/pm/workbench', projectId ? { project_id: projectId } : undefined),
    refetchInterval: 15000,
  })

  const decideMut = useMutation({
    mutationFn: ({ id, approved, comment: c }: { id: number; approved: boolean; comment: string }) =>
      postR(`/pm/decisions/${id}/decide`, { approved, comment: c }),
    onSuccess: (res, vars) => {
      message.success(res.message || (vars.approved ? t('pm.decided') : t('pm.rejectedMsg')))
      setDecide(null)
      setComment('')
      qc.invalidateQueries({ queryKey: ['pm-workbench'] })
      qc.invalidateQueries({ queryKey: ['pm-decisions'] })
    },
  })

  const announceMut = useMutation({
    mutationFn: ({ id, text }: { id: number; text: string }) =>
      post(`/pm/releases/${id}/announce`, { text }),
    onSuccess: () => {
      message.success(t('pm.announced'))
      setAnnounce(null)
      qc.invalidateQueries({ queryKey: ['pm-workbench'] })
    },
  })

  const openAnnounce = async (row: ReleaseCard) => {
    const draft = await get<{ text: string }>(`/pm/releases/${row.id}/announcement`)
    setAnnounce(row)
    setAnnounceText(draft.text)
  }

  const calMap = useMemo(() => {
    const m = new Map<string, NonNullable<Workbench['calendar']>>()
    for (const item of data?.calendar || []) {
      if (!item.date) continue
      const list = m.get(item.date) || []
      list.push(item)
      m.set(item.date, list)
    }
    return m
  }, [data])

  const recentColumns = [
    {
      title: t('pm.colPipe'),
      render: (_: unknown, r: ReleaseCard) => (
        <a onClick={() => navigate(`/executions/${r.pipeline_id}/${r.id}`)}>
          {r.pipeline_name} #{r.build_number}
        </a>
      ),
    },
    {
      title: t('common.environment'),
      width: 80,
      render: (_: unknown, r: ReleaseCard) => (
        <Tag color={envColor(r.group_type)}>{groupDisplayName({ name: r.group_name, type: r.group_type })}</Tag>
      ),
    },
    {
      title: t('common.status'),
      width: 88,
      render: (_: unknown, r: ReleaseCard) => <StatusTag status={r.status} />,
    },
    {
      title: '',
      width: 80,
      render: (_: unknown, r: ReleaseCard) =>
        (data?.is_pm || user?.is_admin) && ['success', 'rolled_back', 'failed'].includes(r.status) ? (
          <Button size="small" icon={<SoundOutlined />} onClick={() => openAnnounce(r)}>
            {t('pm.announce')}
          </Button>
        ) : null,
    },
  ]

  const columns = [
    {
      title: t('pm.colPipe'),
      render: (_: unknown, r: ReleaseCard) => (
        <div>
          <div>
            <a onClick={() => navigate(`/executions/${r.pipeline_id}/${r.id}`)}>
              {r.project_name} / {r.pipeline_name} #{r.build_number}
            </a>
          </div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.operator_name} · {r.iteration_tag || t('pm.noIteration')}
          </Text>
        </div>
      ),
    },
    {
      title: t('common.environment'),
      width: 90,
      render: (_: unknown, r: ReleaseCard) => (
        <Tag color={envColor(r.group_type)}>{groupDisplayName({ name: r.group_name, type: r.group_type })}</Tag>
      ),
    },
    {
      title: t('pm.colSummary'),
      render: (_: unknown, r: ReleaseCard) => r.business_summary || <Text type="secondary">{t('pm.notFilled')}</Text>,
    },
    {
      title: t('pm.blockedAt'),
      width: 220,
      render: (_: unknown, r: ReleaseCard) => r.blocker?.label || '—',
    },
    {
      title: t('common.status'),
      width: 120,
      render: (_: unknown, r: ReleaseCard) => <StatusTag status={r.status} />,
    },
    {
      title: t('pm.colWindow'),
      width: 140,
      dataIndex: 'planned_window',
    },
    {
      title: '',
      width: 140,
      render: (_: unknown, r: ReleaseCard) =>
        (data?.is_pm || user?.is_admin) && ['success', 'rolled_back', 'failed'].includes(r.status) ? (
          <Button size="small" icon={<SoundOutlined />} onClick={() => openAnnounce(r)}>
            {t('pm.announce')}
          </Button>
        ) : null,
    },
  ]

  return (
    <div>
      <Card
        title={
          <Space>
            <ScheduleOutlined />
            {t('pm.title')}
          </Space>
        }
        extra={
          <Space>
            <Select
              allowClear
              placeholder={t('pm.allProjects')}
              style={{ width: 220 }}
              value={projectId}
              options={(data?.projects || []).map((p) => ({
                value: p.id,
                label: p.is_pm ? t('pm.iAmPm', { name: p.name }) : p.name,
              }))}
              onChange={(id) => {
                const next = new URLSearchParams(params)
                if (id) next.set('project', String(id))
                else next.delete('project')
                setParams(next)
              }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => qc.invalidateQueries({ queryKey: ['pm-workbench'] })} loading={isFetching}>
              {t('common.refresh')}
            </Button>
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t('pm.hint')}
        />
        <Row gutter={16}>
          {(data?.stability || []).map((s) => (
            <Col key={s.project_id} xs={24} md={8}>
              <Card size="small" style={{ marginBottom: 12 }}>
                <Statistic title={s.project_name} value={s.success_rate} suffix="%" />
                <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
                  {s.plain}
                </Paragraph>
              </Card>
            </Col>
          ))}
          {!data?.stability?.length ? (
            <Col span={24}>
              <Empty description={t('pm.emptyStability')} />
            </Col>
          ) : null}
        </Row>
      </Card>

      {(data?.pending_mine || []).length > 0 && (
        <Card title={t('pm.pendingMine', { n: (data?.pending_mine || []).length })} style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {data?.pending_mine.map((d) => (
              <Card key={d.id} size="small">
                <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
                  <Descriptions.Item label={t('pm.colRelease')}>
                    {d.release.project_name} / {d.release.pipeline_name} #{d.release.build_number}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('common.environment')}>
                    <Tag color={envColor(d.release.group_type)}>{envLabel(d.release.group_type)}</Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t('pm.colSummary')} span={2}>
                    {d.release.business_summary || t('pm.notFilledYet')}
                  </Descriptions.Item>
                  <Descriptions.Item label={t('pm.impact')}>{d.release.impact_scope || '—'}</Descriptions.Item>
                  <Descriptions.Item label={t('pm.colWindow')}>{d.release.planned_window || t('pm.notScheduled')}</Descriptions.Item>
                </Descriptions>
                <Space style={{ marginTop: 12 }}>
                  <Button
                    type="primary"
                    icon={<CheckOutlined />}
                    onClick={() => setDecide({ id: d.id, approved: true })}
                  >
                    {t('pm.confirmTonight')}
                  </Button>
                  <Button danger icon={<CloseOutlined />} onClick={() => setDecide({ id: d.id, approved: false })}>
                    {t('pm.reject')}
                  </Button>
                  <Button
                    icon={<AuditOutlined />}
                    onClick={() => navigate(`/executions/${d.release.pipeline_id}/${d.release.id}`)}
                  >
                    {t('pm.viewExec')}
                  </Button>
                </Space>
              </Card>
            ))}
          </Space>
        </Card>
      )}

      <Card title={t('pm.inProgress')} style={{ marginBottom: 16 }}>
        <DataTable
          chromeKey="pm-in-progress"
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={data?.in_progress || []}
          pagination={false}
          locale={{ emptyText: t('pm.emptyInProgress') }}
        />
      </Card>

      <Card title={t('pm.planned')} style={{ marginBottom: 16 }}>
        <DataTable
          chromeKey="pm-planned"
          rowKey="id"
          size="small"
          dataSource={data?.planned || []}
          pagination={false}
          locale={{ emptyText: t('pm.emptyPlanned') }}
          columns={[
            { title: t('acl.project'), dataIndex: 'project_name', width: 140 },
            { title: t('pm.colTitle'), dataIndex: 'title' },
            { title: t('acl.pipeline'), dataIndex: 'pipeline_name', width: 160 },
            {
              title: t('pm.colSummary'),
              render: (_: unknown, r: PlannedItem) => r.business_summary || <Text type="secondary">{t('pm.notFilled')}</Text>,
            },
            { title: t('pm.iteration'), dataIndex: 'iteration_tag', width: 120 },
            { title: t('pm.colWindow'), dataIndex: 'planned_window', width: 160 },
            {
              title: t('common.action'),
              width: 100,
              render: (_: unknown, r: PlannedItem) => (
                <Button size="small" onClick={() => navigate('/deploy-requests')}>
                  {t('pm.goRequest')}
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Row gutter={16}>
        <Col xs={24} lg={14}>
          <Card title={t('pm.recent')}>
            <DataTable
              chromeKey="pm-recent"
              rowKey="id"
              size="small"
              columns={recentColumns}
              dataSource={data?.recent || []}
              pagination={{ pageSize: 5, size: 'small', showSizeChanger: false, showTotal: (n) => t('common.total', { n }) }}
              locale={{ emptyText: t('pm.emptyRecent') }}
            />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={t('pm.calendar')}>
            <Calendar
              fullscreen={false}
              fullCellRender={(value: Dayjs, info) => {
                if (info.type !== 'date') return info.originNode
                const key = value.format('YYYY-MM-DD')
                const items = calMap.get(key) || []
                const mark = dayMarkColor(items)
                return (
                  <div
                    className="ant-picker-calendar-date"
                    title={items.map((it) => it.title).join('\n')}
                    style={{ minHeight: 36, padding: '4px 0' }}
                  >
                    <div
                      className="ant-picker-calendar-date-value"
                      style={
                        mark
                          ? {
                              display: 'inline-block',
                              width: 26,
                              height: 26,
                              lineHeight: '26px',
                              borderRadius: '50%',
                              background: mark,
                              color: '#fff',
                              fontWeight: 600,
                            }
                          : undefined
                      }
                    >
                      {value.date()}
                    </div>
                  </div>
                )
              }}
              onSelect={(value: Dayjs) => {
                const items = calMap.get(value.format('YYYY-MM-DD')) || []
                const rel = items.find((i) => i.release_id && i.pipeline_id)
                if (rel?.release_id && rel.pipeline_id) {
                  navigate(`/executions/${rel.pipeline_id}/${rel.release_id}`)
                }
              }}
            />
            <Space size={12} wrap style={{ marginTop: 8 }}>
              <Text type="secondary"><span style={{ color: '#52c41a' }}>●</span> {t('dash.success')}</Text>
              <Text type="secondary"><span style={{ color: '#fa8c16' }}>●</span> {t('pm.legendRunning')}</Text>
              <Text type="secondary"><span style={{ color: '#ff4d4f' }}>●</span> {t('dash.failed')}</Text>
              <Text type="secondary"><span style={{ color: '#1677ff' }}>●</span> {t('pm.legendPlan')}</Text>
            </Space>
            <div>
              <Text type="secondary">{t('pm.calendarHint')}</Text>
            </div>
          </Card>
        </Col>
      </Row>

      <Modal
        title={decide?.approved ? t('pm.confirmOnline') : t('pm.rejectOnline')}
        open={!!decide}
        onCancel={() => {
          setDecide(null)
          setComment('')
        }}
        confirmLoading={decideMut.isPending}
        okButtonProps={{ danger: decide?.approved === false, disabled: decide?.approved === false && !comment.trim() }}
        okText={decide?.approved ? t('common.confirm') : t('pm.reject')}
        onOk={() => decide && decideMut.mutate({ id: decide.id, approved: decide.approved, comment })}
      >
        <p>
          {decide?.approved
            ? t('pm.confirmHint')
            : t('pm.rejectHint')}
        </p>
        <Input.TextArea
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={decide?.approved ? t('pm.commentOptional') : t('pm.rejectRequired')}
        />
      </Modal>

      <Modal
        title={t('pm.announceTitle')}
        open={!!announce}
        onCancel={() => setAnnounce(null)}
        confirmLoading={announceMut.isPending}
        okText={t('pm.send')}
        onOk={() => announce && announceMut.mutate({ id: announce.id, text: announceText })}
      >
        <Paragraph type="secondary">{t('pm.announceHint')}</Paragraph>
        <Input.TextArea rows={8} value={announceText} onChange={(e) => setAnnounceText(e.target.value)} />
      </Modal>
    </div>
  )
}
