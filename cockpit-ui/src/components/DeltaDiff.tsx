import type { KnobValue, ProposalDeltaRow } from '../lib/api'

/* DeltaDiff — a proposal's delta as incumbent → proposed rows (Task 18). The
   backend resolves `current` off the live StrategyConfig for REAL fields only;
   an unknown knob's `current` is null (never a blind getattr). A null current is
   the loud "unmapped knob" — a delta key that names no config field cannot
   change the book (a dead knob), which is exactly the kind of thing the human
   deciding on this proposal needs to see. Values ride as-is: booleans spelled
   out, numbers/strings verbatim (JSON-native by the store contract). */

function fmtKnob(v: KnobValue): string {
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  return String(v)
}

export function DeltaDiff({ rows }: { rows: ProposalDeltaRow[] }) {
  if (rows.length === 0) {
    return <div className="dd-empty">empty delta — no knobs changed</div>
  }
  return (
    <div className="dd">
      {rows.map((r) => {
        const unmapped = r.current === null
        return (
          <div key={r.knob} className="dd-row">
            <span className="dd-knob mono">{r.knob}</span>
            <span className="dd-vals mono">
              <span className="dd-cur">
                {unmapped ? '—' : fmtKnob(r.current as KnobValue)}
              </span>
              <span className="dd-arrow" aria-hidden="true">
                →
              </span>
              <span className="dd-prop">{fmtKnob(r.proposed)}</span>
            </span>
            {unmapped && (
              <span
                className="dd-unmapped"
                title="this delta key names no StrategyConfig field — it cannot change the book (a dead knob)"
              >
                unmapped knob
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
