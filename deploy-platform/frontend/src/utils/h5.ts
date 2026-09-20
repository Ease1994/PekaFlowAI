/**
 * 登录后回跳只接受站内相对路径，避免 ?next=https:// 被拿去钓鱼。
 */
export function safeNextPath(raw: string | null | undefined, fallback = '/'): string {
  const value = (raw || '').trim()
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) return fallback
  if (value.startsWith('/login')) return fallback
  if (value.includes('://')) return fallback
  return value
}

/** 企微内置浏览器。这里 window.open 常被拦，OAuth 必须当前页跳走。 */
export function isWecomWebView() {
  return /wxwork/i.test(navigator.userAgent || '')
}
