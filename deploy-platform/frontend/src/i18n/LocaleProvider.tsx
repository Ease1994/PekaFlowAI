import { useEffect, type ReactNode } from 'react'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import zhTW from 'antd/locale/zh_TW'
import enUS from 'antd/locale/en_US'
import jaJP from 'antd/locale/ja_JP'
import hiIN from 'antd/locale/hi_IN'
import ptBR from 'antd/locale/pt_BR'
import deDE from 'antd/locale/de_DE'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import 'dayjs/locale/zh-tw'
import 'dayjs/locale/en'
import 'dayjs/locale/ja'
import 'dayjs/locale/hi'
import 'dayjs/locale/pt-br'
import 'dayjs/locale/de'
import type { AppLocale } from './types'
import { useI18nStore } from './index'

const ANTD = {
  'zh-CN': zhCN,
  'zh-TW': zhTW,
  en: enUS,
  ja: jaJP,
  hi: hiIN,
  'pt-BR': ptBR,
  de: deDE,
} as const

const DAYJS: Record<AppLocale, string> = {
  'zh-CN': 'zh-cn',
  'zh-TW': 'zh-tw',
  en: 'en',
  ja: 'ja',
  hi: 'hi',
  'pt-BR': 'pt-br',
  de: 'de',
}

/**
 * 把 Ant Design、dayjs、html lang 绑到当前界面语言。
 */
export default function LocaleProvider({ children }: { children: ReactNode }) {
  const locale = useI18nStore((s) => s.locale)

  useEffect(() => {
    document.documentElement.lang = locale
    dayjs.locale(DAYJS[locale])
  }, [locale])

  return (
    <ConfigProvider locale={ANTD[locale]} theme={{ token: { colorPrimary: '#1677ff' } }}>
      {children}
    </ConfigProvider>
  )
}
