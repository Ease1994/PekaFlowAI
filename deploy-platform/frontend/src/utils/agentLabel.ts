import type { BuildAgent } from '@/api/types'
import { t } from '@/i18n'

/**
 * Job 的「构建资源」字段有三种取值，界面上都不该直接把原始值显示出来：
 *   agent:<id>            指定某台构建机
 *   linux/windows/macos   按系统匹配
 *   any / 其它            任意机器，或人工打的标签
 */
export function agentDisplay(raw: string, agents: BuildAgent[] = []): { icon: string; text: string } {
  const value = (raw || '').trim()
  if (value.startsWith('agent:')) {
    const id = Number(value.slice(6))
    const hit = agents.find((a) => a.id === id)
    // 机器可能已被删除，或调用方没拿到机器列表，这时至少别把 agent:12 露出去
    return { icon: hit ? osIcon(hit.os) : '🖥️', text: hit ? hit.name : t('pipe.agentN', { id }) }
  }
  switch (value) {
    case 'linux':
      return { icon: '🐧', text: t('pipe.anyLinux') }
    case 'windows':
      return { icon: '🪟', text: t('pipe.anyWindows') }
    case 'macos':
      return { icon: '🍎', text: t('pipe.anyMacos') }
    case 'any':
    case '':
      return { icon: '⚙', text: t('pipe.anyMachine') }
    case 'platform':
      return { icon: '☁', text: t('pipe.platformExec') }
    case 'node':
      return { icon: '🖧', text: t('pipe.deployNode') }
    default:
      return { icon: '🏷', text: value }
  }
}

function osIcon(os: string): string {
  return os === 'linux' ? '🐧' : os === 'windows' ? '🪟' : '🍎'
}
