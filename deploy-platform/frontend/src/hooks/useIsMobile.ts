import { useEffect, useState } from 'react'

/** 窄于这个宽度按手机排：侧栏改抽屉。企微 H5 多数落在这个区间。 */
export const MOBILE_MAX_PX = 768

/**
 * 当前是不是窄屏。
 * 用 matchMedia，旋转、分屏会跟着变，避免只在挂载时读一次 innerWidth。
 */
export function useIsMobile() {
  const [mobile, setMobile] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia(`(max-width: ${MOBILE_MAX_PX}px)`).matches : false,
  )
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${MOBILE_MAX_PX}px)`)
    const onChange = () => setMobile(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return mobile
}
