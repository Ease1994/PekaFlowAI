import { t } from '@/i18n'

export interface Notice {
  id: number
  title: string
  content: string
  kind: string
  link: string
  related_type: string
  related_id: number | null
  is_read: boolean
  created_at: string
}

export function kindTitle(kind: string) {
  if ((kind || '').startsWith('release.approval') || (kind || '').startsWith('pm.confirm')) {
    return kind.includes('pending') ? t('notify.pending') : t('notify.releaseSys')
  }
  if ((kind || '').startsWith('release')) return t('notify.releaseSys')
  if ((kind || '').startsWith('access')) return t('notify.accessApply')
  return t('notify.system')
}

/**
 * 铃铛「查看详情」按种类进该办事的页面。
 * 库里旧的待审通知 link 仍指向执行画布，不能信。
 */
export function noticeHref(n: Notice): string {
  const kind = n.kind || ''
  const id = n.related_id
  if (kind === 'release.approval.pending') {
    return id ? `/approvals?release_id=${id}` : '/approvals'
  }
  if (kind === 'pm.confirm.pending') {
    return '/approvals?tab=pm'
  }
  if (kind === 'access.apply') {
    return id ? `/permissions?tab=pending&id=${id}` : '/permissions?tab=pending'
  }
  if (kind === 'access.review') {
    return '/permissions?tab=history'
  }
  return n.link || (n.id ? `/notifications?id=${n.id}` : '/notifications')
}

/** 相对时间。界面语言跟 t() 走。 */
export function fromNow(iso: string) {
  if (!iso) return ''
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return iso.replace('T', ' ').slice(0, 16)
  const diff = Date.now() - ts
  const min = Math.floor(diff / 60000)
  if (min < 1) return t('notify.justNow')
  if (min < 60) return t('notify.minutesAgo', { n: min })
  const hour = Math.floor(min / 60)
  if (hour < 24) return t('notify.hoursAgo', { n: hour })
  const day = Math.floor(hour / 24)
  if (day === 1) {
    const d = new Date(ts)
    return t('notify.yesterday', {
      time: `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`,
    })
  }
  if (day < 7) return t('notify.daysAgo', { n: day })
  return iso.replace('T', ' ').slice(0, 16)
}
