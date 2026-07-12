import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { createPortal } from 'react-dom'
import type { Stat } from '../lib/api'
import { costGlyph, fmtStatValue } from '../lib/fmt'

/* ProvenancePopover — the reusable "where did this number come from" panel
   (plan Task 19). It renders the FULL provenance of a Stat: the point value,
   the sample (n rows across n_clusters), the 95% interval and how that bound was
   built, and the four provenance stamps the app argues about — cost level,
   corpus id, facet, unit. Wired into StatChip (StatChip.tsx), so clicking ANY
   StatChip anywhere (Task 17's cohort chip included) opens this.

   POSITIONING: PORTALED to document.body and position:fixed, computed from the
   trigger's viewport rect — because every .panel is overflow:hidden, an in-flow
   absolute popover clips the moment a chip sits at a panel edge (the last
   cohort/leaderboard row, a bottom KPI cell). Fixed + portal escapes every
   ancestor's clip. It opens UPWARD when the trigger sits in the lower half of
   the viewport (else downward) and clamps into the viewport both ways, so it is
   always fully visible. A scroll or resize detaches a fixed panel from its
   moving trigger, so either closes it.

   HONESTY POSTURE (the whole reason this exists): every field is read STRAIGHT
   off the Stat — nothing is fabricated. A null cost_level renders "not measured"
   (the source never stamped one), a null corpus_id renders "not stamped", a
   non-finite CI edge renders "not measured", and thin_clusters=true is a LOUD
   flag (the interval fell back to an IID bound — treat it as soft). It never
   invents a number to fill a gap. */

const MARGIN = 8 // keep this far from every viewport edge
const GAP = 6 // between the trigger and the panel

/** A CI edge off the wire: real numbers format with the unit, ±inf / NaN read as
 * an honest "not measured" (JSON can carry a huge sentinel; a lamp must not
 * render it as a real bound). */
function edge(v: number, unit: string): string {
  return Number.isFinite(v) ? fmtStatValue(v, unit) : 'not measured'
}

export function ProvenancePopover({
  stat,
  label,
  id,
  triggerRef,
  onClose,
}: {
  stat: Stat
  /** The chip's own label, echoed in the popover head so a table of chips is
   * unambiguous about WHICH number this describes. */
  label?: string
  /** The tooltip's id — the trigger points aria-describedby here so a screen
   * reader announces the provenance while it is open. */
  id: string
  /** The chip wrapper (the button lives inside it): the fixed-position anchor
   * AND the "inside" test for the outside-click dismissal — a click on the
   * trigger counts as inside, so the button's own toggle closes the popover
   * instead of the outside handler racing it. */
  triggerRef: RefObject<HTMLElement | null>
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)

  // Measure AFTER the panel renders (its size drives the up/down choice and the
  // clamp) but BEFORE paint, so the pre-positioned frame at 0,0 never shows.
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

  // Dismiss on Escape, on a click/tap OUTSIDE the panel and its trigger, or on a
  // scroll/resize (which detaches a fixed panel from its trigger). NOTE: the
  // mousedown-outside dismissal is introduced HERE — the DISARM popover
  // dismisses only via Escape + explicit buttons, never an outside click. The
  // trigger-inside check (via triggerRef) is what lets the chip's own button
  // toggle the popover closed instead of the outside handler racing its click.
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
    document.addEventListener('mousedown', onDown)
    window.addEventListener('resize', onShift)
    window.addEventListener('scroll', onShift, true) // capture: any scroll container
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('resize', onShift)
      window.removeEventListener('scroll', onShift, true)
    }
  }, [onClose, triggerRef])

  const boundType = stat.thin_clusters
    ? 'IID fallback — thin clusters, treat as soft'
    : 'clustered 95%'

  return createPortal(
    <div
      className="prov-pop"
      role="tooltip"
      id={id}
      ref={ref}
      style={{
        top: pos?.top ?? 0,
        left: pos?.left ?? 0,
        // Hidden until measured (see the layout effect) so the pre-positioned
        // frame at 0,0 never paints.
        visibility: pos === null ? 'hidden' : 'visible',
      }}
    >
      <div className="prov-head">
        PROVENANCE{label !== undefined && <span className="prov-head-lab"> · {label}</span>}
      </div>

      <dl className="prov-grid">
        <dt>value</dt>
        <dd className="mono">{fmtStatValue(stat.value, stat.unit)}</dd>

        <dt>sample</dt>
        <dd className="mono">
          n={stat.n.toLocaleString('en-US')} across{' '}
          {stat.n_clusters.toLocaleString('en-US')} cluster
          {stat.n_clusters === 1 ? '' : 's'}
        </dd>

        <dt>95% CI</dt>
        <dd className="mono">
          {edge(stat.ci_low, stat.unit)} … {edge(stat.ci_high, stat.unit)}
        </dd>

        <dt>bound</dt>
        <dd className={stat.thin_clusters ? 'prov-warn' : undefined}>{boundType}</dd>

        <dt>cost level</dt>
        <dd className="mono">
          {stat.cost_level === null ? (
            <em className="prov-na" title="cost level not stamped at source — an honest unknown, never a default">
              not measured
            </em>
          ) : (
            costGlyph(stat.cost_level)
          )}
        </dd>

        <dt>corpus</dt>
        <dd className="mono">
          {stat.corpus_id === null ? (
            <em className="prov-na" title="corpus id not stamped at source">
              not stamped
            </em>
          ) : (
            stat.corpus_id
          )}
        </dd>

        <dt>facet</dt>
        <dd className="mono">{stat.facet}</dd>

        <dt>unit</dt>
        <dd className="mono">{stat.unit}</dd>
      </dl>

      {stat.thin_clusters && (
        <div className="prov-flag" role="note">
          thin clusters — the interval is an IID fallback, not a clustered bound;
          it can look tighter than the evidence warrants
        </div>
      )}
    </div>,
    document.body,
  )
}
