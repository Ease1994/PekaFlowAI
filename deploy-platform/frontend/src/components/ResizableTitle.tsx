import { useEffect, useRef, type CSSProperties, type ReactNode, type ThHTMLAttributes } from 'react'
import { t } from '@/i18n'

const HANDLE: CSSProperties = {
  position: 'absolute',
  right: 0,
  top: 0,
  width: 8,
  height: '100%',
  userSelect: 'none',
  zIndex: 2,
}

interface Props extends ThHTMLAttributes<HTMLTableCellElement> {
  width?: number
  onResize?: (width: number) => void
  children?: ReactNode
}

/** 表头右侧拖条改列宽。点拖条不会触发表头排序。外观仍是旧表头，只换实心黑指针。 */
export default function ResizableTitle({ width, onResize, children, style, ...rest }: Props) {
  const thRef = useRef<HTMLTableCellElement>(null)
  const startX = useRef(0)
  const startW = useRef(0)
  const dragging = useRef(false)

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current || !onResize) return
      const next = Math.max(56, startW.current + (e.clientX - startX.current))
      onResize(next)
    }
    const onUp = () => {
      dragging.current = false
      document.body.classList.remove('rp-col-resizing')
      document.body.style.userSelect = ''
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    return () => {
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      document.body.classList.remove('rp-col-resizing')
    }
  }, [onResize])

  if (!onResize) {
    return (
      <th {...rest} style={style}>
        {children}
      </th>
    )
  }

  return (
    <th ref={thRef} {...rest} style={{ ...style, position: 'relative' }}>
      {children}
      <span
        role="separator"
        aria-orientation="vertical"
        aria-label={t('common.resizeCol')}
        className="rp-col-resize-handle"
        style={HANDLE}
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => {
          e.preventDefault()
          e.stopPropagation()
          dragging.current = true
          startX.current = e.clientX
          startW.current = width || thRef.current?.offsetWidth || 120
          document.body.classList.add('rp-col-resizing')
          document.body.style.userSelect = 'none'
        }}
      />
    </th>
  )
}
