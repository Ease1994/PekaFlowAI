import { useEffect, useMemo, useState } from 'react'
import { Button, Checkbox, Popover, Radio, Space } from 'antd'
import { SettingOutlined } from '@ant-design/icons'
import { useT } from '@/i18n'

export type TableSize = 'small' | 'middle' | 'large'

export interface ColumnOption {
  key: string
  label: string
  locked?: boolean
}

interface Props {
  options: ColumnOption[]
  visible: string[]
  size: TableSize
  onApply: (visible: string[], size: TableSize) => void
}

/** 表头齿轮：勾选显示哪些列、选行高。点确认才生效。 */
export default function TableColumnSettings({ options, visible, size, onApply }: Props) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [draftVisible, setDraftVisible] = useState(visible)
  const [draftSize, setDraftSize] = useState(size)

  useEffect(() => {
    if (open) {
      setDraftVisible(visible)
      setDraftSize(size)
    }
  }, [open, visible, size])

  const optionalKeys = useMemo(() => options.filter((o) => !o.locked).map((o) => o.key), [options])
  const lockedKeys = useMemo(() => options.filter((o) => o.locked).map((o) => o.key), [options])
  const allChecked = optionalKeys.length > 0 && optionalKeys.every((k) => draftVisible.includes(k))
  const someChecked = optionalKeys.some((k) => draftVisible.includes(k))

  const toggle = (key: string, checked: boolean, locked?: boolean) => {
    if (locked) return
    setDraftVisible((prev) => (checked ? [...new Set([...prev, key])] : prev.filter((k) => k !== key)))
  }

  const content = (
    <div style={{ width: 320 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontWeight: 600 }}>{t('common.fieldDisplay')}</span>
        <Checkbox
          checked={allChecked}
          indeterminate={!allChecked && someChecked}
          onChange={(e) =>
            setDraftVisible(e.target.checked ? [...lockedKeys, ...optionalKeys] : [...lockedKeys])
          }
        >
          {t('common.selectAll')}
        </Checkbox>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: '6px 12px',
          marginBottom: 16,
        }}
      >
        {options.map((o) => (
          <Checkbox
            key={o.key}
            checked={o.locked || draftVisible.includes(o.key)}
            disabled={o.locked}
            onChange={(e) => toggle(o.key, e.target.checked, o.locked)}
          >
            {o.label}
          </Checkbox>
        ))}
      </div>
      <div style={{ marginBottom: 8, fontWeight: 600 }}>{t('common.tableRowHeight')}</div>
      <Radio.Group
        optionType="button"
        buttonStyle="solid"
        value={draftSize}
        onChange={(e) => setDraftSize(e.target.value)}
        options={[
          { label: t('common.sizeSmall'), value: 'small' },
          { label: t('common.sizeMedium'), value: 'middle' },
          { label: t('common.sizeLarge'), value: 'large' },
        ]}
      />
      <div style={{ marginTop: 16, textAlign: 'right' }}>
        <Space>
          <Button
            type="primary"
            size="small"
            onClick={() => {
              onApply([...new Set([...lockedKeys, ...draftVisible])], draftSize)
              setOpen(false)
            }}
          >
            {t('common.confirm')}
          </Button>
          <Button size="small" onClick={() => setOpen(false)}>
            {t('common.cancel')}
          </Button>
        </Space>
      </div>
    </div>
  )

  return (
    <Popover
      title={t('common.tableSettings')}
      trigger="click"
      open={open}
      onOpenChange={setOpen}
      placement="bottomRight"
      content={content}
    >
      <SettingOutlined
        title={t('common.tableSettings')}
        onClick={(e) => e.stopPropagation()}
        style={{ marginLeft: 8, color: '#8c8c8c', cursor: 'pointer' }}
      />
    </Popover>
  )
}
