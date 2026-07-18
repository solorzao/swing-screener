import { useCallback, useState } from 'react'
import type { ChangeEvent } from 'react'
import {
  POLL_MS,
  getGexPlan,
  getGexSetups,
  getGexStats,
  postGexAutograde,
  postGexBuild,
  postGexImportCommit,
  postGexImportParse,
  postGexSettle,
  postGexSetup,
  postGexSetupStatus,
  usePolling,
} from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type {
  GexAnalyzed,
  GexAutogradeItem,
  GexAutogradeResponse,
  GexChecklist,
  GexDayPlan,
  GexEpisode,
  GexGrade,
  GexParseResult,
  GexPlayType,
  GexSetup,
  GexSetupCreate,
  GexSnapshot,
  GexStrike,
  RobinhoodBook,
} from '../lib/api'
import { GexProfileChart } from '../components/GexProfileChart'
import { dashOr, fmtSignedUsd, fmtUsd, localTodayIso } from '../lib/fmt'
import { PanelBody } from '../components/PanelBody'
import { Segmented } from '../components/Segmented'
import { StatChip } from '../components/StatChip'

/* GEX LAB (digitless masthead screen) — the options lab, wired to the firewalled
   swing_screener.options package via routers/gex.py. Five panels:
     1. DAY PLAN     — GET /api/gex/plan snapshots + build/analyze POSTs
     2. GRADER       — the 12-point A+ checklist, graded live, POST /api/gex/setups
     3. JOURNAL      — today's setups, take/skip on `idea` rows
     4. LAB STATS    — overall + by-grade Stats, plus the Robinhood premium book
     5. IMPORT       — a broker CSV → parse review → tagged commit

   HONESTY POSTURE (mirrors the equity cockpit):
   - GEX levels (spot, walls, flip) are deterministic FACTS — plain nullable
     numbers, never Stats; a null renders an em dash, never a fabricated 0.
   - the thin-chain warning rides LOUD inline (a snapshot row badge + the analyze
     result) — thin levels are unreliable and say so.
   - lab expectancy IS a Stat (session-clustered) and renders through StatChip;
     the Robinhood book is premium dollars, deliberately PLAIN labeled rows.
   - the grade preview replicates checklist.py exactly (all checked → A+; only the
     confirmation candle missing → B; anything else → no_trade) but is advisory —
     the server grades authoritatively at insert.

   Every write bumps a screen-local counter threaded into all polls (the
   PositionsScreen local-bump idiom), so an action refreshes the sibling panels
   immediately; the server action-nonce + SSE cover other windows. */

const MINUS = '−'

/** Parse a numeric input the honest way: '' / garbage → null, and the SERVER's
 * validator names any problem — no client-side guess. */
const num = (s: string): number | null => {
  if (s.trim() === '') return null
  const v = Number(s)
  return Number.isFinite(v) ? v : null
}

/** A GEX price level → mono string; null → em dash. */
const level = (v: number | null): string => dashOr(v, (n) => fmtUsd(n))

/** spacing_pct arrives already as a percent number (bias.py); null → em dash. */
const pct = (v: number | null): string =>
  v === null ? '—' : `${v < 0 ? MINUS : ''}${Math.abs(v).toFixed(2)}%`

/* ---------------- Checklist definition (mirror of checklist.py) ---------------- */

interface ChkItem {
  key: keyof GexChecklist
  label: string
  block: 'bias' | 'structure' | 'trigger' | 'risk'
}

const CHK_ITEMS: ChkItem[] = [
  { key: 'chk_daily_bias_clear', label: 'Daily bias is clear, not chop', block: 'bias' },
  { key: 'chk_daily_stack_ordered', label: 'Daily EMA stack is cleanly ordered', block: 'bias' },
  { key: 'chk_m5_agrees', label: '5-minute trend agrees with the daily', block: 'bias' },
  { key: 'chk_gex_levels_marked', label: 'GEX walls and flip are marked', block: 'structure' },
  { key: 'chk_price_at_pivot', label: 'Price is at the pivot level, not mid-range', block: 'structure' },
  { key: 'chk_regime_match', label: 'Gamma regime matches the play', block: 'structure' },
  { key: 'chk_pattern_clean', label: 'Entry pattern is clean, not forced', block: 'trigger' },
  { key: 'chk_volume_confirming', label: 'Volume confirms the move', block: 'trigger' },
  { key: 'chk_risk_sized', label: 'Position is risk-sized to the plan', block: 'risk' },
  { key: 'chk_stop_structural', label: 'Stop sits behind structure', block: 'risk' },
  { key: 'chk_rr_at_least_2', label: 'Reward-to-risk is at least 2R', block: 'risk' },
  { key: 'chk_confirmation_candle', label: 'Confirmation candle has printed', block: 'risk' },
]

const BLOCKS: { id: ChkItem['block']; label: string }[] = [
  { id: 'bias', label: 'Bias' },
  { id: 'structure', label: 'Structure' },
  { id: 'trigger', label: 'Trigger' },
  { id: 'risk', label: 'Risk' },
]

/** key → short human label (the incomplete-verdict names the gap items by label,
 * not by their raw chk_* key). */
const CHK_LABEL: Record<string, string> = Object.fromEntries(
  CHK_ITEMS.map((it) => [it.key, it.label]),
)

/** The play-type segmented options — breakout | range | — (unset). The machine's
 * regime rule keys on this; '' leaves the regime item reading needs_input. */
const PLAY_TYPE_OPTIONS: { value: GexPlayType; label: string; title: string }[] = [
  { value: 'breakout', label: 'breakout', title: 'breakout play — wants negative gamma' },
  { value: 'range', label: 'range', title: 'range play — wants positive gamma' },
  { value: '', label: MINUS, title: 'no play type set' },
]

const EMPTY_CHECKS: GexChecklist = {
  chk_daily_bias_clear: false,
  chk_daily_stack_ordered: false,
  chk_m5_agrees: false,
  chk_gex_levels_marked: false,
  chk_price_at_pivot: false,
  chk_regime_match: false,
  chk_pattern_clean: false,
  chk_volume_confirming: false,
  chk_risk_sized: false,
  chk_stop_structural: false,
  chk_rr_at_least_2: false,
  chk_confirmation_candle: false,
}

/** The client-side grade preview — checklist.py's rule verbatim. Advisory: the
 * server recomputes at insert. */
function previewGrade(checks: GexChecklist): GexGrade {
  const unchecked = CHK_ITEMS.filter((it) => !checks[it.key])
  if (unchecked.length === 0) return 'A+'
  if (unchecked.length === 1 && unchecked[0].key === 'chk_confirmation_candle') return 'B'
  return 'no_trade'
}

/** A CSS-safe token for a grade string (no '+' / '_' in class names). */
const gradeToken = (g: string): string =>
  g === 'A+' ? 'aplus' : g === 'B' ? 'b' : g === 'no_trade' ? 'notrade' : 'other'

const gradeText = (g: string): string => (g === 'no_trade' ? 'no trade' : g)

/** The advisory grade's gating reason in plain language — the chip alone doesn't
 * say WHY, and the whole B-vs-no_trade call hinges on a single item. */
function gradeReason(checks: GexChecklist): string {
  const unchecked = CHK_ITEMS.filter((it) => !checks[it.key])
  if (unchecked.length === 0) return 'all 12 checked'
  if (unchecked.length === 1 && unchecked[0].key === 'chk_confirmation_candle')
    return 'confirmation candle → B'
  return `${unchecked.length} unchecked`
}

function GradeChip({ grade, lg }: { grade: string; lg?: boolean }) {
  return (
    <span className={`gex-grade gex-grade-${gradeToken(grade)}${lg ? ' gex-grade-lg' : ''}`}>
      {gradeText(grade)}
    </span>
  )
}

/* ============================ 1 · DAY PLAN ============================ */

function ThinBadge({ title }: { title?: string }) {
  return (
    <span className="gex-thin" title={title ?? 'thin chain — these levels are unreliable'}>
      thin chain
    </span>
  )
}

/** True when the snapshot's (lab-tz) day is BEFORE the client's local day —
 * the GEX model is morning-static and valid for ONE session, so yesterday's
 * walls must never present as today's decision levels. */
function snapshotIsStale(ts: string): boolean {
  return ts.slice(0, 10) < localTodayIso()
}

function SnapshotsTable({ rows }: { rows: GexSnapshot[] }) {
  if (rows.length === 0) {
    return <div className="panel-wait">no snapshots yet — build today’s plan</div>
  }
  return (
    <div className="gex-table-wrap">
      <table className="gex-table">
        <thead>
          <tr>
            <th className="left">underlying</th>
            <th className="gex-num">as of</th>
            <th className="gex-num">spot</th>
            <th className="gex-num">call wall</th>
            <th className="gex-num">put wall</th>
            <th className="gex-num"><HelpTerm term="gamma flip">gamma flip</HelpTerm></th>
            <th className="left">regime</th>
            <th className="left">flags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.underlying}>
              <td className="mono gex-tkr">{s.underlying}</td>
              <td
                className="mono gex-num"
                title={`snapshot taken ${s.ts.slice(0, 16).replace('T', ' ')}`}
              >
                {snapshotIsStale(s.ts) ? s.ts.slice(0, 10) : s.ts.slice(11, 16)}
              </td>
              <td className="mono gex-num">{level(s.spot)}</td>
              <td className="mono gex-num">{level(s.call_wall)}</td>
              <td className="mono gex-num">{level(s.put_wall)}</td>
              <td className="mono gex-num">{level(s.gamma_flip)}</td>
              <td>{s.regime === '' ? '—' : s.regime}</td>
              <td className="gex-flags-cell">
                {s.thin_chain && <ThinBadge />}
                {snapshotIsStale(s.ts) && (
                  <span
                    className="gex-stale"
                    title="this snapshot predates today's session — the GEX map is morning-static and valid for one session; rebuild the plan"
                  >
                    stale
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** The reading callout — the deterministic what-this-means lines under the
 * chart (server-built, options/reading.py). Thin warning renders loud, the
 * model-honesty closer dim. */
function GexReading({ lines }: { lines: string[] | undefined }) {
  if (lines === undefined || lines.length === 0) return null
  return (
    <ul className="gex-reading">
      {lines.map((line, i) => (
        <li
          key={i}
          className={
            line.startsWith('THIN CHAIN')
              ? 'gex-reading-warn'
              : line.startsWith('Model:')
                ? 'gex-reading-model'
                : undefined
          }
        >
          {line}
        </li>
      ))}
    </ul>
  )
}

/** The map section: one profile chart + reading per snapshot, an underlying
 * selector when the watchlist has more than one charted name. Snapshots
 * without a stored profile (corrupt blob) simply don't chart — their level
 * numbers still sit in the table above. */
function ProfileSection({ snapshots }: { snapshots: GexSnapshot[] }) {
  const charted = snapshots.filter(
    (s): s is GexSnapshot & { profile: GexStrike[] } =>
      s.profile != null && s.profile.length >= 2,
  )
  const [selected, setSelected] = useState<string | null>(null)
  if (charted.length === 0) return null
  const active =
    charted.find((s) => s.underlying === selected) ?? charted[0]
  return (
    <div className="gex-map">
      <div className="gex-map-head">
        <span className="gex-map-title">
          NET <HelpTerm term="GEX profile">GEX PROFILE</HelpTerm>
        </span>
        {charted.length > 1 && (
          <Segmented
            title="charted underlying"
            options={charted.map((s) => ({ value: s.underlying, label: s.underlying }))}
            value={active.underlying}
            onChange={setSelected}
          />
        )}
      </div>
      <GexProfileChart
        profile={active.profile}
        spot={active.spot}
        callWall={active.call_wall}
        putWall={active.put_wall}
        gammaFlip={active.gamma_flip}
      />
      <GexReading lines={active.reading} />
    </div>
  )
}

function BuiltPlans({ plans }: { plans: GexDayPlan[] }) {
  if (plans.length === 0) {
    return <div className="panel-wait">built — no plans returned (no watchlist snapshots)</div>
  }
  return (
    <div className="gex-table-wrap">
      <table className="gex-table">
        <thead>
          <tr>
            <th className="left">underlying</th>
            <th className="left">bias</th>
            <th className="left">regime</th>
            <th className="left">call</th>
            <th className="gex-num">spacing</th>
            <th className="gex-num">spot</th>
            <th className="gex-num">call wall</th>
            <th className="gex-num">put wall</th>
            <th className="gex-num">flip</th>
          </tr>
        </thead>
        <tbody>
          {plans.map((p) => (
            <tr key={p.underlying}>
              <td className="mono gex-tkr">{p.underlying}</td>
              <td>{p.bias}</td>
              <td>{p.regime}</td>
              <td className="mono">{p.call}</td>
              <td className="mono gex-num">{pct(p.spacing_pct)}</td>
              <td className="mono gex-num">{level(p.spot)}</td>
              <td className="mono gex-num">{level(p.call_wall)}</td>
              <td className="mono gex-num">{level(p.put_wall)}</td>
              <td className="mono gex-num">{level(p.gamma_flip)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AnalyzedCard({ a }: { a: GexAnalyzed }) {
  return (
    <div className="gex-analyzed">
      <div className="gex-analyzed-head mono">
        {a.underlying} · regime {a.regime === '' ? '—' : a.regime}
        {a.thin_chain && <ThinBadge />}
      </div>
      <div className="gex-analyzed-levels mono">
        spot {level(a.spot)} · call wall {level(a.call_wall)} · put wall{' '}
        {level(a.put_wall)} · flip {level(a.gamma_flip)}
      </div>
      {a.thin_chain && a.thin_reasons.length > 0 && (
        <ul className="gex-thin-reasons">
          {a.thin_reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
      {a.profile !== undefined && a.profile.length >= 2 && (
        <GexProfileChart
          profile={a.profile}
          spot={a.spot}
          callWall={a.call_wall}
          putWall={a.put_wall}
          gammaFlip={a.gamma_flip}
        />
      )}
      <GexReading lines={a.reading} />
    </div>
  )
}

function DayPlanPanel({ wake, onAction }: { wake: number; onAction: () => void }) {
  const plan = usePolling(getGexPlan, POLL_MS, wake)
  const [building, setBuilding] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [ticker, setTicker] = useState('')
  const [built, setBuilt] = useState<GexDayPlan[] | null>(null)
  const [analyzed, setAnalyzed] = useState<GexAnalyzed | null>(null)
  const [error, setError] = useState<string | null>(null)

  const build = () => {
    setBuilding(true)
    setError(null)
    setBuilt(null)
    setAnalyzed(null)
    postGexBuild().then(
      (res) => {
        setBuilding(false)
        setBuilt(res.plans ?? [])
        onAction()
      },
      (err: unknown) => {
        setBuilding(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  const analyze = () => {
    if (ticker.trim() === '') {
      setError('enter a ticker to analyze')
      return
    }
    setAnalyzing(true)
    setError(null)
    setBuilt(null)
    setAnalyzed(null)
    postGexBuild(ticker).then(
      (res) => {
        setAnalyzing(false)
        setAnalyzed(res.analyzed ?? null)
        onAction()
      },
      (err: unknown) => {
        setAnalyzing(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  const busy = building || analyzing

  return (
    <section className="panel">
      <div className="panel-head">
        DAY PLAN
        <span className="panel-caption">
          latest GEX snapshot per watchlist name · levels are deterministic facts,
          not statistics
        </span>
        <span className="spacer" />
        <button type="button" className="gex-btn" onClick={build} disabled={busy}>
          {building ? 'building…' : 'Build today’s plan'}
        </button>
        <input
          className="gex-in gex-in-tkr mono"
          value={ticker}
          maxLength={16}
          placeholder="ticker…"
          aria-label="ticker to analyze"
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
        />
        <button type="button" className="gex-btn" onClick={analyze} disabled={busy}>
          {analyzing ? 'analyzing…' : 'Analyze ticker'}
        </button>
      </div>
      {error !== null && (
        <div className="gex-err" role="alert">
          {error}
        </div>
      )}
      <PanelBody polled={plan} noun="gex plan">
        {(data) => (
          <>
            {data.watchlist.length > 0 && (
              <div className="gex-watchlist mono">watchlist: {data.watchlist.join(' · ')}</div>
            )}
            {data.snapshots.length > 0 &&
              data.snapshots.every((s) => snapshotIsStale(s.ts)) && (
                <div
                  className="gex-plan-stale"
                  role="status"
                  title="every snapshot predates today's session — a stale GEX map must not read as current decision levels"
                >
                  plan not built today — these are a previous session’s levels
                </div>
              )}
            <SnapshotsTable rows={data.snapshots} />
            <ProfileSection snapshots={data.snapshots} />
          </>
        )}
      </PanelBody>
      {analyzed !== null && (
        <div className="gex-actionbox">
          <div className="gex-actionbox-head">ANALYZE RESULT</div>
          <AnalyzedCard a={analyzed} />
        </div>
      )}
      {built !== null && (
        <div className="gex-actionbox">
          <div className="gex-actionbox-head">BUILT PLANS</div>
          <BuiltPlans plans={built} />
        </div>
      )}
    </section>
  )
}

/* ============================ 2 · CHECKLIST GRADER ============================ */

type Direction = 'long' | 'short'

/** The machine verdict line (options/autograde.py's rule, rendered): a YES names
 * the count and stays "pending your confirms" (blue, never the green edge claim);
 * a NO joins the failing FACTS; an incomplete NAMES the gap items (by label, not
 * raw key) so the trader knows what to grade by eye. */
function machineVerdict(
  res: GexAutogradeResponse,
): { tone: 'yes' | 'no' | 'incomplete'; text: string } {
  if (res.machine_verdict === 'yes') {
    return { tone: 'yes', text: 'machine: 8/8 — YES, pending your confirms' }
  }
  if (res.machine_verdict === 'no') {
    const fails = res.items.filter((it) => it.state === 'fail').map((it) => it.fact)
    return { tone: 'no', text: `machine: NO — ${fails.join(' · ')}` }
  }
  const gaps = res.items
    .filter((it) => it.state === 'unavailable' || it.state === 'needs_input')
    .map((it) => CHK_LABEL[it.key] ?? it.key)
  return { tone: 'incomplete', text: `machine: incomplete — ${gaps.join(' · ')}` }
}

function GraderPanel({ onAction }: { onAction: () => void }) {
  const [underlying, setUnderlying] = useState('')
  const [direction, setDirection] = useState<Direction>('long')
  const [playType, setPlayType] = useState<GexPlayType>('')
  const [entry, setEntry] = useState('')
  const [stop, setStop] = useState('')
  const [target, setTarget] = useState('')
  const [pivot, setPivot] = useState('')
  const [regime, setRegime] = useState('')
  const [pattern, setPattern] = useState('')
  const [notes, setNotes] = useState('')
  const [checks, setChecks] = useState<GexChecklist>(EMPTY_CHECKS)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<GexSetup | null>(null)
  // The last autograde response (holds the per-item facts, the two hints, the
  // verdict, and the provenance blob echoed VERBATIM on submit); `machineKeys` is
  // which boxes still wear the ⚙ mark — a human edit drops that key from the set.
  const [autograding, setAutograding] = useState(false)
  const [autoResult, setAutoResult] = useState<GexAutogradeResponse | null>(null)
  const [machineKeys, setMachineKeys] = useState<Set<keyof GexChecklist>>(new Set())

  // Editing underlying/direction after a grade must drop the stale provenance
  // entirely — it belongs to a different ticket and must never be journaled.
  const clearAutograde = () => {
    setAutoResult(null)
    setMachineKeys((m) => (m.size === 0 ? m : new Set()))
  }

  const toggle = (key: keyof GexChecklist) => {
    setChecks((c) => ({ ...c, [key]: !c[key] }))
    // A human edit of a machine-ticked box flips it out of the machine-graded set
    // (⚙ off) — but the provenance JSON stays as the machine said (its record,
    // not the form state; the human owns the final submission).
    setMachineKeys((m) => {
      if (!m.has(key)) return m
      const next = new Set(m)
      next.delete(key)
      return next
    })
  }

  const grade = previewGrade(checks)
  // Per-key machine item + advisory hint lookups for the checklist render.
  const machineItems = new Map<string, GexAutogradeItem>(
    (autoResult?.items ?? []).map((it): [string, GexAutogradeItem] => [it.key, it]),
  )
  // hints arrive [pivot, stop] (autograde.py HINT_KEYS order); each rides under
  // its own human box.
  const hintForKey = (key: string): string | null => {
    if (autoResult === null) return null
    if (key === 'chk_price_at_pivot') return autoResult.hints[0] ?? null
    if (key === 'chk_stop_structural') return autoResult.hints[1] ?? null
    return null
  }
  const verdict = autoResult === null ? null : machineVerdict(autoResult)

  // Deterministic R / R:R read-out off the bracket — the "reward-to-risk ≥ 2R"
  // check is easier to honour when the number is live. Null → em dash (a fact,
  // never a fabricated 0), same posture as the GEX levels.
  const rEntry = num(entry)
  const rStop = num(stop)
  const rTarget = num(target)
  const risk = rEntry !== null && rStop !== null ? Math.abs(rEntry - rStop) : null
  const reward = rEntry !== null && rTarget !== null ? Math.abs(rTarget - rEntry) : null
  const rr = risk !== null && risk > 0 && reward !== null ? reward / risk : null
  const rrText = `R ${risk === null ? '—' : risk.toFixed(2)} · R:R ${
    rr === null ? '—' : `${rr.toFixed(1)}×`
  }`

  const reset = () => {
    setUnderlying('')
    setDirection('long')
    setPlayType('')
    setEntry('')
    setStop('')
    setTarget('')
    setPivot('')
    setRegime('')
    setPattern('')
    setNotes('')
    setChecks(EMPTY_CHECKS)
    setAutoResult(null)
    setMachineKeys(new Set())
  }

  const runAutograde = () => {
    setAutograding(true)
    setError(null)
    postGexAutograde({
      underlying: underlying.trim(),
      direction,
      play_type: playType,
      entry: num(entry),
      stop: num(stop),
      target: num(target),
      pivot_level: num(pivot),
    }).then(
      (res) => {
        setAutograding(false)
        setAutoResult(res)
        // Pre-fill the eight machine boxes: pass → checked, everything else
        // (fail / needs_input / unavailable) → unchecked. A fail UNCHECKS a box a
        // human had ticked — machine facts win on machine items. The four human
        // boxes (pattern, pivot, stop, risk) are never touched here.
        setChecks((c) => {
          const next = { ...c }
          for (const it of res.items) {
            next[it.key as keyof GexChecklist] = it.state === 'pass'
          }
          return next
        })
        setMachineKeys(new Set(res.items.map((it) => it.key as keyof GexChecklist)))
      },
      (err: unknown) => {
        setAutograding(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  const submit = () => {
    setSubmitting(true)
    setError(null)
    setDone(null)
    const body: GexSetupCreate = {
      underlying: underlying.trim(),
      direction,
      checklist: checks,
      entry: num(entry),
      stop: num(stop),
      target: num(target),
      pivot_level: num(pivot),
      regime: regime.trim() === '' ? undefined : regime.trim(),
      play_type: playType,
      pattern: pattern.trim(),
      notes: notes.trim(),
      // The machine's record, echoed verbatim (null when it never ran / was
      // cleared by a ticker edit) — never reconstructed client-side.
      autograde_json: autoResult?.autograde_json ?? null,
    }
    postGexSetup(body).then(
      (row) => {
        setSubmitting(false)
        setDone(row)
        reset()
        onAction()
      },
      (err: unknown) => {
        setSubmitting(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        CHECKLIST GRADER
        <span className="panel-caption">
          the 12-point <HelpTerm term="A+ checklist">A+ checklist</HelpTerm> · graded live (server regrades at insert)
        </span>
      </div>
      <form
        className="gex-grader"
        onSubmit={(e) => {
          e.preventDefault()
          if (!submitting && underlying.trim() !== '') submit()
        }}
      >
        <div className="gex-grader-top">
          <label className="gex-field gex-field-tkr">
            <span className="gex-lab">underlying</span>
            <input
              className="gex-in mono"
              value={underlying}
              maxLength={16}
              onChange={(e) => {
                setUnderlying(e.target.value.toUpperCase())
                clearAutograde()
              }}
            />
          </label>
          <span className="gex-field">
            <span className="gex-lab">direction</span>
            <Segmented
              title="direction"
              options={[
                { value: 'long', label: 'long' },
                { value: 'short', label: 'short' },
              ]}
              value={direction}
              onChange={(d) => {
                setDirection(d)
                clearAutograde()
              }}
            />
          </span>
          <span className="gex-field">
            <span className="gex-lab">play type</span>
            <Segmented
              title="play type"
              options={PLAY_TYPE_OPTIONS}
              value={playType}
              onChange={setPlayType}
            />
          </span>
          <div className="gex-bracket">
            <label className="gex-field gex-field-num">
              <span className="gex-lab">entry</span>
              <input
                className="gex-in gex-in-num mono"
                type="number"
                step="0.01"
                value={entry}
                onChange={(e) => setEntry(e.target.value)}
              />
            </label>
            <label className="gex-field gex-field-num">
              <span className="gex-lab">stop</span>
              <input
                className="gex-in gex-in-num mono"
                type="number"
                step="0.01"
                value={stop}
                onChange={(e) => setStop(e.target.value)}
              />
            </label>
            <label className="gex-field gex-field-num">
              <span className="gex-lab">target</span>
              <input
                className="gex-in gex-in-num mono"
                type="number"
                step="0.01"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              />
            </label>
            <span className="gex-rr mono" aria-live="polite">
              {rrText}
            </span>
          </div>
          <label className="gex-field gex-field-num">
            <span className="gex-lab">pivot</span>
            <input
              className="gex-in gex-in-num mono"
              type="number"
              step="0.01"
              value={pivot}
              onChange={(e) => setPivot(e.target.value)}
            />
          </label>
          <label className="gex-field gex-field-regime">
            <span className="gex-lab">regime</span>
            <input
              className="gex-in mono"
              value={regime}
              maxLength={16}
              placeholder="e.g. positive"
              onChange={(e) => setRegime(e.target.value)}
            />
          </label>
          <label className="gex-field gex-field-grow">
            <span className="gex-lab">pattern</span>
            <input
              className="gex-in"
              value={pattern}
              maxLength={256}
              onChange={(e) => setPattern(e.target.value)}
            />
          </label>
          <label className="gex-field gex-field-wide">
            <span className="gex-lab">notes</span>
            <input
              className="gex-in"
              value={notes}
              maxLength={2048}
              onChange={(e) => setNotes(e.target.value)}
            />
          </label>
        </div>

        {verdict !== null && (
          <div
            className={`gex-verdict gex-verdict-${verdict.tone}`}
            role="status"
            aria-live="polite"
          >
            {verdict.text}
          </div>
        )}

        <div className="gex-blocks">
          {BLOCKS.map((block) => {
            const items = CHK_ITEMS.filter((it) => it.block === block.id)
            const checkedN = items.filter((it) => checks[it.key]).length
            return (
              <div key={block.id} className="gex-block">
                <div className="gex-block-head">
                  {block.label}
                  <span
                    className={`gex-block-count${checkedN === items.length ? ' done' : ''}`}
                  >
                    {checkedN}/{items.length}
                  </span>
                </div>
                {items.map((it) => {
                  const mi = machineItems.get(it.key)
                  const hint = hintForKey(it.key)
                  return (
                    <div key={it.key} className="gex-check-row">
                      <label className="gex-check">
                        <input
                          type="checkbox"
                          checked={checks[it.key]}
                          onChange={() => toggle(it.key)}
                        />
                        <span>{it.label}</span>
                        {machineKeys.has(it.key) && (
                          <span
                            className="gex-auto"
                            title="machine-graded — editing this box clears the mark"
                          >
                            ⚙
                          </span>
                        )}
                      </label>
                      {mi !== undefined && (
                        <div className={`gex-fact gex-fact-${mi.state}`}>{mi.fact}</div>
                      )}
                      {hint !== null && (
                        <div className="gex-fact gex-hint">
                          <span className="gex-hint-tag">hint</span>
                          {hint}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>

        <div className="gex-grader-foot">
          <span className="gex-grade-preview" aria-live="polite">
            <span className="gex-grade-preview-lab">live grade</span>
            <GradeChip grade={grade} lg />
            <span className="gex-grade-reason">{gradeReason(checks)}</span>
          </span>
          <button
            type="button"
            className="gex-btn"
            disabled={autograding || underlying.trim() === ''}
            title="machine pre-grade the 8 computable checklist items for this ticker — decision support, not the journaled grade"
            onClick={runAutograde}
          >
            {autograding ? 'auto-grading…' : 'Auto-grade'}
          </button>
          <button
            type="submit"
            className="gex-btn gex-submit"
            disabled={submitting || autograding || underlying.trim() === ''}
          >
            {submitting ? 'saving…' : 'Grade & journal setup'}
          </button>
          <span className="gex-note">journals an idea — takes no trade</span>
        </div>

        {error !== null && (
          <div className="gex-err" role="alert">
            {error}
          </div>
        )}
        {done !== null && (
          <div className="gex-done" role="status">
            journaled setup #{done.id} · {done.underlying} · graded{' '}
            <GradeChip grade={done.grade} />
            <button type="button" className="gex-btn gex-btn-quiet" onClick={() => setDone(null)}>
              dismiss
            </button>
          </div>
        )}
      </form>
    </section>
  )
}

/* ============================ 3 · JOURNAL ============================ */

function JournalRow({
  setup,
  busy,
  onStatus,
}: {
  setup: GexSetup
  busy: boolean
  onStatus: (id: number, status: 'taken' | 'skipped') => void
}) {
  const time = setup.ts.slice(11, 16) || setup.ts
  return (
    <tr>
      <td className="mono gex-num">{time}</td>
      <td className="mono gex-tkr">{setup.underlying}</td>
      <td>{setup.direction}</td>
      <td>
        <GradeChip grade={setup.grade} />
      </td>
      <td className="mono gex-num">{level(setup.entry)}</td>
      <td className="mono gex-num">{level(setup.stop)}</td>
      <td className="mono gex-num">{level(setup.target)}</td>
      <td>
        <span className={`gex-status gex-status-${setup.status}`}>{setup.status}</span>
      </td>
      <td>
        {setup.trade == null ? (
          <span className="gex-noaction">—</span>
        ) : setup.trade.status === 'open' ? (
          <span
            className="gex-outcome gex-outcome-open"
            title="the linked lab paper trade is still open — the settle sweep grades it"
          >
            open
          </span>
        ) : (
          <span
            /* null realized_r = closed but ungraded — NEUTRAL, never win-green
               (the old `?? 0 >= 0` styled an unmeasured close as a win). */
            className={`gex-outcome${
              setup.trade.realized_r === null
                ? ''
                : setup.trade.realized_r >= 0
                  ? ' gex-outcome-win'
                  : ' gex-outcome-loss'
            }`}
            title={`settled ${setup.trade.exit_reason ?? '—'}`}
          >
            {setup.trade.exit_reason ?? 'closed'}{' '}
            {setup.trade.realized_r === null
              ? ''
              : `${setup.trade.realized_r >= 0 ? '+' : MINUS}${Math.abs(setup.trade.realized_r).toFixed(2)}R`}
          </span>
        )}
      </td>
      <td>
        {setup.status === 'idea' ? (
          <div className="gex-row-actions">
            <button
              type="button"
              className="gex-btn gex-btn-sm"
              disabled={busy}
              onClick={() => onStatus(setup.id, 'taken')}
            >
              take
            </button>
            <button
              type="button"
              className="gex-btn gex-btn-sm gex-btn-quiet"
              disabled={busy}
              onClick={() => onStatus(setup.id, 'skipped')}
            >
              skip
            </button>
          </div>
        ) : (
          <span className="gex-noaction">—</span>
        )}
      </td>
    </tr>
  )
}

function JournalPanel({ wake, onAction }: { wake: number; onAction: () => void }) {
  const day = localTodayIso()
  // today vs the last 7 days — a taken setup's outcome usually lands AFTER its
  // day (the settle sweep), so a today-only journal read "taken" forever.
  const [scope, setScope] = useState<'today' | 'recent'>('today')
  const setups = usePolling(
    () => getGexSetups(day, scope === 'recent'),
    POLL_MS,
    wake,
    `${day}|${scope}`,
  )
  const [busyId, setBusyId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const onStatus = (id: number, status: 'taken' | 'skipped') => {
    setBusyId(id)
    setError(null)
    postGexSetupStatus(id, status).then(
      () => {
        setBusyId(null)
        onAction()
      },
      (err: unknown) => {
        setBusyId(null)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        JOURNAL
        <span className="panel-caption">
          setups newest first · taking one opens a paper trade on the firewalled
          lab book · outcomes land via the settle sweep
        </span>
        <span className="spacer" />
        <Segmented
          title="journal scope"
          options={[
            { value: 'today', label: 'today' },
            { value: 'recent', label: '7d' },
          ]}
          value={scope}
          onChange={setScope}
        />
      </div>
      {error !== null && (
        <div className="gex-err" role="alert">
          {error}
        </div>
      )}
      <PanelBody polled={setups} noun="setups">
        {(data) =>
          data.setups.length === 0 ? (
            <div className="panel-wait">
              {scope === 'today'
                ? 'no setups journaled today'
                : 'no setups journaled in the last 7 days'}
            </div>
          ) : (
            <div className="gex-table-wrap">
              <table className="gex-table">
                <thead>
                  <tr>
                    <th className="gex-num">time</th>
                    <th className="left">underlying</th>
                    <th className="left">dir</th>
                    <th className="left">grade</th>
                    <th className="gex-num">entry</th>
                    <th className="gex-num">stop</th>
                    <th className="gex-num">target</th>
                    <th className="left">status</th>
                    <th className="left">outcome</th>
                    <th className="left">action</th>
                  </tr>
                </thead>
                <tbody>
                  {data.setups.map((s) => (
                    <JournalRow
                      key={s.id}
                      setup={s}
                      busy={busyId === s.id}
                      onStatus={onStatus}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </PanelBody>
    </section>
  )
}

/* ============================ 4 · LAB STATS ============================ */

function RobinhoodRow({ tag, book }: { tag: string; book: RobinhoodBook }) {
  return (
    <div className="gex-rh-row">
      <span className="gex-rh-tag">{tag}</span>
      <span className="gex-rh-cell mono">P&amp;L {fmtSignedUsd(book.total_pnl)}</span>
      <span className="gex-rh-cell mono">
        W/L {book.wins}/{book.losses}
      </span>
      <span className="gex-rh-cell mono">n {book.n}</span>
      <span className="gex-rh-cell mono">open {book.open}</span>
    </div>
  )
}

function LabStatsPanel({ wake, onAction }: { wake: number; onAction: () => void }) {
  const stats = usePolling(getGexStats, POLL_MS, wake)
  const [settling, setSettling] = useState(false)
  const [settleNote, setSettleNote] = useState<string | null>(null)
  const openCount = stats.data?.open_trades ?? null

  const settle = () => {
    setSettling(true)
    setSettleNote(null)
    postGexSettle().then(
      (r) => {
        setSettling(false)
        setSettleNote(`settled ${r.settled} · ${r.open_remaining} still open`)
        onAction()
      },
      (err: unknown) => {
        setSettling(false)
        setSettleNote(err instanceof Error ? err.message : String(err))
      },
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        LAB STATS
        <span className="panel-caption">
          lab <HelpTerm term="expectancy">expectancy</HelpTerm> is <HelpTerm term="cluster">session-clustered</HelpTerm> (a Stat) · the Robinhood book is
          premium dollars, a labeled display
        </span>
        <span className="spacer" />
        <button
          type="button"
          className="gex-btn"
          disabled={settling}
          title="grade due open lab paper trades against their bars (the CLI settle ritual, idempotent) — an open trade contributes nothing until settled"
          onClick={settle}
        >
          {settling
            ? 'settling…'
            : openCount === null
              ? 'Settle open trades'
              : `Settle open trades (${openCount})`}
        </button>
      </div>
      {settleNote !== null && (
        <div className="gex-settle-note mono" role="status">
          {settleNote}
        </div>
      )}
      <PanelBody polled={stats} noun="lab stats">
        {(data) => {
          const rhTags = Object.keys(data.robinhood).sort((a, b) => {
            const order = (t: string) => (t === 'gex' ? 0 : t === 'other' ? 1 : 2)
            return order(a) - order(b) || a.localeCompare(b)
          })
          return (
            <div className="gex-stats">
              <div className="gex-stats-group">
                <div className="gex-stats-head">EXPECTANCY</div>
                <div className="gex-stats-chips">
                  <StatChip stat={data.overall} label="overall" />
                  {data.by_grade.map((row) => (
                    <StatChip key={row.grade} stat={row} label={row.grade} />
                  ))}
                </div>
              </div>
              <div className="gex-stats-group">
                <div className="gex-stats-head">ROBINHOOD (imported premium)</div>
                {rhTags.length === 0 ? (
                  <div className="panel-wait">no imported Robinhood rows yet</div>
                ) : (
                  <div className="gex-rh">
                    {rhTags.map((tag) => (
                      <RobinhoodRow key={tag} tag={tag} book={data.robinhood[tag]} />
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        }}
      </PanelBody>
    </section>
  )
}

/* ============================ 5 · IMPORT ============================ */

const TAG_OPTIONS = ['gex', 'other', 'skip']

function ImportRow({
  ep,
  tag,
  onTag,
}: {
  ep: GexEpisode
  tag: string
  onTag: (importKey: string, value: string) => void
}) {
  const span = `${ep.opened_on} → ${ep.closed_on ?? 'open'}`
  return (
    <tr>
      <td className="mono gex-tkr">{ep.occ_symbol}</td>
      <td className="mono">{span}</td>
      <td className="mono gex-num">{ep.contracts}</td>
      <td className="mono gex-num">{dashOr(ep.pnl, fmtSignedUsd)}</td>
      <td>{ep.needs_review && <span className="gex-review">needs review</span>}</td>
      <td>
        <select
          className="gex-select mono"
          value={tag}
          aria-label={`tag for ${ep.occ_symbol}`}
          onChange={(e) => onTag(ep.import_key, e.target.value)}
        >
          {TAG_OPTIONS.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </td>
    </tr>
  )
}

function ImportPanel({ onAction }: { onAction: () => void }) {
  const [csvText, setCsvText] = useState<string | null>(null)
  const [fileName, setFileName] = useState('')
  const [parsed, setParsed] = useState<GexParseResult | null>(null)
  const [tags, setTags] = useState<Record<string, string>>({})
  const [parsing, setParsing] = useState(false)
  const [committing, setCommitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [committed, setCommitted] = useState<number | null>(null)

  const parse = (text: string) => {
    setParsing(true)
    setError(null)
    setCommitted(null)
    setParsed(null)
    postGexImportParse(text).then(
      (res) => {
        setParsing(false)
        setParsed(res)
        const t: Record<string, string> = {}
        for (const ep of res.episodes) t[ep.import_key] = 'skip'
        setTags(t)
      },
      (err: unknown) => {
        setParsing(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file === undefined) return
    // Reset the input NOW (the File object stays readable): a same-file
    // re-select must fire change again, or a failed parse/commit could never
    // be retried without picking a different file first.
    e.target.value = ''
    setFileName(file.name)
    const reader = new FileReader()
    reader.onload = () => {
      const text = typeof reader.result === 'string' ? reader.result : ''
      setCsvText(text)
      parse(text)
    }
    reader.onerror = () => setError('could not read the file')
    reader.readAsText(file)
  }

  const onTag = (importKey: string, value: string) =>
    setTags((t) => ({ ...t, [importKey]: value }))

  const commit = () => {
    if (csvText === null) return
    setCommitting(true)
    setError(null)
    postGexImportCommit(csvText, tags).then(
      (res) => {
        setCommitting(false)
        setCommitted(res.committed)
        onAction()
      },
      (err: unknown) => {
        setCommitting(false)
        setError(err instanceof Error ? err.message : String(err))
      },
    )
  }

  return (
    <section className="panel">
      <div className="panel-head">
        IMPORT
        <span className="panel-caption">
          a broker activity CSV → paired episodes → tag each gex / other / skip,
          then commit
        </span>
        <span className="spacer" />
        <label className="gex-btn gex-file">
          {parsing ? 'parsing…' : 'Choose CSV…'}
          <input
            type="file"
            accept=".csv,text/csv"
            className="gex-file-input"
            onChange={onFile}
          />
        </label>
      </div>
      {fileName !== '' && (
        <div className="gex-actionnote mono">
          {fileName}
          {parsed !== null &&
            ` · ${parsed.fills_added} fills added · ${parsed.fills_skipped} skipped (already stored)`}
        </div>
      )}
      {error !== null && (
        <div className="gex-err" role="alert">
          {error}
        </div>
      )}
      {parsed !== null &&
        (parsed.episodes.length === 0 ? (
          <div className="panel-wait">no episodes paired from this CSV</div>
        ) : (
          <>
            <div className="gex-table-wrap">
              <table className="gex-table">
                <thead>
                  <tr>
                    <th className="left">occ symbol</th>
                    <th className="left">span</th>
                    <th className="gex-num">contracts</th>
                    <th className="gex-num">P&amp;L</th>
                    <th className="left">flags</th>
                    <th className="left">tag</th>
                  </tr>
                </thead>
                <tbody>
                  {parsed.episodes.map((ep) => (
                    <ImportRow
                      key={ep.import_key}
                      ep={ep}
                      tag={tags[ep.import_key] ?? 'skip'}
                      onTag={onTag}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            <div className="gex-grader-foot">
              <button
                type="button"
                className="gex-btn gex-submit"
                onClick={commit}
                disabled={committing}
              >
                {committing ? 'committing…' : 'Commit tagged'}
              </button>
              <span className="gex-note">
                skip-tagged episodes are left untouched · re-parses the same CSV
                server-side
              </span>
              {committed !== null && (
                <span className="gex-done-inline" role="status">
                  committed {committed} episode{committed === 1 ? '' : 's'}
                </span>
              )}
            </div>
          </>
        ))}
    </section>
  )
}

/* ============================ screen ============================ */

export function GexLabScreen({ wake }: { wake: number }) {
  // Screen-local action bump summed into every panel's wake — a write refreshes
  // the siblings immediately (PositionsScreen idiom); SSE covers other windows.
  const [bump, setBump] = useState(0)
  const wakeAll = wake + bump
  const onAction = useCallback(() => setBump((b) => b + 1), [])

  return (
    <main className="grid-single">
      <DayPlanPanel wake={wakeAll} onAction={onAction} />
      <GraderPanel onAction={onAction} />
      {/* The write→result band: today's setups (1fr) beside the all-time
          expectancy + premium scoreboard (380px rail) — the PositionsScreen
          pos-cols idiom, so the sparse stats panel stops reading as dead space. */}
      <div className="pos-cols">
        <JournalPanel wake={wakeAll} onAction={onAction} />
        <LabStatsPanel wake={wakeAll} onAction={onAction} />
      </div>
      <ImportPanel onAction={onAction} />
    </main>
  )
}
