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
   ways. A resize, or a scroll that MOVES THE TRIGGER (the document, or one of the
   trigger's own ancestor scroll containers), detaches a fixed panel from its
   trigger and so dismisses — an unrelated container scrolling itself does not (see
   onScroll); Escape always dismisses; a mousedown outside the panel and its trigger
   dismisses UNLESS dismissOnOutside is false (DISARM's popover dismisses
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
    /* A scroll only detaches this fixed panel if it actually MOVED the trigger, so
       dismiss only when the scrolling container is the document or an ancestor of
       the trigger. The listener is capture-phase (scroll does not bubble) and so
       sees EVERY scroll container in the page — including Zone E's event ticker,
       which marquees its own scrollLeft ~36x/s in a rAF loop (EventTicker.tsx).
       Dismissing on those closed every popover ~28ms after it opened, but only once
       the ticker had enough events to overflow — which is why it reproduced with a
       live feed and never on an empty dev server. Scrolling INSIDE the popover is
       likewise not a detach, and is now ignored for free. */
    const onScroll = (e: Event) => {
      const trigger = triggerRef.current
      const target = e.target as Node | null
      if (trigger !== null && target !== null && !target.contains(trigger)) return
      onClose()
    }
    const onResize = () => onClose()
    window.addEventListener('keydown', onKey)
    if (dismissOnOutside) document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onResize)
    window.addEventListener('scroll', onScroll, true) // capture: any scroll container
    return () => {
      window.removeEventListener('keydown', onKey)
      if (dismissOnOutside) document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onResize)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [onClose, triggerRef, dismissOnOutside])

  return { ref, pos }
}
