import { useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { Stat } from '../lib/api'
import { costGlyph, fmtStatValue } from '../lib/fmt'
import { ProvenancePopover } from './ProvenancePopover'

/* StatChip is the ONLY numeric renderer in the app. Its props type takes a full
   Stat object — there is no code path that accepts a bare number, so a statistic
   without provenance is unrepresentable (design rule 1).

   Clicking a chip opens the ProvenancePopover (Task 19): the full provenance of
   the number lives one click away everywhere a StatChip renders — Task 17's
   cohort chip, the leaderboard, the settlement cards. The chip's visible face is
   unchanged; the whole face becomes ONE button so the affordance is keyboard-
   reachable, and BOTH branches (the no-read badge and the full chip) open it —
   even an n<5 chip has a facet / cost / sample worth inspecting. */

/* The API's ci_low..ci_high is the 95% interval. The 80/50% bands are narrower
   fractions of it, centered on the point estimate — normal z-ratios, a simple
   linear map: z(80)/z(95) = 1.282/1.960, z(50)/z(95) = 0.674/1.960. */
const BAND_FRACTIONS = [
  { frac: 1.0, cls: 'ci-band' },
  { frac: 0.654, cls: 'ci-band b80' },
  { frac: 0.344, cls: 'ci-band b50' },
]

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x))
}

export function StatChip({ stat, label }: { stat: Stat; label?: string }) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLSpanElement>(null)
  const popId = useId()
  const labelEl =
    label !== undefined ? <span className="statchip-label">{label}</span> : null

  // The provenance affordance wraps whichever face renders below. The wrap is
  // the popover's fixed-position anchor AND its outside-click "inside" region
  // (button + a portaled popover positioned off it), so a click on the button
  // toggles it closed rather than reading as an outside dismissal. The popover
  // is role="tooltip" (a read-only disclosure, not a modal), so the trigger
  // advertises it via aria-describedby, never aria-haspopup. A closed chip is
  // visually identical to before.
  const wrap = (face: ReactNode) => (
    <span className="statchip-wrap" ref={wrapRef}>
      <button
        type="button"
        className="statchip-btn"
        aria-expanded={open}
        aria-describedby={open ? popId : undefined}
        title="show this number's provenance"
        onClick={() => setOpen((v) => !v)}
      >
        {face}
      </button>
      {open && (
        <ProvenancePopover
          stat={stat}
          label={label}
          id={popId}
          triggerRef={wrapRef}
          onClose={() => setOpen(false)}
        />
      )}
    </span>
  )

  // n-gating, rule 2: below 5 there is no read at all — a badge, never a value.
  if (stat.n < 5) {
    return wrap(
      <span className="statchip">
        {labelEl}
        <span className="noread-badge">n&lt;5 — no read</span>
      </span>,
    )
  }

  const thin = stat.n < 12 // 5–11: value at 45% opacity + THIN chip
  const notMeasured = stat.cost_level === null

  // Tier-gated color (rule 2): saturated green requires facet forward_confirmed,
  // and Phase 1's Stat carries no tier — so nothing here is EVER green. Positive
  // renders --txt, negative --dim; the tier chip (Phase 2) will own the green claim.
  const valueCls = [
    'stat-value',
    stat.value < 0 ? 'neg' : '',
    thin ? 'thin' : '',
    notMeasured ? 'not-measured' : '',
  ]
    .filter(Boolean)
    .join(' ')

  // Linear map of the interval onto the 4px track; each inner band shrinks both
  // sides toward the point position so bands stay nested inside the 95% span.
  const span = stat.ci_high - stat.ci_low
  const pos = span > 0 ? clamp01((stat.value - stat.ci_low) / span) : 0.5

  return wrap(
    <span className="statchip">
      {labelEl}
      <span className="stat-block">
        <span className="stat-line">
          {thin && <span className="thin-chip">THIN</span>}
          <span
            className={valueCls}
            title={
              notMeasured
                ? 'cost level not stamped at source — honest unknown'
                : undefined
            }
          >
            {fmtStatValue(stat.value, stat.unit)}
          </span>
          <sup className="stat-n">
            n={stat.n.toLocaleString('en-US')}·{stat.n_clusters.toLocaleString('en-US')}c
          </sup>
          {!notMeasured && stat.cost_level !== null && (
            <sub className="stat-cost">{costGlyph(stat.cost_level)}</sub>
          )}
        </span>
        <span className="ci-track" aria-hidden="true">
          {stat.thin_clusters ? (
            <span className="ci-dotted" />
          ) : (
            BAND_FRACTIONS.map(({ frac, cls }) => {
              const left = pos * (1 - frac)
              const right = pos + frac * (1 - pos)
              return (
                <span
                  key={cls}
                  className={cls}
                  style={{ left: `${left * 100}%`, width: `${(right - left) * 100}%` }}
                />
              )
            })
          )}
          <span className="ci-tick" style={{ left: `calc(${pos * 100}% - 0.75px)` }} />
        </span>
      </span>
    </span>,
  )
}
