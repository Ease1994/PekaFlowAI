import { Avatar, Badge, Dropdown, Empty, List, Spin } from 'antd'
import { BellOutlined, RobotOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { get, post } from '@/api/client'
import { fromNow, kindTitle, noticeHref, type Notice } from './types'
import { useT } from '@/i18n'

export default function NoticeBell() {
  const navigate = useNavigate()
  const t = useT()
  const qc = useQueryClient()
  const { data: unread } = useQuery({
    queryKey: ['notices-unread'],
    queryFn: () => get<{ count: number }>('/notifications/unread-count'),
    refetchInterval: 15000,
  })
  const { data: notices = [], isFetching } = useQuery({
    queryKey: ['notices-brief'],
    queryFn: () => get<Notice[]>('/notifications', { limit: 8 }),
    refetchInterval: 15000,
  })
  const count = unread?.count || 0

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['notices'] })
    qc.invalidateQueries({ queryKey: ['notices-brief'] })
    qc.invalidateQueries({ queryKey: ['notices-unread'] })
  }

  const open = async (n: Notice) => {
    if (!n.is_read) {
      await post(`/notifications/${n.id}/read`)
      refresh()
    }
    navigate(noticeHref(n))
  }

  return (
    <Dropdown
      trigger={['click']}
      dropdownRender={() => (
        <div
          style={{
            width: 'min(420px, calc(100vw - 16px))',
            maxHeight: 480,
            overflow: 'auto',
            background: '#fff',
            borderRadius: 8,
            boxShadow: '0 6px 16px rgba(0,0,0,0.12)',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '10px 16px',
              borderBottom: '1px solid #f0f0f0',
            }}
          >
            <span style={{ fontWeight: 600, borderBottom: '2px solid #1677ff', paddingBottom: 2 }}>{t('notify.title')}</span>
            <a
              onClick={(e) => {
                e.preventDefault()
                navigate('/notifications')
              }}
            >
              {t('notify.center')} &gt;
            </a>
          </div>
          {isFetching && !notices.length ? (
            <Spin style={{ display: 'block', margin: 24 }} />
          ) : notices.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('notify.empty')} style={{ margin: 24 }} />
          ) : (
            <List
              dataSource={notices}
              renderItem={(n) => (
                <List.Item
                  style={{
                    padding: '12px 16px',
                    alignItems: 'flex-start',
                    background: n.is_read ? undefined : '#f6f9ff',
                  }}
                >
                  <List.Item.Meta
                    avatar={
                      <Avatar
                        style={{ background: (n.kind || '').startsWith('release') ? '#1677ff' : '#52c41a' }}
                        icon={(n.kind || '').startsWith('release') ? <RobotOutlined /> : <SafetyCertificateOutlined />}
                      />
                    }
                    title={
                      <span style={{ fontWeight: n.is_read ? 500 : 600 }}>{kindTitle(n.kind)}</span>
                    }
                    description={
                      <div>
                        {/* 正文裁到两行，链接必须放在裁剪区外，否则内容一长就被一起截掉 */}
                        <div
                          style={{
                            color: '#333',
                            fontSize: 13,
                            lineHeight: 1.6,
                            display: '-webkit-box',
                            WebkitLineClamp: 2,
                            WebkitBoxOrient: 'vertical',
                            overflow: 'hidden',
                          }}
                        >
                          {n.title}
                          {n.content ? `：${n.content.replace(/\n/g, ' ')}` : ''}
                        </div>
                        <div
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            marginTop: 6,
                          }}
                        >
                          <span style={{ color: '#bbb', fontSize: 12 }}>{fromNow(n.created_at)}</span>
                          <a
                            onClick={(e) => {
                              e.preventDefault()
                              e.stopPropagation()
                              open(n)
                            }}
                          >
                            {t('notify.viewDetail')}
                          </a>
                        </div>
                      </div>
                    }
                  />
                </List.Item>
              )}
            />
          )}
        </div>
      )}
    >
      <Badge count={count} overflowCount={99} size="small">
        <BellOutlined style={{ fontSize: 18, cursor: 'pointer' }} />
      </Badge>
    </Dropdown>
  )
}
