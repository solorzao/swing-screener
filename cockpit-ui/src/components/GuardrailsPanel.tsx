import { useEffect, useRef, useState } from 'react'
import {
  ApiError,
  D503_DB,
  POLL_MS,
  getGuardrails,
  postGuardrails,
  usePolling,
} from '../lib/api'
import type {
  GuardrailBreach,
  GuardrailEditBody,
  GuardrailEvent,
  GuardrailLimits,
  GuardrailSweep,
  Guardrails,
  GuardrailsPostResult,
} from '../lib/api'
import { fmtStamp, localTodayIso } from '../lib/fmt'
import { HelpTerm } from './HelpTerm'
import { HoldToConfirm } from './HoldToConfirm'

/* GUARDRAILS — the brake, on the Execution Safety screen (screen 7).

   A BRAKE, NOT A KNOB. Nothing here arms anything: the four breakers stop
   dispatch, the HALT stops it by hand, and clearing only returns the machine to
   whatever the env already permitted. Env stays the master arm.

   DB-SCOPED, and that matters on this screen. Everything else in EXECUTION
   STATUS reads THIS process's env ("the Azure jobs run under their own") — the
   brake reads the shared `agent_guardrails` row, the single venue of record for
   every process, so a trip landed by an Azure job shows here. The caption says
   so rather than letting the neighbouring env_scope label be read as covering
   this panel too.

   HONESTY RULES, inherited from the screen:
   - a failed read force-nulls to a dashed UNKNOWN block. A stale "OK" banner is
     the one thing a brake panel may never show. The swap covers the DATA
     sections ONLY (banner, history): every control keeps rendering from the
     retained last-good snapshot, because unmounting them would destroy
     in-flight OPERATOR state — a HALT result carrying an UNPROTECTED alarm, a
     typed limits edit, a ticked acknowledgement — and would drop the result of
     any request still in flight. A poll failure is not an operator's fault and
     must not cost them their work.
   - a lingering `sweep_state` of pending/partial is RETRYING, never "failed":
     every hourly cycle re-runs that sweep and DISARM resumes the same one. Same
     wording as the auditor's guardrail-stuck-sweep rule.
   - a write that 503s with the `database error (` prefix carries the G7 hint —
     the write grant may be missing in prod. The GET half keeps working read-only
     and the brake is still ENFORCED by the jobs; only the cockpit's own edits
     are refused. */

//: the sweep_state values that mean the trip's sweep has not finished. Same
//: vocabulary as db/guardrails_repo.py's INCOMPLETE_SWEEPS (the server-side owner).
const SWEEP_RETRYING = ['pending', 'partial']

//: how much history the panel shows. Mirrors routers/safety.py's _EVENT_HISTORY
//: so the caption below the HISTORY heading is true whatever the server sends.
const EVENT_LIMIT = 25

/** A write rejection → one human line + whether to show the G7 write-grant hint.
 * `database error (` is one of the three prefixes routers/safety.py documents as
 * a stable contract; the text after it is human-facing and never matched on. */
interface WriteErr {
  text: string
  g7: boolean
}

function writeError(err: unknown, realRun: boolean): WriteErr {
  if (err instanceof ApiError) {
    return { text: err.message, g7: err.message.startsWith(D503_DB) }
  }
  return {
    text: realRun
      ? 'backend unreachable — the action may or may not have landed'
      : 'backend unreachable',
    g7: false,
  }
}

/** The 422 field rows (Pydantic's LIST detail shape), keyed by field name — the
 * repo's own ValueError arrives as a plain string instead and rides `text`. */
function fieldErrorsOf(err: unknown): Record<string, string> {
  if (!(err instanceof ApiError) || err.fieldErrors === null) return {}
  const out: Record<string, string> = {}
  for (const fe of err.fieldErrors) out[fe.loc] = fe.msg
  return out
}

/** `announce` false when the PARENT is already a live region (the result pops
 * carry role=alert/status): a nested role="alert" double-announces the same
 * sentence, which on a safety surface reads as two separate failures. */
function ErrLine({ err, announce = true }: { err: WriteErr; announce?: boolean }) {
  return (
    <div className="gr-err" role={announce ? 'alert' : undefined}>
      {err.text}
      {err.g7 && (
        <div className="gr-err-hint">
          write grant (G7) may be missing — the brake is still enforced by the jobs
        </div>
      )}
    </div>
  )
}

/* ---------- the state banner ---------- */

function StateBanner({ g }: { g: Guardrails }) {
  if (g.state === 'ok') {
    return (
      <div className="gr-banner gr-ok">
        <div className="gr-state">
          <HelpTerm term="brake state">OK</HelpTerm>
        </div>
        <div className="gr-banner-note">
          the brake is released — the breakers below are consulted before every
          dispatch, and hourly while the market is open
        </div>
      </div>
    )
  }
  if (g.state === 'halted') {
    return (
      <div className="gr-banner gr-halted" role="status">
        <div className="gr-state">
          <HelpTerm term="brake state">HALTED</HelpTerm>
        </div>
        <div className="gr-banner-note">
          an operator brake is engaged — nothing dispatches until it is cleared.
          Positions and their stops are untouched.
        </div>
      </div>
    )
  }
  // TRIPPED. A sweep that has not finished takes the headline: the venue may
  // still hold working entry orders, which outranks the breaker's own text.
  const retrying = g.sweep_state !== null && SWEEP_RETRYING.includes(g.sweep_state)
  const reason = g.trip_reason ?? 'no reason recorded'
  return (
    <div className="gr-banner gr-tripped" role="alert">
      <div className="gr-state">
        <HelpTerm term="brake state">TRIPPED</HelpTerm> —{' '}
        {retrying ? (
          <HelpTerm term="sweep (guardrail)">SWEEP RETRYING</HelpTerm>
        ) : (
          reason
        )}
      </div>
      {retrying && <div className="gr-banner-reason">{reason}</div>}
      <div className="gr-banner-note">
        {g.trip_id !== null && `trip #${g.trip_id} · `}
        {/* Only 'complete' earns the completed sentence. A null sweep_state is
            the row not SAYING — never evidence the venue was swept. */}
        {retrying
          ? `the trip's venue sweep has not finished (sweep_state: ${g.sweep_state}) — ` +
            'every cycle RETRIES it, and DISARM resumes that same sweep. Retrying, ' +
            'not failed: entry orders may still be working at the broker until it ' +
            'completes.'
          : g.sweep_state === 'complete'
            ? 'the sweep completed — entry orders were pulled, protective stops ' +
              'kept. Nothing dispatches until the trip is cleared.'
            : (g.sweep_state === null
                ? 'sweep state not reported'
                : `sweep state: ${g.sweep_state}`) +
              ' — nothing dispatches until the trip is cleared.'}
      </div>
    </div>
  )
}

/* ---------- what actually resolves a breach ---------- */

/** Clearing a trip NEVER resolves the breach that caused it — it only releases
 * the brake, and the next hourly consult re-trips on the same breaker. What the
 * operator needs is therefore per-BREAKER: the drawdown is cumulative and only a
 * re-anchored high-water mark moves it, while the day-scoped breakers simply
 * roll over and the streak breaks on a win. Naming the drawdown remedy at every
 * breach (the first cut did) sends an operator to reset an anchor that has
 * nothing to do with the cap they actually hit. */
const BREACH_REMEDY: Record<string, string> = {
  max_drawdown_usd:
    'the drawdown is measured from the high-water anchor, which has not moved — ' +
    're-anchor it (beside this) or raise the cap',
  max_trades_per_day:
    'the count resets at the next trading day of record — or raise the cap in ' +
    'BREAKERS below',
  max_daily_loss_usd:
    "the day's realized loss resets at the next trading day of record — or raise " +
    'the cap in BREAKERS below',
  loss_streak_halt:
    'the streak resets on the next winning close — or raise the threshold in ' +
    'BREAKERS below',
}

/** The drawdown is the ONE breaker with a cockpit-side remedy, so it is the one
 * breach that gets the anchor-reset form offered beside the clear. */
function isAnchorBreach(breaker: string): boolean {
  return breaker === 'max_drawdown_usd'
}

/** The re-trip warning. `pending` = the clear has not happened yet (the dialog);
 * false = it already has (the result panel), which only changes the tense. */
function BreachAlarm({
  breach,
  pending,
  announce = true,
}: {
  breach: GuardrailBreach
  pending: boolean
  announce?: boolean
}) {
  return (
    <div className="gr-alarm" role={announce ? 'alert' : undefined}>
      breach still active — {pending ? 'clearing' : 'this'} will re-trip (sweep +
      email) within the hour
      {isAnchorBreach(breach.breaker) && ' unless you also reset the drawdown anchor'}
      <div className="gr-alarm-detail mono">
        {breach.breaker}: {breach.reason}
      </div>
      <div className="gr-alarm-remedy">
        what clears it:{' '}
        {BREACH_REMEDY[breach.breaker] ??
          'this breaker has no remedy on record — the breach is real either way'}
      </div>
    </div>
  )
}

/* ---------- the limits form ---------- */

interface LimitField {
  key: keyof GuardrailLimits
  label: string
  kind: 'num' | 'int' | 'date'
  note: string
  /** No unset: the column is NOT NULL, so a null is a 422 and never a disarm. */
  required?: boolean
  /** Glossary key — when set, the LABEL becomes an inline HelpTerm (the
   * JournalScreen Metric idiom). Only the fields whose meaning is a defined
   * term carry one; the three day/count caps say what they are on the tin. */
  term?: string
}

const LIMIT_FIELDS: LimitField[] = [
  {
    key: 'max_daily_loss_usd',
    label: 'max daily loss $',
    kind: 'num',
    note: "today's realized loss in dollars",
  },
  {
    key: 'max_trades_per_day',
    label: 'max trades / day',
    kind: 'int',
    note: 'counted orders on the day of record',
  },
  {
    key: 'max_drawdown_usd',
    label: 'max drawdown $',
    kind: 'num',
    note: 'measured from the high-water anchor below',
  },
  {
    key: 'loss_streak_halt',
    label: 'loss streak halt',
    kind: 'int',
    note: 'consecutive losing closes',
    term: 'loss streak',
  },
  {
    key: 'hwm_anchor_date',
    label: 'drawdown anchor date',
    kind: 'date',
    note: 'the day the drawdown is measured from',
    term: 'drawdown anchor / high-water mark',
  },
  {
    key: 'hwm_baseline_usd',
    label: 'high-water baseline $',
    kind: 'num',
    required: true,
    note: 'equity at that anchor — NOT NULL, so it has no unset',
    term: 'drawdown anchor / high-water mark',
  },
]

type Draft = Record<string, string>

/** The wire's six editable columns as form strings; '' spells "unset (null)". */
function draftOf(g: GuardrailLimits): Draft {
  const d: Draft = {}
  for (const f of LIMIT_FIELDS) {
    const v = g[f.key]
    d[f.key] = v === null ? '' : String(v)
  }
  return d
}

type Parsed = { ok: true; value: number | string | null } | { ok: false; msg: string }

/** One field string → its wire value. The non-finite guard is load-bearing:
 * `JSON.stringify({x: NaN})` emits `null`, and a null on this endpoint UNSETS the
 * breaker — so a typo'd cap would silently DISARM the very thing being set. */
function parseField(f: LimitField, raw: string): Parsed {
  const s = raw.trim()
  if (s === '') {
    return f.required
      ? { ok: false, msg: `${f.label} has no unset — the column is NOT NULL` }
      : { ok: true, value: null }
  }
  if (f.kind === 'date') return { ok: true, value: s }
  const n = Number(s)
  if (!Number.isFinite(n)) return { ok: false, msg: `${f.label} must be a number` }
  if (f.kind === 'int' && !Number.isInteger(n)) {
    return { ok: false, msg: `${f.label} must be a whole number` }
  }
  // Positivity is the REPO's rule and its message is the one the tests pin —
  // never restated here, so the two can't drift.
  return { ok: true, value: n }
}

function shown(v: string): string {
  return v === '' ? 'unset' : v
}

function LimitsForm({
  g,
  stale,
  onWrote,
}: {
  g: Guardrails
  /** The poll is failing: the values below are the last GOOD read, not current.
   * The form still renders (drafts must survive a bad poll) — it just stops
   * claiming the numbers are live. */
  stale: boolean
  onWrote: () => void
}) {
  const wire = draftOf(g)
  // Separator-joined: '1'+'2' and '12'+'' must not alias into one key.
  const wireKey = LIMIT_FIELDS.map((f) => wire[f.key]).join('|')

  const [base, setBase] = useState<Draft>(wire)
  const [draft, setDraft] = useState<Draft>(wire)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<WriteErr | null>(null)
  const [fieldErrs, setFieldErrs] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<string | null>(null)

  const changed = LIMIT_FIELDS.filter((f) => draft[f.key] !== base[f.key])
  const dirty = changed.length > 0
  // The row moved under the form (our own save, or another process's edit).
  const wireMoved = LIMIT_FIELDS.some((f) => base[f.key] !== wire[f.key])

  // Resync from the wire ONLY when the operator has nothing pending — a poll
  // must never stomp typed input. While an edit IS pending the fields keep what
  // was loaded (that is what "presence" is diffed against) and the note below
  // says the row moved, rather than silently mixing two snapshots.
  const liveRef = useRef({ wire, dirty })
  liveRef.current = { wire, dirty }
  useEffect(() => {
    if (liveRef.current.dirty) return
    setBase(liveRef.current.wire)
    setDraft(liveRef.current.wire)
  }, [wireKey])

  const set = (key: string, value: string) => {
    setDraft((d) => ({ ...d, [key]: value }))
    setSaved(null)
  }

  const submit = () => {
    setErr(null)
    setFieldErrs({})
    setSaved(null)
    if (!dirty) {
      setErr({ text: 'nothing changed — edit a field first', g7: false })
      return
    }
    // PRESENCE, not value: only the fields the operator actually touched ride
    // the body. An omitted key is left alone; an explicit null unsets.
    const body: GuardrailEditBody = { action: 'edit' }
    for (const f of changed) {
      const p = parseField(f, draft[f.key])
      if (!p.ok) {
        setErr({ text: p.msg, g7: false })
        setFieldErrs({ [f.key]: p.msg })
        return
      }
      Object.assign(body, { [f.key]: p.value })
    }
    const sent: Draft = { ...draft }
    setBusy(true)
    postGuardrails(body).then(
      (r: GuardrailsPostResult) => {
        setBusy(false)
        // What was SENT is the new baseline: without this the form stays
        // permanently "dirty" (draft != base), which both keeps offering to
        // re-send the same edit and blocks the poll's resync forever. Anything
        // typed DURING the request stays pending, which is correct.
        setBase(sent)
        setSaved(
          r.enrichment_error != null
            ? `saved — the edit COMMITTED, but the follow-up read failed ` +
              `(${r.enrichment_error}); the values below re-poll`
            : 'saved — the limits below are the fresh row',
        )
        onWrote()
      },
      (e: unknown) => {
        setBusy(false)
        setErr(writeError(e, true))
        setFieldErrs(fieldErrorsOf(e))
      },
    )
  }

  // Discard the pending edit and show TRUTH — the current wire, not the snapshot
  // this form loaded (they differ when another process edited meanwhile).
  const revert = () => {
    setBase(wire)
    setDraft(wire)
    setErr(null)
    setFieldErrs({})
    setSaved(null)
  }

  return (
    <div className="gr-form">
      <div className="gr-sub">
        BREAKERS
        <span className="gr-sub-note">
          each one is consulted before dispatch · blank = that breaker is OFF
        </span>
      </div>
      {stale && (
        <div className="gr-note-row">
          the brake read is failing — these are the LAST GOOD values, not
          necessarily the current ones. Your edit still sends (the server is the
          authority); it just cannot be previewed against fresh state.
        </div>
      )}
      <div className="gr-fields">
        {LIMIT_FIELDS.map((f) => (
          /* EXPLICIT htmlFor/id (the AnalysisPanel / LogTradeForm house pattern),
             NOT the wrapping label's implicit association — because a HelpTerm
             renders a <button>, and a <button> is a LABELABLE element. Wrapped
             implicitly, the label's control resolves to its first labelable
             descendant, i.e. the help affordance, and the input beside it is left
             with NO accessible name at all (the term-carrying fields, verified in
             the browser: input.labels.length was 0). The explicit pairing pins the
             control to the input; the button is then just content inside the
             label, and being interactive content it also does not forward its
             click to the field. */
          <label className="gr-field" key={f.key} htmlFor={`gr-limit-${f.key}`}>
            <span className="gr-lab">
              {f.term === undefined ? f.label : <HelpTerm term={f.term}>{f.label}</HelpTerm>}
              <em className="gr-lab-note">{f.note}</em>
            </span>
            <input
              id={`gr-limit-${f.key}`}
              className="gr-in mono"
              type={f.kind === 'date' ? 'date' : 'number'}
              step={f.kind === 'int' ? '1' : 'any'}
              value={draft[f.key]}
              disabled={busy}
              placeholder={f.required ? '' : 'unset'}
              aria-invalid={fieldErrs[f.key] !== undefined || undefined}
              onChange={(e) => set(f.key, e.target.value)}
            />
            {fieldErrs[f.key] !== undefined && (
              <span className="gr-field-err">{fieldErrs[f.key]}</span>
            )}
          </label>
        ))}
      </div>

      <div className="gr-preview mono">
        {dirty ? (
          <>
            will send:{' '}
            {changed
              .map((f) => `${f.key} ${shown(base[f.key])} → ${shown(draft[f.key])}`)
              .join(' · ')}
            <span className="gr-preview-note">
              {' '}
              — untouched keys are omitted (left alone); a blanked field UNSETS that
              breaker
            </span>
          </>
        ) : (
          <span className="gr-preview-note">
            no pending changes — editing a cap never touches the brake state
          </span>
        )}
      </div>

      {/* Only while an edit is PENDING: with nothing pending the effect above has
          already resynced, and a just-saved form would otherwise flash this note
          for the one tick before its own write comes back on the wire. */}
      {wireMoved && dirty && (
        <div className="gr-note-row">
          the brake row changed while you were editing — the fields above still show
          what you loaded; revert to see the current values
        </div>
      )}
      {saved !== null && <div className="gr-saved">{saved}</div>}
      {err !== null && <ErrLine err={err} />}

      <div className="gr-actions-row">
        <button
          type="button"
          className="gr-btn gr-submit"
          onClick={submit}
          disabled={busy}
        >
          {busy ? 'saving…' : 'SAVE LIMITS'}
        </button>
        <button
          type="button"
          className="gr-btn"
          onClick={revert}
          disabled={busy || !dirty}
        >
          revert
        </button>
      </div>
    </div>
  )
}

/* ---------- HALT (the DisarmControl composition, verbatim) ---------- */

/** The sweep block a halt response is CONTRACTUALLY required to carry — this
 * stand-in exists so a wire that somehow omits it degrades to a stated "no sweep
 * block on the response", never a crash and never an invented clean sweep. */
const EMPTY_SWEEP: GuardrailSweep = {
  ran: false,
  detail: 'no sweep block on the response',
  cancelled: [],
  sells_kept: 0,
  stops_restored: [],
  unprotected: [],
}

/** What a halt's sweep reported, rendered EXACTLY from the wire — verbs follow
 * the response's own dry_run flag, and a non-empty `unprotected` is the alarm.
 * `ran: false` WITH a detail is the no-broker path: the brake is DB truth and it
 * holds; the venue simply was not swept. */
function SweepOutcome({ sweep, dry }: { sweep: GuardrailSweep; dry: boolean }) {
  if (!sweep.ran && sweep.detail !== '') {
    return (
      <div className="gr-row gr-dim">
        the venue was not swept — {sweep.detail}
      </div>
    )
  }
  return (
    <>
      <div className="gr-row">
        <span className="gr-verb">{dry ? 'would cancel' : 'cancelled'}</span>{' '}
        {sweep.cancelled.length === 0
          ? 'no entry orders'
          : `${sweep.cancelled.length} entry order${
              sweep.cancelled.length === 1 ? '' : 's'
            }: ${sweep.cancelled.map((o) => `${o.symbol} (${o.broker_order_id})`).join(', ')}`}
      </div>
      <div className="gr-row">
        <span className="gr-verb">{dry ? 'keeps' : 'kept'}</span> {sweep.sells_kept}{' '}
        protective sell order{sweep.sells_kept === 1 ? '' : 's'}
      </div>
      <div className="gr-row">
        <span className="gr-verb">{dry ? 'would restore' : 'restored'}</span>{' '}
        {sweep.stops_restored.length === 0
          ? sweep.unprotected.length === 0
            ? 'no stops (none were missing)'
            : 'none'
          : `stops for ${sweep.stops_restored.join(', ')} — at the recorded level, copied never computed`}
      </div>
      {sweep.unprotected.length > 0 && (
        <div className="gr-alarm" role="alert">
          UNPROTECTED — no recorded stop level anywhere for{' '}
          {sweep.unprotected.join(', ')}. Left alone (never auto-closed) — this needs
          your hand at the broker.
        </div>
      )}
    </>
  )
}

/** The four actions in operator English. The wire's `action` is a snake_case
 * enum; an operator reading a result panel mid-emergency should not have to
 * translate `clear_trip` in their head. */
const COMMITTED_PHRASE: Record<string, string> = {
  edit: 'limits saved',
  halt: 'the brake is now HALTED',
  clear_halt: 'the HALT is cleared',
  clear_trip: 'the trip is cleared',
}

/** The wire's own account of what a committed write did. */
function CommitLine({ r }: { r: GuardrailsPostResult }) {
  return (
    <>
      <div className="gr-row">
        {r.committed
          ? `${COMMITTED_PHRASE[r.action] ?? 'the write landed'} — the brake row moved`
          : 'preview only — nothing moved: not the state, not a row, not the venue'}
      </div>
      {r.enrichment_error != null && (
        <div className="gr-warn-row">
          the follow-up read failed ({r.enrichment_error}) — the WRITE STANDS; the
          panel re-polls for the fresh state
        </div>
      )}
    </>
  )
}

type HaltPhase =
  | { kind: 'idle' }
  | { kind: 'preview'; result: GuardrailsPostResult | null } // holding; null = in flight
  | { kind: 'firing' }
  | { kind: 'done'; result: GuardrailsPostResult }
  | { kind: 'blocked'; detail: string } // the 409 states, wire detail verbatim
  | { kind: 'failed'; err: WriteErr; partial: boolean }

function haltFailure(err: unknown, realRun: boolean): HaltPhase {
  if (err instanceof ApiError && err.status === 409) {
    return { kind: 'blocked', detail: err.message }
  }
  // A dry run moved nothing. A REAL halt that failed has ALREADY engaged the
  // brake (persist-first) and may have swept part of the venue before dying.
  return { kind: 'failed', err: writeError(err, realRun), partial: realRun }
}

function HaltControl({ g, onWrote }: { g: Guardrails; onWrote: () => void }) {
  const [phase, setPhase] = useState<HaltPhase>({ kind: 'idle' })
  // Orphans stale preview responses: bumped on every hold-start, abort and fire.
  const seqRef = useRef(0)
  const previewCtrlRef = useRef<AbortController | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const dismissRef = useRef<HTMLButtonElement>(null)

  const panelUp =
    phase.kind === 'done' || phase.kind === 'blocked' || phase.kind === 'failed'
  // Alarm panels: unprotected positions, or a halt whose sweep died partway.
  // These demand an explicit dismissal (no Escape) — deliberate friction.
  const alarmUp =
    (phase.kind === 'done' && (phase.result.sweep?.unprotected.length ?? 0) > 0) ||
    (phase.kind === 'failed' && phase.partial)
  // 'ok' is the only state HALT can leave: the repo's transition is ok -> halted.
  const disabled = g.state !== 'ok' || phase.kind === 'firing' || panelUp

  // The result panel is the NEAREST reason the button is dead — say that first,
  // or an operator staring at their own HALT result is told about the state
  // instead of about the dismiss button in front of them.
  const title = panelUp
    ? 'dismiss the HALT result first'
    : g.state !== 'ok'
      ? `nothing to halt — the brake is already engaged (state: ${g.state})`
      : 'hold 900 ms to HALT — stops dispatch and sweeps resting entry orders'

  const focusBackRef = useRef(false)
  const dismiss = () => {
    focusBackRef.current = true
    setPhase({ kind: 'idle' })
  }

  // Completing a hold disables the button, so focus would fall to <body> at the
  // moment a keyboard user needs to read the result — land it on the dismiss
  // control, and hand it back post-commit when the panel goes away.
  useEffect(() => {
    if (panelUp) {
      dismissRef.current?.focus()
    } else if (focusBackRef.current) {
      focusBackRef.current = false
      btnRef.current?.focus()
    }
  }, [panelUp, phase.kind])

  // Escape dismisses the NON-alarm panels only.
  useEffect(() => {
    if (!panelUp || alarmUp) return undefined
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dismissRef.current?.click()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [panelUp, alarmUp])

  const onHoldStart = () => {
    const seq = ++seqRef.current
    const ctrl = new AbortController()
    previewCtrlRef.current = ctrl
    setPhase({ kind: 'preview', result: null })
    // dry_run=1: LOCK-FREE server-side, so an abandoned preview can never 409
    // the emergency DISARM the operator reaches for next.
    postGuardrails({ action: 'halt' }, true, ctrl.signal).then(
      (r) => {
        if (seqRef.current !== seq) return
        setPhase((p) => (p.kind === 'preview' ? { kind: 'preview', result: r } : p))
      },
      (err: unknown) => {
        if (seqRef.current !== seq) return
        setPhase(haltFailure(err, false)) // vetoes the hold via `vetoed` below
      },
    )
  }

  const onAbort = () => {
    seqRef.current++ // orphan the in-flight preview FIRST
    previewCtrlRef.current?.abort()
    previewCtrlRef.current = null
    setPhase((p) => (p.kind === 'preview' ? { kind: 'idle' } : p))
  }

  const onFire = () => {
    // HoldToConfirm calls this only when armed — i.e. the preview LANDED.
    seqRef.current++
    previewCtrlRef.current = null
    setPhase({ kind: 'firing' })
    postGuardrails({ action: 'halt' }).then(
      (r) => {
        setPhase({ kind: 'done', result: r })
        onWrote()
      },
      (err: unknown) => {
        setPhase(haltFailure(err, true))
        onWrote() // the brake engaged before the failure — refetch the truth
      },
    )
  }

  const label = (
    <span className="dz-btn-label">
      <b>HALT</b>
      <small>stops dispatch, sweeps resting entries</small>
    </span>
  )

  return (
    <div className="gr-ctl">
      <HoldToConfirm
        holdMs={900}
        className="disarm"
        title={title}
        disabled={disabled}
        armed={phase.kind === 'preview' && phase.result !== null}
        vetoed={phase.kind === 'blocked' || phase.kind === 'failed'}
        buttonRef={btnRef}
        label={
          phase.kind === 'firing' ? (
            <span className="dz-btn-label">
              <b>HALTING…</b>
            </span>
          ) : (
            label
          )
        }
        waitingLabel={
          <span className="dz-btn-label">
            <b>HALT</b>
            <small>waiting on the dry-run preview…</small>
          </span>
        }
        onHoldStart={onHoldStart}
        onFire={onFire}
        onAbort={onAbort}
      />

      {phase.kind === 'preview' && (
        <div className="gr-pop" role="status">
          <div className="gr-pop-head">DRY-RUN PREVIEW</div>
          {phase.result === null ? (
            <div className="gr-row gr-dim">querying the venue…</div>
          ) : (
            <>
              <SweepOutcome sweep={phase.result.sweep ?? EMPTY_SWEEP} dry />
              <div className="gr-foot">
                the brake has NOT moved — this preview changed nothing
              </div>
            </>
          )}
          <div className="gr-foot">
            keep holding to fire the real HALT · release or Esc aborts
          </div>
        </div>
      )}

      {phase.kind === 'firing' && (
        <div className="gr-pop" role="status">
          <div className="gr-pop-head">HALT — FIRING</div>
          <div className="gr-row gr-dim">engaging the brake, then sweeping…</div>
        </div>
      )}

      {phase.kind === 'done' && (
        <div className="gr-pop" role="status">
          <div className="gr-pop-head">
            HALT ENGAGED
            {(phase.result.sweep?.unprotected.length ?? 0) > 0 && (
              <span className="gr-head-alarm"> — UNPROTECTED POSITIONS</span>
            )}
          </div>
          <CommitLine r={phase.result} />
          <SweepOutcome sweep={phase.result.sweep ?? EMPTY_SWEEP} dry={false} />
          <button
            type="button"
            className="gr-dismiss"
            ref={dismissRef}
            onClick={dismiss}
          >
            dismiss
          </button>
        </div>
      )}

      {phase.kind === 'blocked' && (
        <div className="gr-pop" role="status">
          <div className="gr-pop-head">HALT BLOCKED</div>
          <div className="gr-row">{phase.detail}</div>
          <div className="gr-foot">
            nothing moved by this request
            {phase.detail.includes('in flight') &&
              ' — a protective action holds the single-flight lock (this window or another); try again when it finishes'}
          </div>
          <button
            type="button"
            className="gr-dismiss"
            ref={dismissRef}
            onClick={dismiss}
          >
            dismiss
          </button>
        </div>
      )}

      {phase.kind === 'failed' && (
        <div className="gr-pop" role={phase.partial ? 'alert' : 'status'}>
          <div className="gr-pop-head">
            HALT FAILED
            {phase.partial && <span className="gr-head-alarm"> — SWEEP UNCERTAIN</span>}
          </div>
          <ErrLine err={phase.err} announce={false} />
          {phase.partial ? (
            <div className="gr-alarm">
              The BRAKE IS ON — it lands before the venue is touched, so the halt
              itself is durable (the banner above will say HALTED). What failed is
              the sweep: resting entry orders may still be working. Verify at the
              broker, and DISARM if in doubt.
            </div>
          ) : (
            <div className="gr-foot">the preview touched nothing — the brake is unchanged</div>
          )}
          <button
            type="button"
            className="gr-dismiss"
            ref={dismissRef}
            onClick={dismiss}
          >
            dismiss
          </button>
        </div>
      )}
    </div>
  )
}

/* ---------- clearing: the halt release and the trip acknowledgement ---------- */

type ReleaseOutcome =
  | { kind: 'done'; result: GuardrailsPostResult }
  | { kind: 'failed'; err: WriteErr; blocked: boolean }

/** A hold-to-confirm brake RELEASE. No preview: these actions are pure DB
 * transitions with no venue side, and the server 422s `dry_run` on them rather
 * than letting a "preview" quietly release the brake.
 *
 * The OUTCOME is reported UP, never rendered here — CloseTradeForm's precedent.
 * A successful clear flips the state on the next poll, which unmounts the very
 * control that ran it: a result panel living inside would vanish with it, taking
 * the "breach still active — this will re-trip" alarm along. That alarm is the
 * whole point of the flow, so it is rendered by the panel, which survives. */
function ReleaseControl({
  label,
  caption,
  title,
  disabled = false,
  ariaDisabled = false,
  describedBy,
  body,
  onWrote,
  onOutcome,
}: {
  label: string
  caption: string
  title: string
  disabled?: boolean
  /** States the dead-ness for AT when `disabled` is a GATE the operator can
   * lift (the trip acknowledgement), not a permanent condition. */
  ariaDisabled?: boolean
  /** The element that explains how to lift that gate. */
  describedBy?: string
  body: () => { action: 'clear_halt' } | { action: 'clear_trip'; ack_trip_id: number }
  onWrote: () => void
  onOutcome: (o: ReleaseOutcome) => void
}) {
  const [firing, setFiring] = useState(false)

  const onFire = () => {
    setFiring(true)
    postGuardrails(body()).then(
      (r) => {
        setFiring(false)
        onOutcome({ kind: 'done', result: r })
        onWrote()
      },
      (err: unknown) => {
        setFiring(false)
        onOutcome({
          kind: 'failed',
          err: writeError(err, true),
          blocked: err instanceof ApiError && err.status === 409,
        })
        onWrote()
      },
    )
  }

  return (
    <div className="gr-ctl">
      <HoldToConfirm
        holdMs={900}
        className="release"
        title={title}
        disabled={disabled || firing}
        ariaDisabled={ariaDisabled}
        describedBy={describedBy}
        label={
          <span className="dz-btn-label">
            <b>{firing ? 'CLEARING…' : label}</b>
            <small>{caption}</small>
          </span>
        }
        onFire={onFire}
      />
    </div>
  )
}

/** The release outcome, rendered at PANEL level so it outlives the control that
 * produced it. Persistent until dismissed — a re-trip warning must not be
 * something the operator can miss by blinking. */
function ReleaseResult({
  outcome,
  onDismiss,
}: {
  outcome: ReleaseOutcome
  onDismiss: () => void
}) {
  const dismissRef = useRef<HTMLButtonElement>(null)
  // The button that had focus was destroyed by the state flip — land focus on
  // the panel's own dismiss control (post-commit, from an effect).
  useEffect(() => {
    dismissRef.current?.focus()
  }, [outcome])

  const alarm = outcome.kind === 'done' && outcome.result.current_breach != null
  return (
    <div className="gr-pop gr-pop-wide" role={alarm ? 'alert' : 'status'}>
      <div className="gr-pop-head">
        {outcome.kind === 'done' ? 'BRAKE RELEASED' : 'CLEAR REFUSED'}
        {alarm && <span className="gr-head-alarm"> — WILL RE-TRIP</span>}
      </div>
      {outcome.kind === 'done' ? (
        <>
          <CommitLine r={outcome.result} />
          {outcome.result.current_breach != null && (
            // announce=false: this panel IS the live region (role above).
            <BreachAlarm
              breach={outcome.result.current_breach}
              pending={false}
              announce={false}
            />
          )}
        </>
      ) : (
        <>
          <ErrLine err={outcome.err} announce={false} />
          <div className="gr-foot">
            {outcome.blocked
              ? 'a state, not a failure — the brake was not where this screen thought it was (a stale screen cannot release a newer trip); the panel re-polls'
              : 'the brake did NOT move'}
          </div>
        </>
      )}
      <button type="button" className="gr-dismiss" ref={dismissRef} onClick={onDismiss}>
        dismiss
      </button>
    </div>
  )
}

/** The drawdown anchor reset offered BESIDE the trip clear: an ordinary `edit`
 * setting today as the anchor and a fresh baseline. It is the sanctioned remedy
 * for a cumulative breaker whose breach is still real — clearing alone just
 * re-trips on the next hourly consult, by design. */
function AnchorReset({ g, onWrote }: { g: Guardrails; onWrote: () => void }) {
  const wireBaseline = String(g.hwm_baseline_usd)
  const [day, setDay] = useState(localTodayIso())
  const [baseline, setBaseline] = useState(wireBaseline)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<WriteErr | null>(null)
  const [done, setDone] = useState(false)
  // Untouched = still exactly what the row said when this was seeded, so a row
  // that moves under an untouched form re-seeds. This control now lives across
  // read failures and across trips; without it the operator would be offered a
  // baseline from a snapshot two trips old. A TOUCHED field is never overwritten.
  const [seeded, setSeeded] = useState(wireBaseline)
  const liveRef = useRef({ wireBaseline, touched: baseline !== seeded })
  liveRef.current = { wireBaseline, touched: baseline !== seeded }
  useEffect(() => {
    if (liveRef.current.touched) return
    setSeeded(liveRef.current.wireBaseline)
    setBaseline(liveRef.current.wireBaseline)
    setDay(localTodayIso())
    setDone(false)
  }, [wireBaseline])

  const n = Number(baseline.trim())
  const parsable = baseline.trim() !== '' && Number.isFinite(n)

  const submit = () => {
    setErr(null)
    setDone(false)
    if (!parsable) {
      setErr({ text: 'high-water baseline must be a number', g7: false })
      return
    }
    setBusy(true)
    postGuardrails({
      action: 'edit',
      hwm_anchor_date: day,
      hwm_baseline_usd: n,
    }).then(
      () => {
        setBusy(false)
        setDone(true)
        onWrote()
      },
      (e: unknown) => {
        setBusy(false)
        setErr(writeError(e, true))
      },
    )
  }

  return (
    <div className="gr-anchor">
      <div className="gr-sub">
        <HelpTerm term="drawdown anchor / high-water mark">
          RESET THE DRAWDOWN ANCHOR
        </HelpTerm>
        <span className="gr-sub-note">
          re-bases the high-water mark so the cumulative breaker stops measuring an
          old peak
        </span>
      </div>
      <div className="gr-fields gr-fields-2">
        <label className="gr-field">
          <span className="gr-lab">anchor date</span>
          <input
            className="gr-in mono"
            type="date"
            value={day}
            disabled={busy}
            onChange={(e) => setDay(e.target.value)}
          />
        </label>
        <label className="gr-field">
          <span className="gr-lab">
            high-water baseline $
            <em className="gr-lab-note">
              realized $ at the new anchor — 0 starts fresh from here
            </em>
          </span>
          <input
            className="gr-in mono"
            type="number"
            step="any"
            value={baseline}
            disabled={busy}
            aria-invalid={!parsable || undefined}
            onChange={(e) => setBaseline(e.target.value)}
          />
        </label>
      </div>
      {done && <div className="gr-saved">anchor reset — the edit committed</div>}
      {err !== null && <ErrLine err={err} />}
      <button
        type="button"
        className="gr-btn gr-submit"
        onClick={submit}
        disabled={busy}
      >
        {busy ? 'saving…' : 'RESET ANCHOR'}
      </button>
    </div>
  )
}

/** The trip acknowledgement: read the trip, tick the box, then hold.
 *
 * The checkbox gates the hold through `disabled`, NOT through HoldToConfirm's
 * `armed`. `armed` is an ASYNC WAIT-GATE — a completed hold sits waiting and
 * fires the instant it flips true — so wiring a pre-ticked checkbox through it
 * would fire the release the moment the hold timer ticked, with no gate at all. */
function ClearTripControl({
  g,
  onWrote,
  onOutcome,
}: {
  g: Guardrails
  onWrote: () => void
  onOutcome: (o: ReleaseOutcome) => void
}) {
  const [acked, setAcked] = useState(false)
  const tripId = g.trip_id
  const breach = g.current_breach

  // A NEW trip must never inherit the previous acknowledgement. This control now
  // survives read failures and state changes, so without the reset a box ticked
  // for trip #3 would still be ticked when #4 lands — under a label that then
  // reads "I have read trip #4". (The server's ack_trip_id election would still
  // refuse the stale clear; this is about the screen not lying first.)
  useEffect(() => {
    setAcked(false)
  }, [tripId])

  // A tripped row with no trip_id cannot be acknowledged: the clear is keyed on
  // the id the operator READ, and there is nothing to key on. Say so plainly.
  if (tripId === null) {
    return (
      <div className="gr-note-row">
        this trip carries no id — it cannot be acknowledged from here; DISARM still
        sweeps the venue unconditionally
      </div>
    )
  }

  const ackId = `gr-ack-${tripId}`
  return (
    <div className="gr-clear">
      <div className="gr-sub">
        CLEAR THE <HelpTerm term="trip">TRIP</HelpTerm>
        <span className="gr-sub-note">
          releases the brake · the breach that caused it is NOT resolved by clearing
        </span>
      </div>
      {breach !== null && <BreachAlarm breach={breach} pending />}
      <div className="gr-clear-body">
        <div className="gr-clear-ack">
          <label className="gr-check" id={ackId}>
            <input
              type="checkbox"
              checked={acked}
              onChange={(e) => setAcked(e.target.checked)}
            />
            <span>
              I have read trip #{tripId}: {g.trip_reason ?? 'no reason recorded'}
            </span>
          </label>
          <ReleaseControl
            label="CLEAR TRIP"
            caption={`acknowledges trip #${tripId}`}
            title={
              acked
                ? `hold 900 ms to clear trip #${tripId}`
                : 'acknowledge the trip first'
            }
            disabled={!acked}
            ariaDisabled={!acked}
            describedBy={ackId}
            body={() => ({ action: 'clear_trip', ack_trip_id: tripId })}
            onWrote={onWrote}
            onOutcome={onOutcome}
          />
        </div>
        {/* The anchor reset is the DRAWDOWN's remedy and only its remedy — a
            day-scoped cap or a loss streak is not resolved by re-anchoring, and
            offering the form there sends the operator to move a number that has
            nothing to do with the cap they hit (BREACH_REMEDY says what does). */}
        {breach !== null && isAnchorBreach(breach.breaker) && (
          <AnchorReset g={g} onWrote={onWrote} />
        )}
      </div>
    </div>
  )
}

/* ---------- the event history ---------- */

/* A LIST, not a table: this panel lives in the screen's fixed 380px column, and
   the reasons are full sentences ("max trades/day: 1 >= 1", a sweep's own
   itemized summary) — a five-column table shreds them one character wide. Meta
   line + reason, the jr-note idiom. */
function EventHistory({ events }: { events: GuardrailEvent[] }) {
  if (events.length === 0) {
    return <div className="panel-wait">no guardrail events yet</div>
  }
  return (
    <div className="gr-events">
      {/* Sliced client-side too: the caption promises "last 25", and a server
          that ever widens its page must not silently make that caption a lie. */}
      {events.slice(0, EVENT_LIMIT).map((e) => (
        <div className="gr-ev" key={e.id}>
          <div className="gr-ev-meta mono">
            <span className={`gr-kind gr-kind-${e.kind}`}>{e.kind}</span>
            <span className="gr-when">{fmtStamp(e.created_at)}</span>
            {e.breaker !== '' && <span className="gr-brk">{e.breaker}</span>}
            <span className="spacer" />
            <span className="gr-src">{e.source}</span>
          </div>
          {e.reason !== '' && <div className="gr-reason">{e.reason}</div>}
        </div>
      ))}
    </div>
  )
}

/* ---------- the panel ---------- */

/** What replaces a DATA section when the read failed. Only truth CLAIMS route
 * through here — never a control, never a form (see the panel). */
function DataUnknown({ error, noun }: { error: string | null; noun: string }) {
  return (
    <div className="sfy-unknown-block">
      {error !== null
        ? `UNKNOWN — the brake read failed (${error}). Not "ok": the ${noun} was not read.`
        : 'waiting for first fetch…'}
    </div>
  )
}

export function GuardrailsPanel({ wake }: { wake: number }) {
  // A local bump refetches THIS panel the instant a write lands; the server's
  // action nonce wakes every other window over SSE (the designed path).
  const [bump, setBump] = useState(0)
  // The release outcome lives HERE, not in the control: a successful clear flips
  // the state and unmounts that control on the next poll (see ReleaseControl).
  const [release, setRelease] = useState<ReleaseOutcome | null>(null)
  const gr = usePolling(getGuardrails, POLL_MS, wake + bump)
  const onWrote = () => setBump((b) => b + 1)

  // ONE poll, read TWO ways, and the split is the design:
  //
  // * `live` is force-nulled on any fetch error and drives every truth CLAIM —
  //   the state banner and the history. A brake panel showing a stale "OK" is
  //   the exact failure this screen's doctrine exists to prevent.
  //
  // * `last` is usePolling's RETAINED last-good snapshot and drives the
  //   CONTROLS, which therefore never unmount on a failed poll. That is not a
  //   convenience: unmounting them destroys in-flight OPERATOR state — a HALT
  //   result panel holding an UNPROTECTED alarm, a half-typed limits edit, a
  //   ticked acknowledgement — and a request still in flight resolves into a
  //   dead component, dropping its result silently. A poll failure is not the
  //   operator's doing and must never cost them their work or their alarm.
  //
  // Acting on a stale snapshot is safe because the SERVER is the authority on
  // every one of these actions: HALT 409s if the brake already moved, and
  // clear_trip's conditional UPDATE refuses an acknowledgement of a trip that is
  // no longer current. The stale line below says so rather than pretending.
  const live = gr.error === null ? gr.data : null
  const last = gr.data
  const stale = gr.error !== null

  return (
    <section className="panel">
      <div className="panel-head">
        <HelpTerm term="guardrail">GUARDRAILS</HelpTerm>
        <span className="panel-caption">
          a brake, not a knob — env stays the master arm · scope: the shared
          agent_guardrails row, read by EVERY process (not this process&apos;s env)
        </span>
      </div>
      <div className="gr-body">
        {live !== null ? (
          <StateBanner g={live} />
        ) : (
          <DataUnknown error={gr.error} noun="row" />
        )}

        {/* OUTSIDE the read swap, all of it — see the split above. */}
        {release !== null && (
          <ReleaseResult outcome={release} onDismiss={() => setRelease(null)} />
        )}

        {last !== null && (
          <>
            {stale && (
              <div className="gr-note-row">
                the controls below still act — they read the last good snapshot,
                and the server refuses a stale action (a HALT on an already-halted
                brake 409s; an acknowledgement of a superseded trip is rejected)
              </div>
            )}
            {/* The one head the controls row lacked: the HALT is the panel's
                only hand-thrown brake, and (unlike a gr-sub over a form) it
                exists to carry the term — a HelpTerm is a <button> and cannot
                live inside the HALT button's own label. */}
            <div className="gr-sub">
              <HelpTerm term="HALT (brake)">HALT</HelpTerm>
              <span className="gr-sub-note">
                the hand brake · releasing it arms nothing — the env master arm still decides
              </span>
            </div>
            <div className="gr-controls">
              <HaltControl g={last} onWrote={onWrote} />
              {last.state === 'halted' && (
                <ReleaseControl
                  label="CLEAR HALT"
                  caption="releases the operator brake"
                  title="hold 900 ms to release the HALT"
                  body={() => ({ action: 'clear_halt' })}
                  onWrote={onWrote}
                  onOutcome={setRelease}
                />
              )}
            </div>
            {last.state === 'tripped' && (
              <ClearTripControl g={last} onWrote={onWrote} onOutcome={setRelease} />
            )}
            <LimitsForm g={last} stale={stale} onWrote={onWrote} />
          </>
        )}

        <div className="gr-sub">
          HISTORY
          <span className="gr-sub-note">
            the append-only audit trail — last {EVENT_LIMIT}, newest first
          </span>
        </div>
        {live !== null ? (
          <EventHistory events={live.events} />
        ) : (
          <DataUnknown error={gr.error} noun="history" />
        )}
      </div>
    </section>
  )
}
