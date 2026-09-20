/**
 * 界面语言：localStorage 记住选择，缺省按浏览器语言，再缺省简体中文。
 * 文案用点号路径取嵌套字段；缺键回落到简体。
 */
import { useCallback } from 'react'
import { create } from 'zustand'
import type { AppLocale, Messages } from './types'
import { zhCN } from './locales/zh-CN'
import { zhTW } from './locales/zh-TW'
import { en } from './locales/en'
import { ja } from './locales/ja'
import { hi } from './locales/hi'
import { ptBR } from './locales/pt-BR'
import { de } from './locales/de'

const STORAGE_KEY = 'rp-locale'

const DICTS: Record<AppLocale, Messages> = {
  'zh-CN': zhCN,
  'zh-TW': zhTW,
  en,
  ja,
  hi,
  'pt-BR': ptBR,
  de,
}

/** 下拉框里显示的语言名（各用自己的文字）。 */
export const LOCALE_OPTIONS: { value: AppLocale; nativeLabel: string }[] = [
  { value: 'zh-CN', nativeLabel: '简体中文' },
  { value: 'zh-TW', nativeLabel: '繁體中文' },
  { value: 'en', nativeLabel: 'English' },
  { value: 'ja', nativeLabel: '日本語' },
  { value: 'hi', nativeLabel: 'हिन्दी' },
  { value: 'pt-BR', nativeLabel: 'Português (Brasil)' },
  { value: 'de', nativeLabel: 'Deutsch' },
]

const KNOWN = new Set<string>(LOCALE_OPTIONS.map((item) => item.value))

/**
 * 把浏览器语言映射到本产品支持的七种之一。
 * zh-TW / zh-HK 走繁体；pt 走巴西葡语。
 */
export function detectLocale(raw?: string): AppLocale {
  const tag = (raw || '').trim().toLowerCase().replace(/_/g, '-')
  if (KNOWN.has(raw || '')) return raw as AppLocale
  if (tag.startsWith('zh-tw') || tag.startsWith('zh-hk') || tag.startsWith('zh-hant')) return 'zh-TW'
  if (tag.startsWith('zh')) return 'zh-CN'
  if (tag.startsWith('ja')) return 'ja'
  if (tag.startsWith('hi')) return 'hi'
  if (tag.startsWith('pt')) return 'pt-BR'
  if (tag.startsWith('de')) return 'de'
  if (tag.startsWith('en')) return 'en'
  return 'zh-CN'
}

function readStored(): AppLocale {
  try {
    return detectLocale(localStorage.getItem(STORAGE_KEY) || navigator.language)
  } catch {
    return 'zh-CN'
  }
}

type I18nState = {
  /** 当前界面语言。 */
  locale: AppLocale
  /** 切换语言并写入 localStorage，刷新后仍生效。 */
  setLocale: (next: AppLocale) => void
}

export const useI18nStore = create<I18nState>((set) => ({
  locale: readStored(),
  setLocale: (next) => {
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // 无痕模式写不了也要能当场切换
    }
    set({ locale: next })
  },
}))

function lookup(messages: Messages, path: string): string | undefined {
  const parts = path.split('.')
  let cur: unknown = messages
  for (const part of parts) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[part]
  }
  return typeof cur === 'string' ? cur : undefined
}

/**
 * 取当前语言的句子。变量写成 `{name}`。
 */
export function t(key: string, vars?: Record<string, string | number>): string {
  const locale = useI18nStore.getState().locale
  let text = lookup(DICTS[locale], key) ?? lookup(DICTS['zh-CN'], key) ?? key
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`{${name}}`).join(String(value))
    }
  }
  return text
}

/** 组件里用：订阅 locale，切换后返回新函数，好让 useMemo 重新算菜单。 */
export function useT(): (key: string, vars?: Record<string, string | number>) => string {
  const locale = useI18nStore((s) => s.locale)
  return useCallback(
    (key: string, vars?: Record<string, string | number>) => t(key, vars),
    [locale],
  )
}

/**
 * 按当前界面语言格式化时间。
 * 空值返回空串；解析失败原样返回，避免把坏数据显示成 Invalid Date。
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const locale = useI18nStore.getState().locale
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString(locale, { hour12: false })
}

export type { AppLocale, Messages }
