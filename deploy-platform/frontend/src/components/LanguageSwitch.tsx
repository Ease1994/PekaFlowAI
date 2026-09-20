import { Dropdown, Button } from 'antd'
import { GlobalOutlined } from '@ant-design/icons'
import { LOCALE_OPTIONS, useI18nStore, useT } from '@/i18n'

/**
 * 顶栏 / 登录页的语言切换。选项用各语言自己的名字，不随界面语言改。
 */
export default function LanguageSwitch() {
  const locale = useI18nStore((s) => s.locale)
  const setLocale = useI18nStore((s) => s.setLocale)
  const t = useT()
  const current = LOCALE_OPTIONS.find((item) => item.value === locale)

  return (
    <Dropdown
      trigger={['click']}
      menu={{
        selectable: true,
        selectedKeys: [locale],
        onClick: ({ key }) => setLocale(key as typeof locale),
        items: LOCALE_OPTIONS.map((item) => ({
          key: item.value,
          label: item.nativeLabel,
        })),
      }}
    >
      <Button type="text" icon={<GlobalOutlined />} aria-label={t('common.language')}>
        <span className="rp-lang-label">{current?.nativeLabel ?? t('common.language')}</span>
      </Button>
    </Dropdown>
  )
}
