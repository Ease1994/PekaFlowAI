import { useState } from 'react'
import { Alert, Button, Card, Empty, Input, Modal, Space, Tabs, Tag, message } from 'antd'
import DataTable from '@/components/DataTable'
import { CheckOutlined, CloseOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { get, postR } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { ReleaseApproval } from '@/api/types'
import { envColor, groupDisplayName } from '@/env'
import { useIsMobile } from '@/hooks/useIsMobile'
import { t as translate, useT } from '@/i18n'
import { releaseStatusMeta } from '@/utils/releaseStatus'

/** 审批弹窗要处理哪一张单、通过还是驳回、技术审批还是业务确认。 */
type DecideTarget = { id: number; approved: boolean; kind?: 'tech' | 'pm'; action?: string }

/** 审批单状态码对应 Tag 颜色；句子按码现取。 */
const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  approved: 'success',
  rejected: 'error',
  cancelled: 'default',
}

/** 审批单状态码 → i18n 键。pending/rejected/cancelled 复用 status.*。 */
const STATUS_I18N: Record<string, string> = {
  pending: 'status.pending',
  approved: 'approval.approved',
  rejected: 'status.rejected',
  cancelled: 'status.cancelled',
}

const ACTION_COLOR: Record<string, string> = {
  release: 'blue',
  rollback: 'orange',
  rebuild: 'cyan',
}

/**
 * 审批列表上的操作类型文案。回滚、Rebuild、普通发布必须一眼能分开。
 */
function actionNoun(action?: string) {
  if (action === 'rollback') return translate('pipe.actionRollback')
  if (action === 'rebuild') return translate('approval.actionRebuild')
  return translate('approval.actionRelease')
}

/** 审批单状态文案：有码就翻译，没有码回落到原值。 */
function approvalStatusText(code: string): string {
  return STATUS_I18N[code] ? translate(STATUS_I18N[code]) : code
}

/**
 * 这次要批的内容：发布看 commit 说明；回滚先写撤销哪次构建，再补 commit 或回滚计划。
 */
function changeText(row: ReleaseApproval) {
  const hash = (row.commit_short || (row.source_ref ? row.source_ref.slice(0, 8) : '')).trim()
  const title = (row.commit_title || '').trim()
  if (row.action === 'rollback') {
    const undo = row.rollback_of_build_number
      ? translate('approval.undoBuild', { n: row.rollback_of_build_number })
      : translate('approval.undoLast')
    const extra = title ? [hash, title].filter(Boolean).join('  ') : (row.undo_summary || '').trim() || hash
    return extra ? `${undo} · ${extra}` : undo
  }
  return [hash, title].filter(Boolean).join('  ') || '-'
}

/**
 * 当前用户能不能点这张审批单的通过/驳回。
 * 优先信后端 can_decide；没有这个字段时按管理员或指定审批人兜底。
 */
function canDecideApproval(row: ReleaseApproval, userId?: number, isAdmin?: boolean) {
  return row.can_decide ?? (!!isAdmin || row.approver_id === userId)
}

/**
 * 窄屏审批卡片：流水线、类型、环境、这次要动的内容和通过/驳回。
 * 宽表在手机上会把操作列挤出屏幕，企微 H5 点进来后几乎没法批。
 */
function ApprovalMobileCards({
  rows,
  userId,
  isAdmin,
  onDecide,
  onOpenRelease,
}: {
  rows: ReleaseApproval[]
  userId?: number
  isAdmin?: boolean
  onDecide: (row: ReleaseApproval, approved: boolean) => void
  onOpenRelease: (row: ReleaseApproval) => void
}) {
  const t = useT()
  if (rows.length === 0) return <Empty description={t('approval.noRecords')} />
  return (
    <div className="mobile-entity-list">
      {rows.map((row) => {
        const canDecide = canDecideApproval(row, userId, isAdmin)
        const ended = row.status === 'pending' && row.release_status !== 'pending'
        return (
          <div key={row.id} className="mobile-entity-card">
            <a className="mobile-entity-title" onClick={() => onOpenRelease(row)}>
              {row.pipeline_name || `${actionNoun(row.action)} #${row.build_number || row.release_id}`}
            </a>
            <div className="mobile-entity-meta">
              <Tag color={ACTION_COLOR[row.action || 'release']}>{row.action_label || actionNoun(row.action)}</Tag>
              {row.group_type ? <Tag color={envColor(row.group_type)}>{groupDisplayName({ name: row.group_name, type: row.group_type })}</Tag> : null}
              <Tag color={STATUS_COLOR[row.status]}>{approvalStatusText(row.status)}</Tag>
            </div>
            <div className="mobile-entity-meta">{changeText(row)}</div>
            <div className="mobile-entity-meta">
              {row.project_name ? <span>{row.project_name} · </span> : null}
              <span>{t('approval.requesterName', { name: row.requester || '-' })}</span>
              {row.is_self_approval ? <Tag color="blue">{t('approval.selfReview')}</Tag> : null}
            </div>
            {row.status === 'pending' && !ended && canDecide ? (
              <div className="mobile-entity-actions">
                <Space>
                  <Button type="primary" size="small" icon={<CheckOutlined />} onClick={() => onDecide(row, true)}>
                    {t('approval.pass')}
                  </Button>
                  <Button danger size="small" icon={<CloseOutlined />} onClick={() => onDecide(row, false)}>
                    {t('approval.reject')}
                  </Button>
                </Space>
              </div>
            ) : (
              <div className="mobile-entity-meta">
                {ended
                  ? t('approval.actionEnded', { action: actionNoun(row.action) })
                  : canDecide
                    ? null
                    : t('approval.waitingApprover', { name: row.approver || t('approval.approver') })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/**
 * 窄屏业务确认卡片。字段来自项目经理闸，和上面技术审批不是同一张表。
 */
function PmMobileCards({
  rows,
  onDecide,
}: {
  rows: any[]
  onDecide: (row: any, approved: boolean) => void
}) {
  const t = useT()
  if (rows.length === 0) {
    return (
      <div style={{ color: '#999', padding: 16 }}>
        {t('approval.pmEmpty')}
      </div>
    )
  }
  return (
    <div className="mobile-entity-list">
      {rows.map((r) => (
        <div key={r.id} className="mobile-entity-card">
          <div className="mobile-entity-title">{r.release?.pipeline_name || t('approval.pendingOnline')}</div>
          <div className="mobile-entity-meta">{r.release?.project_name}</div>
          <div className="mobile-entity-meta">
            {t('approval.summary', { text: r.release?.business_summary || t('approval.notFilled') })}
          </div>
          <div className="mobile-entity-meta">
            {t('approval.window', { text: r.release?.planned_window || t('approval.notScheduled') })}
          </div>
          <div className="mobile-entity-actions">
            <Space>
              <Button type="primary" size="small" icon={<CheckOutlined />} onClick={() => onDecide(r, true)}>
                {t('common.confirm')}
              </Button>
              <Button danger size="small" icon={<CloseOutlined />} onClick={() => onDecide(r, false)}>
                {t('approval.reject')}
              </Button>
            </Space>
          </div>
        </div>
      ))}
    </div>
  )
}

export default function ReleaseApprovalPage() {
  const t = useT()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const user = useAuthStore((s) => s.user)
  /** 企微 H5 进来主要是审批，窄屏改卡片，不要再横滑 1500px 的表。 */
  const isMobile = useIsMobile()
  const tab = params.get('tab') || 'pending'
  /** 通知链接带 release_id，用来高亮刚提交的那张单。 */
  const focusReleaseId = Number(params.get('release_id') || 0) || 0
  const [target, setTarget] = useState<DecideTarget | null>(null)
  const [comment, setComment] = useState('')

  const { data: pending = [] } = useQuery({
    queryKey: ['release-approvals', 'pending'],
    queryFn: () => get<ReleaseApproval[]>('/approvals/pending', { scope: 'pending' }),
    refetchInterval: 10000,
  })
  const { data: mine = [] } = useQuery({
    queryKey: ['release-approvals', 'mine'],
    queryFn: () => get<ReleaseApproval[]>('/approvals/pending', { scope: 'mine' }),
    refetchInterval: 10000,
  })
  const { data: allRows = [] } = useQuery({
    queryKey: ['release-approvals', 'all'],
    queryFn: () => get<ReleaseApproval[]>('/approvals/pending', { scope: 'all' }),
    enabled: !!user?.is_admin,
    refetchInterval: 10000,
  })

  const { data: pmPending = [] } = useQuery({
    queryKey: ['pm-decisions', 'pending'],
    queryFn: () => get<any[]>('/pm/decisions', { scope: 'pending' }),
    refetchInterval: 10000,
  })

  const decideMut = useMutation({
    mutationFn: ({
      id,
      approved,
      comment: c,
      kind,
    }: {
      id: number
      approved: boolean
      comment: string
      kind?: 'tech' | 'pm'
      action?: string
    }) =>
      kind === 'pm'
        ? postR<{ status?: string }>(`/pm/decisions/${id}/decide`, { approved, comment: c })
        : postR<{ status?: string }>(`/approvals/${id}/decide`, { approved, comment: c }),
    onSuccess: (res, vars) => {
      // 审批过了不代表跑起来了：拆构建任务也可能失败（比如仓库凭证解不开）。
      // 这种情况后端返回成功 + 原因，别报「已进入执行」把人骗了
      const started = res.data?.status === 'running' || res.data?.status === 'queued'
      if (vars.approved && !started) {
        message.warning(res.message || t('approval.passStartWarn', { action: actionNoun(vars.action) }), 8)
      } else {
        message.success(vars.approved ? t('approval.passedQueued', { action: actionNoun(vars.action) }) : t('approval.rejectedMsg'))
      }
      setTarget(null)
      setComment('')
      qc.invalidateQueries({ queryKey: ['release-approvals'] })
      qc.invalidateQueries({ queryKey: ['pm-decisions'] })
    },
    onError: () => {
      // 多半是这单子已经被处理过了，刷新一下让它从待办里消失，省得反复点
      qc.invalidateQueries({ queryKey: ['release-approvals'] })
      qc.invalidateQueries({ queryKey: ['pm-decisions'] })
    },
  })

  const columns = (opts: { review?: boolean }) => [
    { title: t('approval.colId'), dataIndex: 'id', width: 70 },
    {
      title: t('approval.colBuild'),
      dataIndex: 'build_number',
      width: 90,
      render: (_: number | null | undefined, row: ReleaseApproval) =>
        row.pipeline_id ? (
          <a onClick={() => navigate(`/executions/${row.pipeline_id}/${row.release_id}`)}>
            #{row.build_number || row.release_id}
          </a>
        ) : (
          `#${row.build_number || row.release_id}`
        ),
    },
    { title: t('acl.project'), dataIndex: 'project_name', ellipsis: true },
    { title: t('acl.pipeline'), dataIndex: 'pipeline_name', ellipsis: true },
    {
      title: t('common.environment'),
      dataIndex: 'group_type',
      width: 100,
      render: (v: string, row: ReleaseApproval) => (
        <Tag color={envColor(v)}>{groupDisplayName({ name: row.group_name, type: v })}</Tag>
      ),
    },
    {
      title: t('common.type'),
      dataIndex: 'action',
      width: 90,
      render: (v: string, row: ReleaseApproval) => (
        <Tag color={ACTION_COLOR[v || 'release']}>{row.action_label || actionNoun(v)}</Tag>
      ),
    },
    {
      title: t('approval.colChange'),
      key: 'change',
      width: 280,
      ellipsis: true,
      render: (_: unknown, row: ReleaseApproval) => (
        <span title={changeText(row)}>{changeText(row)}</span>
      ),
    },
    {
      title: t('approval.colRequester'),
      dataIndex: 'requester',
      width: 130,
      render: (v: string, row: ReleaseApproval) => (
        <Space size={4}>
          <span>{v || '-'}</span>
          {row.is_self_approval && <Tag color="blue">{t('approval.selfReview')}</Tag>}
        </Space>
      ),
    },
    { title: t('approval.colApprover'), dataIndex: 'approver', width: 180, ellipsis: true },
    {
      title: t('approval.colApprovalStatus'),
      dataIndex: 'status',
      width: 100,
      render: (v: string) => <Tag color={STATUS_COLOR[v]}>{approvalStatusText(v)}</Tag>,
    },
    {
      title: t('approval.colReleaseStatus'),
      dataIndex: 'release_status',
      width: 100,
      render: (v: string) => {
        const meta = releaseStatusMeta(v)
        return <Tag color={meta.color}>{meta.text}</Tag>
      },
    },
    { title: t('approval.colCreated'), dataIndex: 'created_at', width: 180 },
    { title: t('approval.colComment'), dataIndex: 'comment', ellipsis: true },
    ...(opts.review
      ? [
          {
            title: t('common.action'),
            width: 170,
            fixed: 'right' as const,
                  render: (_: unknown, row: ReleaseApproval) => {
                    if (row.status !== 'pending') return null
                    // 发布已取消/结束时后端也会拒绝，别给一个必然报错的按钮
                    if (row.release_status !== 'pending') return <span style={{ color: '#999' }}>{t('approval.actionEnded', { action: actionNoun(row.action) })}</span>
                    const canDecide =
                      row.can_decide ?? (!!user?.is_admin || row.approver_id === user?.id)
                    if (!canDecide) {
                      return <span style={{ color: '#999' }}>{t('approval.waitingApprover', { name: row.approver || t('approval.approver') })}</span>
                    }
              return (
                <Space>
                  <Button
                    type="primary"
                    size="small"
                    icon={<CheckOutlined />}
                    onClick={() => setTarget({ id: row.id, approved: true, action: row.action })}
                  >
                    {t('approval.pass')}
                  </Button>
                  <Button
                    danger
                    size="small"
                    icon={<CloseOutlined />}
                    onClick={() => setTarget({ id: row.id, approved: false, action: row.action })}
                  >
                    {t('approval.reject')}
                  </Button>
                </Space>
              )
            },
          },
        ]
      : []),
  ]

  /** 点流水线名进执行页；手机卡片和桌面表格共用。 */
  const openRelease = (row: ReleaseApproval) => {
    if (row.pipeline_id) navigate(`/executions/${row.pipeline_id}/${row.release_id}`)
  }

  /** 窄屏卡片、宽屏表格，数据源不变。 */
  const renderApprovals = (rows: ReleaseApproval[], chromeKey: string) =>
    isMobile ? (
      <ApprovalMobileCards
        rows={rows}
        userId={user?.id}
        isAdmin={!!user?.is_admin}
        onDecide={(row, approved) => setTarget({ id: row.id, approved, action: row.action })}
        onOpenRelease={openRelease}
      />
    ) : (
      <DataTable
        chromeKey={chromeKey}
        rowKey="id"
        size="small"
        scroll={{ x: 1680 }}
        columns={columns({ review: true })}
        dataSource={rows}
        rowClassName={(row: ReleaseApproval) =>
          focusReleaseId && row.release_id === focusReleaseId ? 'ant-table-row-selected' : ''
        }
      />
    )

  return (
    <div>
      {!isMobile && (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t('approval.alertTitle')}
        description={
          <>
            {t('approval.alertDesc')}
            <ThunderboltOutlined style={{ color: '#fa8c16', margin: '0 4px' }} />
            {t('approval.alertEmergency')}
          </>
        }
      />
      )}

      <Card>
        <Tabs
          size={isMobile ? 'small' : 'middle'}
          activeKey={['pending', 'pm', 'mine', 'all'].includes(tab) ? tab : 'pending'}
          onChange={(k) => {
            const next = new URLSearchParams(params)
            next.set('tab', k)
            setParams(next, { replace: true })
          }}
          items={[
            {
              key: 'pending',
              label: t('approval.tabPending', { n: pending.length }),
              children: renderApprovals(pending, 'approval-pending'),
            },
            {
              key: 'pm',
              label: t('approval.tabPm', { n: pmPending.length }),
              children: isMobile ? (
                <PmMobileCards
                  rows={pmPending}
                  onDecide={(r, approved) => setTarget({ id: r.id, approved, kind: 'pm' })}
                />
              ) : pmPending.length === 0 ? (
                  <div style={{ color: '#999', padding: 16 }}>
                    {t('approval.pmEmpty')}
                  </div>
                ) : (
                  <DataTable
                    chromeKey="pm-pending"
                    rowKey="id"
                    size="small"
                    dataSource={pmPending}
                    columns={[
                      { title: t('acl.project'), render: (_: unknown, r: any) => r.release?.project_name },
                      { title: t('acl.pipeline'), render: (_: unknown, r: any) => r.release?.pipeline_name },
                      { title: t('approval.colSummary'), render: (_: unknown, r: any) => r.release?.business_summary || t('approval.notFilled') },
                      { title: t('approval.colWindow'), render: (_: unknown, r: any) => r.release?.planned_window || t('approval.notScheduled') },
                      {
                        title: t('common.action'),
                        width: 180,
                        render: (_: unknown, r: any) => (
                          <Space>
                            <Button
                              type="primary"
                              size="small"
                              icon={<CheckOutlined />}
                              onClick={() => setTarget({ id: r.id, approved: true, kind: 'pm' })}
                            >
                              {t('common.confirm')}
                            </Button>
                            <Button
                              danger
                              size="small"
                              icon={<CloseOutlined />}
                              onClick={() => setTarget({ id: r.id, approved: false, kind: 'pm' })}
                            >
                              {t('approval.reject')}
                            </Button>
                          </Space>
                        ),
                      },
                    ]}
                  />
                ),
            },
            {
              key: 'mine',
              label: t('approval.tabMine', { n: mine.length }),
              children: renderApprovals(mine, 'approval-mine'),
            },
            ...(user?.is_admin
              ? [
                  {
                    key: 'all',
                    label: t('approval.tabAll', { n: allRows.length }),
                    children: renderApprovals(allRows, 'approval-all'),
                  },
                ]
              : []),
          ]}
        />
      </Card>

      <Modal
        title={
          target?.kind === 'pm'
            ? target?.approved
              ? t('approval.modalConfirmOnline')
              : t('approval.modalRejectOnline')
            : target?.approved
              ? t('approval.modalPassAction', { action: actionNoun(target?.action) })
              : t('approval.modalRejectAction', { action: actionNoun(target?.action) })
        }
        open={!!target}
        styles={{ content: { maxWidth: 'calc(100vw - 24px)' } }}
        onCancel={() => {
          setTarget(null)
          // 留着的话，下一条审批会带出上一条的意见，很容易顺手就提交了
          setComment('')
        }}
        confirmLoading={decideMut.isPending}
        okButtonProps={{
          danger: target?.approved === false,
          disabled: target?.approved === false && !comment.trim(),
        }}
        okText={target?.approved ? t('approval.okPass') : t('approval.okReject')}
        onOk={() => {
          if (!target) return
          decideMut.mutate({
            id: target.id,
            approved: target.approved,
            comment,
            kind: target.kind,
            action: target.action,
          })
        }}
      >
        <p>
          {target?.kind === 'pm'
            ? target?.approved
              ? t('approval.pmPassHint')
              : t('approval.pmRejectHint')
            : target?.approved
              ? t('approval.techPassHint', { action: actionNoun(target?.action) })
              : t('approval.techRejectHint', { action: actionNoun(target?.action) })}
        </p>
        <Input.TextArea
          rows={3}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder={target?.approved ? t('approval.commentOptional') : t('approval.rejectRequired')}
        />
      </Modal>
    </div>
  )
}
