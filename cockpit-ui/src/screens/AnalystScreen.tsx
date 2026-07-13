import { useState } from 'react'
import { POLL_MS, getAnalyst, getAnalysisList, usePolling } from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type {
  AnalystFreshness,
  AnalystPlayType,
  AnalystSpend,
  CalibrationProgress,
  CalibrationRow,
} from '../lib/api'
import { fmtR, fmtUsd } from '../lib/fmt'
import { AnalysisPanel } from '../components/AnalysisPanel'
import { PanelBody } from '../components/PanelBody'

/* Screen 6 — Analyst (plan Task 19): the analyst's calibration report card plus
   the deep-analysis surface (the sixth action).

   HONESTY POSTURE, top to bottom:
   - every R here is the SHADOW BOOK's — scored research-grid calls, no real
     dollars; the wire's `r_basis` says so and this screen labels it once, loud.
   - calibration is Stat-SHAPED, never a bare mean: each grade's mean R rides
     with its n, and a grade with no scored history shows "0 scored · unproven"
     in canonical order (high/medium/low/avoid) — never omitted, never a
     fabricated zero-edge.
   - the calibration-progress bound is UNKNOWN when ci_low is null (its true
     value is −inf below the data floors) — rendered "insufficient data", never
     green, never a made-up number.
   - spend is REAL $ off the wire (today / 7d / 30d), and it names its own
     undercount: NULL-cost rows (the deterministic path, legacy) are counted but
     not summed, so the totals can only under-report. */

/* ---------------- Calibration ---------------- */

const GRADE_LABEL: Record<string, string> = {
  high: 'high',
  medium: 'medium',
  low: 'low',
  avoid: 'avoid',
}

/** One grade's scored record — Stat-shaped: mean R with its n, or the honest
 * "0 scored · unproven" when the grade has no scored history (the calibration
 * wire is grade/n/mean_r only — no CI/cost/corpus — so this is not a full
 * StatChip; it shows the provenance it actually has: n scored, shadow book). */
function CalibrationCell({ row }: { row: CalibrationRow }) {
  if (row.n === 0 || row.mean_r === null) {
    return (
      <span className="cal-unproven" title="no scored calls of this grade yet — the grade is unproven on the shadow book">
        0 scored · unproven
      </span>
    )
  }
  return (
    <span className="mono cal-mean">
      {fmtR(row.mean_r)}
      <sup className="cal-n"> n={row.n.toLocaleString('en-US')}</sup>
    </span>
  )
}

function CalibrationTable({ rows }: { rows: CalibrationRow[] }) {
  return (
    <div className="cal-table-wrap">
      <table className="cal-table">
        <thead>
          <tr>
            <th>grade</th>
            <th><HelpTerm term="R-multiple">mean R</HelpTerm> (scored)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.grade}>
              <td className="mono cal-grade">{GRADE_LABEL[row.grade] ?? row.grade}</td>
              <td>
                <CalibrationCell row={row} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The nudge-attribution read: across scored calls where the analyst MOVED the
 * baseline (final != baseline), did its moves add R? null = none scored. */
function Nudge({ nudge }: { nudge: AnalystPlayType['nudge'] }) {
  if (nudge === null) {
    return (
      <span className="cal-dim">
        no scored calls where the analyst moved the baseline yet
      </span>
    )
  }
  return (
    <span className="mono">
      analyst moves: {fmtR(nudge.mean_r)}
      <sup className="cal-n"> n={nudge.n.toLocaleString('en-US')}</sup>{' '}
      <span className="cal-dim">mean R across the calls it nudged</span>
    </span>
  )
}

/** The unfilled-fraction three-way split: scored / pending-in-window / expired
 * (the pick never filled — an untested judgment). The fraction names the
 * denominator so a small sample never reads as a confident signal. */
function Freshness({ f }: { f: AnalystFreshness }) {
  const total = f.scored + f.pending_in_window + f.expired_unfilled
  return (
    <div className="cal-fresh">
      <span className="cal-fresh-item">
        <b>{f.scored}</b> scored
      </span>
      <span className="cal-fresh-item">
        <b>{f.pending_in_window}</b> pending in window
      </span>
      <span className="cal-fresh-item cal-fresh-exp" title="the pick never filled inside the scoring window — an untested judgment, unscored forever">
        <b>{f.expired_unfilled}</b> expired unfilled
      </span>
      <span className="cal-fresh-frac cal-dim">
        {total === 0
          ? 'no calls yet'
          : `unfilled ${((f.expired_unfilled / total) * 100).toFixed(0)}% of ${total}`}
      </span>
    </div>
  )
}

/** The autonomy gate's calibration test as a countdown — NOT a lamp that can go
 * green (green is the tier-edge claim). The bound is UNKNOWN when ci_low is null
 * (−inf below the floors): rendered "insufficient data", never a number. */
function Progress({ p }: { p: CalibrationProgress }) {
  return (
    <div className="cal-prog">
      <div className="cal-prog-verdict">
        calibration:{' '}
        <span className={p.calibrated ? 'cal-prog-yes' : 'cal-prog-no'}>
          {p.calibrated ? 'passes' : 'not yet'}
        </span>
        <span className="cal-dim"> · {p.reason}</span>
      </div>
      <div className="cal-prog-row mono">
        high−low {fmtR(p.high_minus_low)} · <HelpTerm term="lower bound">lower bound</HelpTerm>{' '}
        {p.ci_low === null ? (
          <em className="cal-na" title="below the data floors the true bound is −inf, which JSON cannot carry — an honest unknown, never green">
            insufficient data
          </em>
        ) : (
          `≥ ${fmtR(p.ci_low)}`
        )}
      </div>
      <div className="cal-prog-row mono cal-dim">
        high {p.n_high}/{p.min_per_bucket} · low {p.n_low}/{p.min_per_bucket} · clusters{' '}
        {p.n_clusters_high}+{p.n_clusters_low} vs {p.cluster_floor} floor
      </div>
    </div>
  )
}

function PlayTypeBlock({ pt }: { pt: AnalystPlayType }) {
  return (
    <div className="cal-block">
      <div className="cal-block-head">{pt.play_type.toUpperCase()}</div>
      <CalibrationTable rows={pt.calibration} />
      <div className="cal-nudge">
        <Nudge nudge={pt.nudge} />
      </div>
      <Freshness f={pt.freshness} />
      <Progress p={pt.progress} />
    </div>
  )
}

/* ---------------- Spend ---------------- */

function SpendStrip({ spend, today }: { spend: AnalystSpend; today: string }) {
  return (
    <div className="cal-spend">
      <span className="cal-spend-item">
        <span className="cal-spend-lab">today</span>
        <span className="mono cal-spend-val">{fmtUsd(spend.today_usd)}</span>
      </span>
      <span className="cal-spend-item">
        <span className="cal-spend-lab">7d</span>
        <span className="mono cal-spend-val">{fmtUsd(spend.last_7d_usd)}</span>
      </span>
      <span className="cal-spend-item">
        <span className="cal-spend-lab">30d</span>
        <span className="mono cal-spend-val">{fmtUsd(spend.last_30d_usd)}</span>
      </span>
      <span className="cal-spend-note cal-dim" title={spend.note}>
        {spend.uncosted_calls_30d > 0
          ? `${spend.uncosted_calls_30d} uncosted call${spend.uncosted_calls_30d === 1 ? '' : 's'} in 30d — totals undercount`
          : 'real analyst token spend · as of ' + today}
      </span>
    </div>
  )
}

/* ---------------- Screen ---------------- */

export function AnalystScreen({ wake }: { wake: number }) {
  const analyst = usePolling(getAnalyst, POLL_MS, wake)
  const [bump, setBump] = useState(0)
  const analysis = usePolling(getAnalysisList, POLL_MS, wake + bump)
  const onRequested = () => setBump((b) => b + 1)

  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">
          ANALYST <HelpTerm term="Calibration">CALIBRATION</HelpTerm>
          <span className="panel-caption">
            every R is the <HelpTerm term="shadow book">shadow book</HelpTerm>’s — scored research-grid calls, no real dollars ·
            a grade with no scored history is unproven, not a zero
          </span>
        </div>
        <PanelBody polled={analyst} noun="analyst calibration">
          {(data) => (
            <div className="cal">
              <SpendStrip spend={data.spend} today={data.today} />
              <div className="cal-blocks">
                {data.play_types.map((pt) => (
                  <PlayTypeBlock key={pt.play_type} pt={pt} />
                ))}
              </div>
            </div>
          )}
        </PanelBody>
      </section>

      <section className="panel">
        <div className="panel-head">
          <HelpTerm term="deep analysis">DEEP ANALYSIS</HelpTerm>
          <span className="panel-caption">
            queue an on-demand single-ticker report · the worker runs it later, bounded
            by the run-cost cap
          </span>
        </div>
        <AnalysisPanel list={analysis} onRequested={onRequested} />
      </section>
    </main>
  )
}
