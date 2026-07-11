import type { Funnel } from '../lib/api'

/* The reversal funnel: five proportional stage bars with hatched drop segments —
   the hatch is the part that DIDN'T make it past each gate. All numerals here are
   structural counts (stage counts, pool cap), not statistics, so they render as
   plain text by design. The null empty state ("no funnel recorded yet") belongs
   to the caller — this component takes a real snapshot. */

const STAGES = ['detected', 'confirmed', 'fresh', 'actionable', 'surfaced'] as const

export function FunnelBar({ funnel }: { funnel: Funnel }) {
  const max = Math.max(funnel.detected, 1)

  const rows: { stage: (typeof STAGES)[number]; count: number; drop: number }[] = []
  let prev: number | null = null
  for (const stage of STAGES) {
    const count = funnel[stage]
    rows.push({ stage, count, drop: prev === null ? 0 : Math.max(prev - count, 0) })
    prev = count
  }

  return (
    <div className="funnel">
      <div className="funnel-date">latest digest · {funnel.run_date}</div>
      {rows.map(({ stage, count, drop }) => (
        <div key={stage} className="funnel-row">
          <span className="funnel-label">
            {stage}
            {stage === 'actionable' && !funnel.already_ran_checked && (
              <span
                className="funnel-unchecked"
                title="no live-quote source at digest time — the already-ran drop was skipped"
              >
                (unchecked)
              </span>
            )}
          </span>
          <span className="funnel-track">
            <span
              className="funnel-fill"
              style={{ width: `${(count / max) * 100}%` }}
            />
            {drop > 0 && (
              <span
                className="funnel-drop"
                style={{
                  left: `${(count / max) * 100}%`,
                  width: `${(drop / max) * 100}%`,
                }}
              />
            )}
          </span>
          <span className="funnel-count">{count}</span>
        </div>
      ))}
      {funnel.overflow.length > 0 && (
        <div className="funnel-overflow">
          <span className="funnel-note">lost the top-5/sector race:</span>
          {funnel.overflow.map((t) => (
            <span key={t} className="funnel-chip">
              {t}
            </span>
          ))}
        </div>
      )}
      <div className="funnel-note">
        fresh = strength bar + cooldown + pool cap ({funnel.pool_n})
      </div>
    </div>
  )
}
