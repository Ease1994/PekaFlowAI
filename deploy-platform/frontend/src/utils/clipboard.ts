import { message } from 'antd'
import { t } from '@/i18n'

/**
 * 复制文本到剪贴板。
 *
 * navigator.clipboard 只在安全上下文（HTTPS 或 localhost）下存在，
 * 平台常以 http://内网IP 访问，这时必须退回 execCommand 方案，否则一律复制失败。
 */
export async function copyText(text: string, successTip?: string): Promise<boolean> {
  const ok = (await writeViaClipboardApi(text)) || writeViaExecCommand(text)
  if (ok) {
    if (successTip !== '') message.success(successTip || t('common.copied'))
  } else {
    message.error(t('common.copyFailed'))
  }
  return ok
}

async function writeViaClipboardApi(text: string): Promise<boolean> {
  if (!navigator.clipboard?.writeText) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function writeViaExecCommand(text: string): boolean {
  const el = document.createElement('textarea')
  el.value = text
  // 移出视口但保持可聚焦，readOnly 避免移动端弹出软键盘
  el.setAttribute('readonly', '')
  el.style.position = 'fixed'
  el.style.top = '-1000px'
  el.style.opacity = '0'
  document.body.appendChild(el)
  try {
    el.select()
    el.setSelectionRange(0, el.value.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(el)
  }
}
