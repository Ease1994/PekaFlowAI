import { Modal, Card } from 'antd'
import { useT } from '@/i18n'

interface JobTypeSelectorProps {
  open: boolean
  onClose: () => void
  onSelect: (type: 'linux' | 'windows' | 'macos') => void
}

const JOB_TYPES = [
  { type: 'windows' as const, label: 'Windows', icon: '🪟', desc: 'Windows Server 2019+' },
  { type: 'macos' as const, label: 'macOS', icon: '🍎', desc: 'macOS 12+' },
  { type: 'linux' as const, label: 'Linux', icon: '🐧', desc: 'Ubuntu / CentOS / Debian' },
]

/**
 * Job 类型选择器（对应蓝盾"请选择Job类型"弹窗）
 */
export default function JobTypeSelector({ open, onClose, onSelect }: JobTypeSelectorProps) {
  const t = useT()
  return (
    <Modal
      title={t("pipe.pickJobType")}
      open={open}
      onCancel={onClose}
      footer={null}
      width={520}
      destroyOnClose
    >
      <div style={{ display: 'flex', justifyContent: 'space-around', padding: '24px 0' }}>
        {JOB_TYPES.map((t) => (
          <Card
            key={t.type}
            hoverable
            style={{ width: 130, textAlign: 'center' }}
            onClick={() => {
              onSelect(t.type)
              onClose()
            }}
          >
            <div style={{ fontSize: 56, marginBottom: 12 }}>{t.icon}</div>
            <div style={{ fontWeight: 600 }}>{t.label}</div>
            <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>{t.desc}</div>
          </Card>
        ))}
      </div>
    </Modal>
  )
}
