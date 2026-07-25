import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  ApiError,
  D503_DB,
  POLL_MS,
  getGuardrails,
  postGuardrails,
  usePolling,
} from '../lib/api'
import type {
  GuardrailEditBody,
  GuardrailEvent,
  GuardrailLimits,
  GuardrailSweep,
  Guardrails,
  GuardrailsPostResult,
  Polled,
} from '../lib/api'
import { fmtStamp, localTodayIso } from '../lib/fmt'
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
     the one thing a brake panel may never show.
   - a lingering `sweep_state` of pending/partial is RETRYING, never "failed":
     every hourly cycle re-runs that sweep and DISARM resumes the same one. Same
     wording as the auditor's guardrail-stuck-sweep rule.
   - a write that 503s with the `database error (` prefix carries the G7 hint —
     the write grant may be missing in prod. The GET half keeps working read-only
     and the brake is still ENFORCED by the jobs; only the cockpit's own edits
     are refused. */

//: the sweep_state values that mean the trip's sweep has not finished. Same
//: vocabulary as pipeline/guardrails.py's _INCOMPLETE_SWEEPS.
const SWEEP_RETRYING = ['pending', 'partial']

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

function ErrLine({ err }: { err: WriteErr }) {
  return (
    <div className="gr-err" role="alert">
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
        <div className="gr-state">OK</div>
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
        <div className="gr-state">HALTED</div>
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
      <div className="gr-state">TRIPPED — {retrying ? 'SWEEP RETRYING' : reason}</div>
      {retrying && <div className="gr-banner-reason">{reason}</div>}
      <div className="gr-banner-note">
        {g.trip_id !== null && `trip #${g.trip_id} · `}
        {retrying
          ? `the trip's venue sweep has not finished (sweep_state: ${g.sweep_state}) — ` +
            'every cycle RETRIES it, and DISARM resumes that same sweep. Retrying, ' +
            'not failed: entry orders may still be working at the broker until it ' +
            'completes.'
          : 'the sweep completed — entry orders were pulled, protective stops kept. ' +
            'Nothing dispatches until the trip is cleared.'}
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
  },
  {
    key: 'hwm_anchor_date',
    label: 'drawdown anchor date',
    kind: 'date',
    note: 'the day the drawdown is measured from',
  },
  {
    key: 'hwm_baseline_usd',
    label: 'high-water baseline $',
    kind: 'num',
    required: true,
    note: 'equity at that anchor — NOT NULL, so it has no unset',
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

function LimitsForm({ g, onWrote }: { g: Guardrails; onWrote: () => void }) {
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
      <div className="gr-fields">
        {LIMIT_FIELDS.map((f) => (
          <label className="gr-field" key={f.key}>
            <span className="gr-lab">
              {f.label}
              <em className="gr-lab-note">{f.note}</em>
            </span>
            <input
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

/** The wire's own account of what a committed write did. */
function CommitLine({ r }: { r: GuardrailsPostResult }) {
  return (
    <>
      <div className="gr-row">
        {r.committed
          ? `${r.action} committed — the brake row moved`
          : `${r.action} previewed — nothing moved: not the state, not a row, not the venue`}
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

  const title =
    g.state !== 'ok'
      ? `nothing to halt — the brake is already engaged (state: ${g.state})`
      : panelUp
        ? 'dismiss the HALT result first'
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
          <ErrLine err={phase.err} />
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
  body,
  onWrote,
  onOutcome,
}: {
  label: string
  caption: string
  title: string
  disabled?: boolean
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
            <div className="gr-alarm">
              breach still active — {outcome.result.current_breach.breaker}:{' '}
              {outcome.result.current_breach.reason}. This will re-trip (sweep +
              email) within the hour unless you also reset the drawdown anchor.
            </div>
          )}
        </>
      ) : (
        <>
          <ErrLine err={outcome.err} />
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
  const [day, setDay] = useState(localTodayIso())
  const [baseline, setBaseline] = useState(String(g.hwm_baseline_usd))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<WriteErr | null>(null)
  const [done, setDone] = useState(false)

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
        RESET THE DRAWDOWN ANCHOR
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
          <span className="gr-lab">high-water baseline $</span>
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

  return (
    <div className="gr-clear">
      <div className="gr-sub">
        CLEAR THE TRIP
        <span className="gr-sub-note">
          releases the brake · the breach that caused it is NOT resolved by clearing
        </span>
      </div>
      {breach !== null && (
        <div className="gr-alarm" role="alert">
          breach still active — clearing will re-trip (sweep + email) within the hour
          unless you also reset the drawdown anchor
          <div className="gr-alarm-detail mono">
            {breach.breaker}: {breach.reason}
          </div>
        </div>
      )}
      <div className="gr-clear-body">
        <div className="gr-clear-ack">
          <label className="gr-check">
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
            body={() => ({ action: 'clear_trip', ack_trip_id: tripId })}
            onWrote={onWrote}
            onOutcome={onOutcome}
          />
        </div>
        {breach !== null && <AnchorReset g={g} onWrote={onWrote} />}
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
      {events.map((e) => (
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

/** The force-null wrapper, mirroring SafetyScreen's SafetyBody: children render
 * only from a LIVE read. A brake panel showing a stale "OK" is the exact failure
 * this screen's doctrine exists to prevent. */
function GuardBody({
  polled,
  children,
}: {
  polled: Polled<Guardrails>
  children: (g: Guardrails) => ReactNode
}) {
  const g = polled.error === null ? polled.data : null
  if (g !== null) return <>{children(g)}</>
  return (
    <div className="sfy-unknown-block">
      {polled.error !== null
        ? `UNKNOWN — the brake read failed (${polled.error}). Not "ok": the row was not read.`
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

  return (
    <section className="panel">
      <div className="panel-head">
        GUARDRAILS
        <span className="panel-caption">
          a brake, not a knob — env stays the master arm · scope: the shared
          agent_guardrails row, read by EVERY process (not this process&apos;s env)
        </span>
      </div>
      <GuardBody polled={gr}>
        {(g) => (
          <div className="gr-body">
            <StateBanner g={g} />
            {release !== null && (
              <ReleaseResult outcome={release} onDismiss={() => setRelease(null)} />
            )}
            <div className="gr-controls">
              <HaltControl g={g} onWrote={onWrote} />
              {g.state === 'halted' && (
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
            {g.state === 'tripped' && (
              <ClearTripControl g={g} onWrote={onWrote} onOutcome={setRelease} />
            )}
            <LimitsForm g={g} onWrote={onWrote} />
            <div className="gr-sub">
              HISTORY
              <span className="gr-sub-note">
                the append-only audit trail — last 25, newest first
              </span>
            </div>
            <EventHistory events={g.events} />
          </div>
        )}
      </GuardBody>
    </section>
  )
}
