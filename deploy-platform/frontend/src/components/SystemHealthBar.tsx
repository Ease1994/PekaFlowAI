import { useMemo, useState } from 'react'
import { Alert, Drawer, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import { useIsMobile } from '@/hooks/useIsMobile'
import { useT } from '@/i18n'

type HealthComponent = {
  name: string
  status: 'ok' | 'down' | string
  detail: string
}

type HealthPayload = {
  status: string
  checked_at: string
  components: HealthComponent[]
  alerts: { name: string; detail: string }[]
}

/**
 * 顶栏依赖告警：ES / Redis / MySQL 挂了才出现，点开看探测原文。
 */
export default function SystemHealthBar() {
  const [open, setOpen] = useState(false)
  const isMobile = useIsMobile()
  const t = useT()
  const { data } = useQuery({
    queryKey: ['system-health-deps'],
    queryFn: () => get<HealthPayload>('/health/deps', undefined, { skipErrorToast: true }),
    refetchInterval: 20000,
    retry: 1,
  })

  const down = useMemo(
    () => (data?.components || []).filter((c) => c.status !== 'ok'),
    [data],
  )
  if (!down.length) return null

  return (
    <>
      <Space size={8} className="system-health-bar" wrap={false}>
        {down.map((c) => (
          <Tag
            key={c.name}
            color="error"
            style={{ cursor: 'pointer', marginInlineEnd: 0 }}
            onClick={() => setOpen(true)}
          >
            {t('health.abnormal', { name: c.name })}
          </Tag>
        ))}
        <Typography.Link style={{ fontSize: 12, whiteSpace: 'nowrap' }} onClick={() => setOpen(true)}>
          {t('health.details')}
        </Typography.Link>
      </Space>
      <Drawer title={t('health.drawerTitle')} width={isMobile ? '100%' : 480} open={open} onClose={() => setOpen(false)}>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {t('health.checkedAt', { time: data?.checked_at || t('health.hint') })}
        </Typography.Paragraph>
        {(data?.components || []).map((c) => (
          <Alert
            key={c.name}
            type={c.status === 'ok' ? 'success' : 'error'}
            showIcon
            style={{ marginBottom: 12 }}
            message={`${c.name}：${c.status === 'ok' ? t('health.ok') : t('health.bad')}`}
            description={<pre className="system-health-detail">{c.detail || t('health.hint')}</pre>}
          />
        ))}
      </Drawer>
    </>
  )
}
