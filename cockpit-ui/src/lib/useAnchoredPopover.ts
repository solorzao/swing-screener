import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

/* useAnchoredPopover — the portal-friendly anchored-positioning + dismissal
   machinery lifted verbatim from ProvenancePopover (which stays its regression
   oracle). Every .panel is overflow:hidden, so an in-flow popover clips the
   moment its trigger sits at a panel edge; the fix is to portal the popover to
   document.body and position:fixed it, computed from the trigger's viewport rect.

   Measures the popover AFTER it renders (its size drives the up/down flip and the
   clamp) but BEFORE paint, so the pre-positioned 0,0 frame never shows — the
   consumer hides itself while `pos` is null. Opens UPWARD when the trigger sits in
   the lower half of the viewport (else downward) and clamps into the viewport both
   ways. A scroll or resize detaches a fixed panel from its moving trigger, so
   either dismisses; Escape always dismisses; a mousedown outside the panel and its
   trigger dismisses UNLESS dismissOnOutside is false (DISARM's popover dismisses
   escape-only — the opt-out preserves that). */

const MARGIN = 8 // keep this far from every viewport edge
const GAP = 6 // between the trigger and the panel

export interface AnchoredPos {
  top: number
  left: number
}

export function useAnchoredPopover<T extends HTMLElement>(
  triggerRef: RefObject<T | null>,
  onClose: () => void,
  { dismissOnOutside = true }: { dismissOnOutside?: boolean } = {},
): { ref: RefObject<HTMLDivElement | null>; pos: AnchoredPos | null } {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<AnchoredPos | null>(null)

  useLayoutEffect(() => {
    const trigger = triggerRef.current
    const pop = ref.current
    if (trigger === null || pop === null) return
    const t = trigger.getBoundingClientRect()
    const w = pop.offsetWidth
    const h = pop.offsetHeight
    const vw = window.innerWidth
    const vh = window.innerHeight
    const openUp = t.top + t.height / 2 > vh / 2
    let top = openUp ? t.top - h - GAP : t.bottom + GAP
    top = Math.max(MARGIN, Math.min(top, vh - h - MARGIN))
    const left = Math.max(MARGIN, Math.min(t.left, vw - w - MARGIN))
    setPos({ top, left })
  }, [triggerRef])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (ref.current?.contains(target)) return
      if (triggerRef.current?.contains(target)) return
      onClose()
    }
    const onShift = () => onClose()
    window.addEventListener('keydown', onKey)
    if (dismissOnOutside) document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onShift)
    window.addEventListener('scroll', onShift, true) // capture: any scroll container
    return () => {
      window.removeEventListener('keydown', onKey)
      if (dismissOnOutside) document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onShift)
      window.removeEventListener('scroll', onShift, true)
    }
  }, [onClose, triggerRef, dismissOnOutside])

  return { ref, pos }
}
