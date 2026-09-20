import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { get } from '@/api/client'
import { useT } from '@/i18n'

/** 公开品牌信息：登录页不要求登录也能拉。侧栏图标是前端固定资源，不走配置。 */
export interface PlatformBranding {
  /** 侧栏 / 登录页 / 浏览器标题 */
  display_name: string
  /** 有图才有地址；空表示顶栏左侧空白 */
  header_image_url: string
  /** 空则不显示顶栏通知横幅 */
  header_notice_text: string
  /** 横幅醒目色：red / orange / gold / magenta / volcano */
  header_notice_color: string
}

const FALLBACK_NAME = 'PekaFlowAI'
/** 出厂名（含旧版称呼）。设置里没改过时，侧栏显示当前产品名。 */
const LEGACY_FACTORY_NAMES = new Set([
  'PekaFlowAI',
  'PekaFlow',
  '发布部署平台',
  '發佈部署平台',
  'Release Platform',
  'Release-Plattform',
  'リリースプラットフォーム',
  'रिलीज़ प्लेटफ़ॉर्म',
  'Plataforma de release',
])
/** 出厂管理员姓名。设置里没改过时，随界面语言显示。 */
const FACTORY_ADMIN_NAME = '系统管理员'
/** 与侧栏同一构图的固定网站图标，不能在平台设置里改。 */
export const PLATFORM_FAVICON = '/favicon.svg'

/** 顶栏通知可选的醒目色：实心底、浅字，保证一眼能看见。 */
export const HEADER_NOTICE_COLORS = {
  red: { label: 'brand.red', bg: '#cf1322', fg: '#fff' },
  orange: { label: 'brand.orange', bg: '#d46b08', fg: '#fff' },
  gold: { label: 'brand.gold', bg: '#d4b106', fg: '#1f1f1f' },
  magenta: { label: 'brand.magenta', bg: '#c41d7f', fg: '#fff' },
  volcano: { label: 'brand.volcano', bg: '#d4380d', fg: '#fff' },
} as const

export type HeaderNoticeColor = keyof typeof HEADER_NOTICE_COLORS

/**
 * 读取平台显示名、顶栏图和通知横幅。
 * 失败时用内置默认值，避免登录页因为配置接口挂了整页空白。
 */
export function usePlatformBranding() {
  return useQuery({
    queryKey: ['platform-branding'],
    queryFn: () => get<PlatformBranding>('/settings/branding', undefined, { skipErrorToast: true }),
    staleTime: 60_000,
  })
}

/** 空值或出厂名（含升级前的旧称呼）视为未自定义。 */
export function isFactoryProductName(raw?: string): boolean {
  const text = (raw || '').trim()
  return !text || LEGACY_FACTORY_NAMES.has(text)
}

/** 把查询结果收成一定有值的显示名。出厂名随 layout.productName。 */
export function resolveDisplayName(
  data: PlatformBranding | undefined,
  translate?: (key: string) => string,
): string {
  const raw = (data?.display_name || '').trim()
  if (isFactoryProductName(raw)) {
    return translate ? translate('layout.productName') : FALLBACK_NAME
  }
  return raw
}

/** 出厂「系统管理员」随界面语言走；用户自己改过的名字保持原样。 */
export function resolveUserDisplayName(
  user: { display_name?: string; username?: string } | null | undefined,
  translate: (key: string) => string,
): string {
  const raw = (user?.display_name || '').trim()
  if (raw === FACTORY_ADMIN_NAME) return translate('layout.bootstrapAdmin')
  return raw || user?.username || translate('layout.notSignedIn')
}

/** 不在预设里的颜色回落到红色警示。 */
export function resolveNoticeColor(raw: string | undefined): HeaderNoticeColor {
  if (raw && raw in HEADER_NOTICE_COLORS) return raw as HeaderNoticeColor
  return 'red'
}

/**
 * 浏览器标题跟平台显示名走；标签页图标永远用侧栏那张固定图。
 */
export function ApplyPlatformBranding() {
  const { data } = usePlatformBranding()
  const t = useT()
  const name = resolveDisplayName(data, t)

  useEffect(() => {
    document.title = name
    let link = document.querySelector<HTMLLinkElement>("link[rel='icon']")
    if (!link) {
      link = document.createElement('link')
      link.rel = 'icon'
      document.head.appendChild(link)
    }
    link.type = 'image/svg+xml'
    link.href = PLATFORM_FAVICON
  }, [name])

  return null
}
