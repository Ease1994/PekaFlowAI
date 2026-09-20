import { Radio, Input, Space, Alert, Typography } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import type { GraphTrigger } from '@/api/types'
import { formatDateTime, useT } from '@/i18n'

const { Text } = Typography

interface TriggerPanelProps {
  triggers: GraphTrigger[]
  onChange: (next: GraphTrigger[]) => void
  pipelineId?: number
}

/**
 * 一条流水线只能选手动或定时，两种不能并存。
 * 选定时后到点自动跑；列表里的「执行」按钮两种都能用。
 */
export default function TriggerPanel({ triggers, onChange, pipelineId }: TriggerPanelProps) {
  const t = useT()
  const current = triggers[0] || { type: 'manual', cron: null, label: t('pipe.manualTrigger') }
  const isCron = current.type === 'cron'

  const { data: nextRun } = useQuery({
    queryKey: ['next-run', pipelineId],
    queryFn: () => get<{ next_run_at: string | null }>(`/pipelines/${pipelineId}/next-run`),
    enabled: !!pipelineId && isCron,
    refetchInterval: isCron ? 30000 : false,
  })

  const setType = (type: 'manual' | 'cron') => {
    if (type === 'cron') {
      onChange([
        {
          type: 'cron',
          cron: current.cron || '0 2 * * *',
          label: t('pipe.cronTrigger'),
        },
      ])
      return
    }
    onChange([{ type: 'manual', cron: null, label: t('pipe.manualTrigger') }])
  }

  return (
    <div style={{ padding: 16, maxWidth: 640 }}>
      <div style={{ marginBottom: 16, color: '#666', fontSize: 13 }}>
        <ThunderboltOutlined /> {t('pipe.triggerModeHint')}
      </div>

      <Radio.Group
        value={isCron ? 'cron' : 'manual'}
        onChange={(e) => setType(e.target.value)}
        optionType="button"
        buttonStyle="solid"
        style={{ marginBottom: 16 }}
      >
        <Radio.Button value="manual">{t('pipe.manualTrigger')}</Radio.Button>
        <Radio.Button value="cron">{t('pipe.cronTrigger')}</Radio.Button>
      </Radio.Group>

      {isCron ? (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <div>
            <Text type="secondary">{t('pipe.cronExpr')}</Text>
            <Input
              value={current.cron || ''}
              onChange={(e) =>
                onChange([{ type: 'cron', cron: e.target.value, label: t('pipe.cronTrigger') }])
              }
              placeholder="0 2 * * *"
              style={{ marginTop: 6, maxWidth: 280 }}
            />
            <div style={{ color: '#999', fontSize: 12, marginTop: 6 }}>
              {t('pipe.cronExample', { daily: '0 2 * * *', every: '*/15 * * * *' })}
            </div>
          </div>
          <Alert
            type={nextRun?.next_run_at ? 'success' : 'warning'}
            showIcon
            message={
              nextRun?.next_run_at
                ? t('pipe.cronNext', { time: formatDateTime(nextRun.next_run_at) })
                : t('pipe.cronNeedRedis')
            }
            description={t("pipe.cronDesc")}
          />
        </Space>
      ) : (
        <Alert
          type="info"
          showIcon
          message={t("pipe.manualTrigger")}
          description={t("pipe.manualDesc")}
        />
      )}
    </div>
  )
}
