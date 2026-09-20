import { useMemo, useState } from 'react'
import { Avatar, Button, Card, Drawer, Empty, List, Space, Tabs, Tag, message } from 'antd'
import { RobotOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { get, post } from '@/api/client'
import { fromNow, kindTitle, noticeHref, type Notice } from './types'
import { useT } from '@/i18n'

export default function NoticeCenter() {
  const navigate = useNavigate()
  const t = useT()
  const [params, setParams] = useSearchParams()
  const qc = useQueryClient()
  const tab = params.get('tab') === 'read' ? 'read' : 'unread'
  const kind = params.get('kind') || ''
  const focusId = Number(params.get('id') || 0)
  const [detail, setDetail] = useState<Notice | null>(null)

  const query = useMemo(() => {
    const q: Record<string, string | number | boolean> = { limit: 100 }
    if (tab === 'unread') q.unread_only = true
    else q.read_only = true
    if (kind) q.kind = kind
    return q
  }, [tab, kind])

  const { data: notices = [] } = useQuery({
    queryKey: ['notices', query],
    queryFn: () => get<Notice[]>('/notifications', query),
    refetchInterval: 15000,
  })
  const { data: unread } = useQuery({
    queryKey: ['notices-unread'],
    queryFn: () => get<{ count: number }>('/notifications/unread-count'),
    refetchInterval: 15000,
  })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['notices'] })
    qc.invalidateQueries({ queryKey: ['notices-brief'] })
    qc.invalidateQueries({ queryKey: ['notices-unread'] })
  }

  const markOne = async (n: Notice) => {
    if (!n.is_read) {
      await post(`/notifications/${n.id}/read`)
      refresh()
    }
  }

  const openDetail = async (n: Notice) => {
    await markOne(n)
    setDetail(n)
  }

  const goLink = async (n: Notice) => {
    await markOne(n)
    navigate(noticeHref(n))
  }

  const markAll = async () => {
    const r = await post<{ updated: number }>('/notifications/read-all')
    message.success(r.updated ? t('notify.marked', { n: r.updated }) : t('notify.noneUnread'))
    refresh()
  }

  // 助手/铃铛给的是 /notifications?id=。当前 tab 是未读时，已读那条不在列表里，要单独拉。
  const focusedNoticeQuery = useQuery({
    queryKey: ['notice-one', focusId],
    queryFn: () => get<Notice>(`/notifications/${focusId}`),
    enabled: focusId > 0,
  })
  const focused = focusedNoticeQuery.data || (focusId ? notices.find((n) => n.id === focusId) : null)

  return (
    <Card
      title={t('notify.allTitle')}
      extra={
        <Button type="link" onClick={markAll} disabled={!unread?.count}>
          {t('notify.markAll')}
        </Button>
      }
    >
      <Tabs
        activeKey={tab}
        onChange={(k) => {
          const next = new URLSearchParams(params)
          next.set('tab', k)
          setParams(next)
        }}
        items={[
          { key: 'unread', label: unread?.count ? t('notify.unreadN', { n: unread.count }) : t('notify.unread') },
          { key: 'read', label: t('notify.read') },
        ]}
      />
      <Space style={{ marginBottom: 12 }}>
        {[
          { key: '', text: t('notify.all') },
          { key: 'release', text: t('notify.release') },
          { key: 'access', text: t('notify.access') },
        ].map((x) => (
          <Tag.CheckableTag
            key={x.key}
            checked={kind === x.key}
            onChange={() => {
              const next = new URLSearchParams(params)
              if (x.key) next.set('kind', x.key)
              else next.delete('kind')
              setParams(next)
            }}
          >
            {x.text}
          </Tag.CheckableTag>
        ))}
      </Space>
      {notices.length === 0 ? (
        <Empty description={tab === 'unread' ? t('notify.noneUnread') : t('notify.noneRead')} />
      ) : (
        <List
          dataSource={notices}
          renderItem={(n) => (
            <List.Item style={{ alignItems: 'flex-start', background: n.is_read ? undefined : '#f6f9ff' }}>
              <List.Item.Meta
                avatar={
                  <Avatar
                    style={{ background: (n.kind || '').startsWith('release') ? '#1677ff' : '#52c41a' }}
                    icon={(n.kind || '').startsWith('release') ? <RobotOutlined /> : <SafetyCertificateOutlined />}
                  />
                }
                title={
                  <Space>
                    <b>{kindTitle(n.kind)}</b>
                    <span style={{ color: '#bbb', fontWeight: 400, fontSize: 12 }}>{fromNow(n.created_at)}</span>
                  </Space>
                }
                description={
                  <div style={{ color: '#333', whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>
                    <div style={{ marginBottom: 4, fontWeight: 500 }}>{n.title}</div>
                    <div style={{ color: '#555', display: '-webkit-box', WebkitLineClamp: 4, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
                      {n.content}
                    </div>
                    <a
                      style={{ marginTop: 4, display: 'inline-block' }}
                      onClick={(e) => {
                        e.preventDefault()
                        openDetail(n)
                      }}
                    >
                      {t('notify.viewDetail')}
                    </a>
                  </div>
                }
              />
            </List.Item>
          )}
        />
      )}

      <Drawer
        title={detail?.title || focused?.title || t('notify.detail')}
        width={520}
        open={!!detail || !!focused}
        onClose={() => {
          setDetail(null)
          if (focusId) {
            const next = new URLSearchParams(params)
            next.delete('id')
            setParams(next)
          }
        }}
        extra={
          (detail || focused) ? (
            <Button type="primary" onClick={() => goLink(detail || focused!)}>
              {t('notify.openRelated')}
            </Button>
          ) : null
        }
      >
        {(detail || focused) && (
          <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.8 }}>
            {(detail || focused)!.content || t('notify.noBody')}
          </div>
        )}
      </Drawer>
    </Card>
  )
}
