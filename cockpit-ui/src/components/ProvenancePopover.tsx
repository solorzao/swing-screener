import { useEffect, useRef } from 'react'
import type { Stat } from '../lib/api'
import { costGlyph, fmtStatValue } from '../lib/fmt'

/* ProvenancePopover — the reusable "where did this number come from" panel
   (plan Task 19). It renders the FULL provenance of a Stat: the point value,
   the sample (n rows across n_clusters), the 95% interval and how that bound was
   built, and the four provenance stamps the app argues about — cost level,
   corpus id, facet, unit. Wired into StatChip (StatChip.tsx), so clicking ANY
   StatChip anywhere (Task 17's cohort chip included) opens this.

   HONESTY POSTURE (the whole reason this exists): every field is read STRAIGHT
   off the Stat — nothing is fabricated. A null cost_level renders "not measured"
   (the source never stamped one), a null corpus_id renders "not stamped", a
   non-finite CI edge renders "not measured", and thin_clusters=true is a LOUD
   flag (the interval fell back to an IID bound — treat it as soft). It never
   invents a number to fill a gap. */

/** A CI edge off the wire: real numbers format with the unit, ±inf / NaN read as
 * an honest "not measured" (JSON can carry a huge sentinel; a lamp must not
 * render it as a real bound). */
function edge(v: number, unit: string): string {
  return Number.isFinite(v) ? fmtStatValue(v, unit) : 'not measured'
}

export function ProvenancePopover({
  stat,
  label,
  onClose,
}: {
  stat: Stat
  /** The chip's own label, echoed in the popover head so a table of chips is
   * unambiguous about WHICH number this describes. */
  label?: string
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)

  // Dismiss on Escape or a click/tap outside the panel — the same lightweight
  // pattern the DISARM popover uses, self-contained so every StatChip inherits
  // it. `mousedown` (not click) so a press that starts outside closes before a
  // nested control can eat the click.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    const onDown = (e: MouseEvent) => {
      if (ref.current !== null && !ref.current.contains(e.target as Node)) onClose()
    }
    window.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [onClose])

  const boundType = stat.thin_clusters
    ? 'IID fallback — thin clusters, treat as soft'
    : 'clustered 95%'

  return (
    <div className="prov-pop" role="dialog" aria-label="statistic provenance" ref={ref}>
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
    </div>
  )
}
