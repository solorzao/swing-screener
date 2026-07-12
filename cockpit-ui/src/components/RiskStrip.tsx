import type { ReactNode } from 'react'
import type { Polled, PositionRow, Positions } from '../lib/api'
import { dashOr, fmtClock, fmtPct, fmtR, fmtUsd } from '../lib/fmt'
import { BracketLamp } from './BracketLamp'
import { CapGauge } from './CapGauge'
import { PositionLamp } from './PositionLamp'

/* Zone B — RISK (the Mission Control top band, design doc Zone B): every open
   position as one strip — badge lamp, planned $ risk, current R multiple,
   distance-to-stop bar, BracketLamp — plus the three cap gauges in a compact
   row. "Open positions" is the wire's open set: real (manual) Trade rows AND
   live broker-owned rows — the design's "open real positions" predates the
   live-row wire spec, and a risk band that hid an actual broker position
   while claiming FLAT would be lying about exposure.

   The FLAT claim is load-bearing (plan Task 16): "FLAT — no exposure" renders
   ONLY from a fresh successful fetch. On a fetch error, strips keep the
   stale-dim treatment (last good data, dimmed, labeled) but an EMPTY stale
   read degrades to a dashed "risk state unknown" — a stale empty list must
   never pass for a live flat book. Prices are last COMPLETED daily closes;
   the caption says so. */

/** Distance-to-stop bar scale: 15% of price renders as a full bar. Arbitrary
 * but fixed, so strips are comparable to each other; the label carries the
 * exact number. */
const STOP_BAR_FULL = 0.15

/** Planned dollar risk to the stop ((entry − stop) × size), or null when the
 * wire can't say: a live row with no ExecutionLog shares join, or a pending
 * entry. Never a guessed number. */
function riskDollars(row: PositionRow): number | null {
  if (row.entry_price === null || row.size === null) return null
  return (row.entry_price - row.stop) * row.size
}

function Strip({ row }: { row: PositionRow }) {
  const risk = riskDollars(row)
  // A stop at/above entry has no positive risk to size — the degenerate case
  // the server nulls realized_r for. Show the dash (never "risk −$1,234", a
  // minus after the $ that reads as a loss, not a missing measurement).
  const riskDegenerate = risk !== null && risk <= 0
  const r = row.pl?.r_multiple ?? null
  const dist = row.pl?.dist_to_stop_pct ?? null
  return (
    <div className="rs-strip">
      <PositionLamp badge={row.badge} />
      <span className="rs-ticker mono">{row.ticker}</span>
      {row.kind === 'live' && <span className="rs-kind">live</span>}
      <span
        className="rs-cell mono"
        title={
          riskDegenerate
            ? 'stop at/above entry — no positive risk to size'
            : 'planned risk to the stop: (entry − stop) × size — dollars at 1R'
        }
      >
        {risk === null || riskDegenerate ? 'risk —' : `risk ${fmtUsd(risk, 0)}`}
      </span>
      <span className="rs-cell mono" title="current R multiple at the last close">
        {dashOr(r, fmtR)}
      </span>
      <div
        className={dist === null ? 'rs-stopbar rs-indet' : 'rs-stopbar'}
        title={
          dist === null
            ? 'distance to stop not computable — no usable quote, or the row degraded (pl: null)'
            : `${fmtPct(dist)} of price between the last close and the stop`
        }
      >
        {dist !== null && (
          <div
            className="rs-stopfill"
            style={{
              width: `${Math.min(Math.max(dist, 0) / STOP_BAR_FULL, 1) * 100}%`,
            }}
          />
        )}
      </div>
      <span className="rs-cell mono">{dashOr(dist, fmtPct)}</span>
      <BracketLamp state={row.bracket} />
    </div>
  )
}

export function RiskStrip({ polled }: { polled: Polled<Positions> }) {
  const { data, error } = polled
  const fresh = error === null && data !== null

  let body: ReactNode
  if (data === null) {
    body =
      error === null ? (
        <div className="panel-wait">waiting for first fetch…</div>
      ) : (
        <div className="rs-noclaim">risk unavailable — {error}</div>
      )
  } else if (data.open.length === 0) {
    // FLAT only from a FRESH fetch; a stale empty read is an unknown, not a claim.
    body = fresh ? (
      <div className="rs-flat">FLAT — no exposure</div>
    ) : (
      <div className="rs-noclaim">
        risk state unknown — last fetch failed ({error}); the last good read was
        flat, but that is not claimed as current
      </div>
    )
  } else {
    body = (
      <div className={fresh ? 'rs-body' : 'rs-body stale'}>
        <div className="rs-strips">
          {data.open.map((row) => (
            <Strip
              key={row.kind === 'real' ? `real-${row.trade_id}` : `live-${row.paper_id}`}
              row={row}
            />
          ))}
        </div>
        <div className="rs-caps">
          <CapGauge label="daily notional" cap={data.caps.notional} kind="usd" />
          <CapGauge label="daily loss (R)" cap={data.caps.loss_r} kind="r" />
          <CapGauge label="concurrent" cap={data.caps.concurrent} kind="count" />
        </div>
      </div>
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        RISK
        <span className="panel-caption">
          prices as of last close
          {data !== null && ` · quotes ${fmtClock(data.quotes_as_of)}`}
        </span>
      </div>
      {error !== null && data !== null && data.open.length > 0 && (
        <div className="panel-error">showing last good data · {error}</div>
      )}
      {body}
    </section>
  )
}
