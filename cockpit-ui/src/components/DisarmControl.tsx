import { useEffect, useRef, useState } from 'react'
import { ApiError, D503_SWEEP, postDisarm } from '../lib/api'
import type { DisarmRaw, DisarmResult, DisarmResumePreview, Gate } from '../lib/api'
import { HelpTerm } from './HelpTerm'
import { HoldToConfirm } from './HoldToConfirm'

/* The DISARM subsystem (the sixth action — plan Task 15 / scope decision 1),
   composed into the masthead as one element.

   DISARM is the venue sweep, and ONLY that: cancels entry-side orders, keeps
   (and restores) bracket stops, never closes positions, never computes a stop
   level. It does NOT flip SWING_EXECUTION_MODE — env is per-process. The flow:
   hold-start fires the dry_run=1 preview; the real POST arms only once the
   preview has LANDED (HoldToConfirm's `armed` gate — a completed hold waits on
   the response, it never fires blind).

   The 409s are STATES, not crashes: "no broker configured" and "disarm already
   in flight" render as distinct dismissible panels. A REAL-run failure (503 or
   the network dying mid-POST) is rendered PARTIAL-loud: the router invalidates
   the snapshot and bumps the wake nonce even on the failure path, because a
   partial disarm has still moved venue state.

   MODE-AWARE (Task 16). The response is a `mode`-tagged union and `mode` alone
   is what this branches on. `raw` renders exactly as it always has. On a TRIPPED
   book with an unfinished sweep the endpoint routes through the trip's own
   resume instead (so both processes derive the same client_order_ids and the
   venue collapses the duplicate-stop race): `guardrail-resume` cannot itemize
   what it moved — the pipeline returns a bool — so it renders the sweep's own
   recorded summary and NEVER the empty arrays, which would read as "nothing
   moved". Its dry run (`guardrail-resume-preview`) is the one resume path that
   can itemize, and it does. */
type DisarmPhase =
  | { kind: 'idle' }
  | { kind: 'preview'; result: DisarmResult | null } // holding; null = in flight
  | { kind: 'firing' }
  | { kind: 'done'; result: DisarmResult }
  | { kind: 'blocked'; detail: string } // the 409 states, wire detail verbatim
  | { kind: 'failed'; detail: string; partial: boolean }

function disarmFailure(err: unknown, realRun: boolean): DisarmPhase {
  if (err instanceof ApiError) {
    if (err.status === 409) return { kind: 'blocked', detail: err.message }
    // A dry-run failure moved nothing (dry runs never touch the venue); a
    // real-run failure may have moved SOME venue state before raising. The
    // resume's own 503 (`guardrail sweep did not complete` — one of the three
    // documented prefixes) is exactly that case stated by the server, so it maps
    // onto the same PARTIAL panel even if it ever arrives off a non-real run.
    return {
      kind: 'failed',
      detail: err.message,
      partial: realRun || err.message.startsWith(D503_SWEEP),
    }
  }
  // A bare TypeError — the backend itself is unreachable. For a real run the
  // POST may or may not have executed server-side: outcome unknown, say so.
  return {
    kind: 'failed',
    detail: realRun
      ? 'backend unreachable — the disarm may or may not have run'
      : 'backend unreachable',
    partial: realRun,
  }
}

/** The ITEMIZED rendering — raw runs and the resume PREVIEW, the two shapes whose
 * arrays actually describe the venue. Rendered EXACTLY from the wire: verbs follow
 * the response's own dry_run flag, counts come from the arrays as returned, and a
 * non-empty `unprotected` is the alarm. No optimistic rendering, no fabricated
 * zeros: an empty list renders as the wire's honest "none".
 *
 * NEVER called for `guardrail-resume`: that mode's arrays are empty because the
 * pipeline returns a bool, and "cancelled no entry orders" would be a lie. */
function DisarmItemized({ result }: { result: DisarmRaw | DisarmResumePreview }) {
  const dry = result.dry_run
  return (
    <>
      <div className="dz-row">
        <span className="dz-verb">{dry ? 'would cancel' : 'cancelled'}</span>{' '}
        {result.cancelled.length === 0
          ? 'no entry orders'
          : `${result.cancelled.length} entry order${result.cancelled.length === 1 ? '' : 's'}: ${result.cancelled
              .map((o) => `${o.symbol} (${o.broker_order_id})`)
              .join(', ')}`}
      </div>
      <div className="dz-row">
        <span className="dz-verb">{dry ? 'keeps' : 'kept'}</span> {result.sells_kept}{' '}
        protective sell order{result.sells_kept === 1 ? '' : 's'}
      </div>
      <div className="dz-row">
        <span className="dz-verb">{dry ? 'would restore' : 'restored'}</span>{' '}
        {result.stops_restored.length === 0
          ? // "none were missing" would contradict a non-empty unprotected list
            // one line below (stops WERE missing there — just unrestorable).
            result.unprotected.length === 0
            ? 'no stops (none were missing)'
            : 'none'
          : `stops for ${result.stops_restored.join(', ')} — at the recorded level, copied never computed`}
      </div>
      <UnprotectedAlarm unprotected={result.unprotected} />
    </>
  )
}

/** The UNPROTECTED alarm, rendered from `result.unprotected` and from nothing
 * else — SAME component, SAME condition, every mode. The wire now reports the
 * field faithfully on all three (the resume reads it back off its sweep event's
 * values_json), so the alarm posture can no longer depend on which disarm path
 * ran: identical venue state used to render red-and-no-Escape on the raw path
 * and as calm body text on the resume. */
function UnprotectedAlarm({ unprotected }: { unprotected: string[] }) {
  if (unprotected.length === 0) return null
  return (
    <div className="dz-alarm" role="alert">
      UNPROTECTED — no recorded stop level anywhere for {unprotected.join(', ')}.
      Left alone (never auto-closed) — this needs your hand at the broker.
    </div>
  )
}

/** The venue-side client_order_id prefix both processes derive from the trip.
 * Diagnostic, and deliberately behind a disclosure: an operator mid-emergency
 * should not be handed a raw order key they did not ask for. */
function ResumeKey({ resumeKey }: { resumeKey: string }) {
  return (
    <details className="dz-key">
      <summary>venue key</summary>
      <span className="mono">{resumeKey}</span>
    </details>
  )
}

/** The mode dispatcher — `mode` alone decides, never the array contents. */
function DisarmOutcome({ result }: { result: DisarmResult }) {
  if (result.mode === 'raw') return <DisarmItemized result={result} />
  if (result.mode === 'guardrail-resume') {
    return (
      <>
        <div className="dz-row">
          <span className="dz-verb">resumed</span> the sweep for trip #{result.trip_id}{' '}
          — the same client_order_ids the tripping process derives, so the venue
          collapses the race instead of stacking a second live stop.
        </div>
        {/* The sweep event's OWN recorded summary, verbatim: the same text the
            Auditor and the guardrails history show, so the three cannot disagree.
            The arrays are empty on this path and are NOT rendered. */}
        <div className="dz-row">{result.detail}</div>
        <div className="dz-row">
          <span className="dz-verb">
            <HelpTerm term="sweep (guardrail)">sweep</HelpTerm>
          </span>{' '}
          {result.sweep_state ?? 'unknown'}
        </div>
        {/* The one array this mode DOES fill (routers/safety.py `_sweep_record`):
            a position with no stop anywhere is an alarm, not a count. */}
        <UnprotectedAlarm unprotected={result.unprotected} />
        <ResumeKey resumeKey={result.resume_key} />
      </>
    )
  }
  return (
    <>
      <div className="dz-row">
        <span className="dz-verb">would resume</span> the sweep for trip #
        {result.trip_id}
        {result.sweep_state !== null && ` (sweep ${result.sweep_state})`} rather than
        running a fresh one
      </div>
      <DisarmItemized result={result} />
      <ResumeKey resumeKey={result.resume_key} />
    </>
  )
}

export function DisarmControl({ gate }: { gate: Gate | null }) {
  const [phase, setPhase] = useState<DisarmPhase>({ kind: 'idle' })
  // Orphans stale preview responses: bumped on every hold-start, abort, and
  // fire, so a slow dry-run landing after the flow moved on can never
  // resurrect a dead preview panel.
  const seqRef = useRef(0)
  // The in-flight preview request, cancelled outright on abort — press-release
  // spam must not pile dry-run venue reads up server-side. (Correctness never
  // rests on this: the seq counter already orphans late responses.)
  const previewCtrlRef = useRef<AbortController | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const dismissRef = useRef<HTMLButtonElement>(null)

  const brokerConfigured = gate !== null && gate.broker_configured
  // A dismissible panel (result / blocked / failed) LOCKS the button: the
  // unprotected alarm is persistent-until-dismissed, and a fresh hold must not
  // silently replace it. One rule, no lost alarms.
  const panelUp =
    phase.kind === 'done' || phase.kind === 'blocked' || phase.kind === 'failed'
  // Alarm panels: unprotected positions, or a partial disarm. These demand an
  // explicit focused/pointered dismissal (no Escape) — deliberate friction.
  // MODE-BLIND on purpose: `unprotected` is on every member of the wire union
  // and every mode fills it faithfully, so the loud/no-Escape posture is decided
  // by the VENUE STATE, never by which code path reported it.
  const alarmUp =
    (phase.kind === 'done' && phase.result.unprotected.length > 0) ||
    (phase.kind === 'failed' && phase.partial)
  const disabled = !brokerConfigured || phase.kind === 'firing' || panelUp

  const title = !brokerConfigured
    ? gate === null
      ? 'gate status unavailable — DISARM stays disabled'
      : 'no broker configured'
    : panelUp
      ? 'dismiss the DISARM result first'
      : 'hold 900 ms to DISARM — cancels entry-side, keeps bracket stops'

  // True between "the user dismissed" and "focus handed back to the button".
  const focusBackRef = useRef(false)

  const dismiss = () => {
    focusBackRef.current = true
    setPhase({ kind: 'idle' })
  }

  // Keyboard flow across the fire: completing a hold disables the button, so
  // without this the focus falls to <body> at the exact moment a keyboard
  // user needs to read the result — land it on the panel's dismiss control.
  // On dismissal, hand focus back to the DISARM button — from THIS effect,
  // post-commit, because focusing before React re-enables the button is a
  // silent no-op (a timeout races the commit; an effect never does).
  useEffect(() => {
    if (panelUp) {
      dismissRef.current?.focus()
    } else if (focusBackRef.current) {
      focusBackRef.current = false
      btnRef.current?.focus()
    }
  }, [panelUp, phase.kind])

  // Escape dismisses the NON-alarm panels only (blocked, calm dry-run
  // failure, done-with-nothing-unprotected); alarm panels stay click/Enter-
  // only — see alarmUp above.
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
    postDisarm(true, ctrl.signal).then(
      (r) => {
        if (seqRef.current !== seq) return
        setPhase((p) => (p.kind === 'preview' ? { kind: 'preview', result: r } : p))
      },
      (err: unknown) => {
        // An aborted request's rejection always finds a stale seq (onAbort
        // bumps BEFORE it aborts) — only genuine failures land here.
        if (seqRef.current !== seq) return
        setPhase(disarmFailure(err, false)) // vetoes the hold via `vetoed` below
      },
    )
  }

  const onAbort = () => {
    seqRef.current++ // orphan the in-flight preview FIRST (see above)
    previewCtrlRef.current?.abort()
    previewCtrlRef.current = null
    // Only a live preview resets to idle — an abort caused by a veto must keep
    // the blocked/failed panel that vetoed it.
    setPhase((p) => (p.kind === 'preview' ? { kind: 'idle' } : p))
  }

  const onFire = () => {
    // HoldToConfirm calls this only when armed — i.e. the preview LANDED.
    seqRef.current++
    previewCtrlRef.current = null
    setPhase({ kind: 'firing' })
    // While in flight the button is disabled (no client-side second fire; the
    // server's single-flight lock is the backstop, not the plan).
    postDisarm(false).then(
      (r) => setPhase({ kind: 'done', result: r }),
      (err: unknown) => setPhase(disarmFailure(err, true)),
    )
    // No manual refetch here: a successful real run bumps the server-side
    // action nonce, the SSE change event wakes every poller (safety screen,
    // gate, positions) — scope decision 12's designed path.
  }

  const label = (
    <span className="dz-btn-label">
      <b>DISARM</b>
      <small>cancels entry-side, keeps bracket stops</small>
    </span>
  )

  return (
    <span className="dz-wrap">
      <HoldToConfirm
        holdMs={900}
        className="disarm"
        title={title}
        disabled={disabled}
        armed={phase.kind === 'preview' && phase.result !== null}
        vetoed={phase.kind === 'blocked' || phase.kind === 'failed'}
        buttonRef={btnRef}
        label={phase.kind === 'firing' ? <span className="dz-btn-label"><b>FIRING…</b></span> : label}
        waitingLabel={
          <span className="dz-btn-label">
            <b>DISARM</b>
            <small>waiting on the dry-run preview…</small>
          </span>
        }
        onHoldStart={onHoldStart}
        onFire={onFire}
        onAbort={onAbort}
      />

      {phase.kind === 'preview' && (
        <div className="dz-pop" role="status">
          <div className="dz-head">DRY-RUN PREVIEW</div>
          {phase.result === null ? (
            <div className="dz-row dz-dim">querying the venue…</div>
          ) : (
            <DisarmOutcome result={phase.result} />
          )}
          <div className="dz-foot">
            keep holding to fire the real DISARM · release or Esc aborts
          </div>
        </div>
      )}

      {phase.kind === 'firing' && (
        <div className="dz-pop" role="status">
          <div className="dz-head">DISARM — FIRING</div>
          <div className="dz-row dz-dim">real run in flight at the venue…</div>
        </div>
      )}

      {phase.kind === 'done' && (
        <div className="dz-pop" role="status">
          <div className="dz-head">
            DISARM COMPLETE
            {phase.result.unprotected.length > 0 && (
              <span className="dz-head-alarm"> — UNPROTECTED POSITIONS</span>
            )}
          </div>
          <DisarmOutcome result={phase.result} />
          <button type="button" className="dz-dismiss" ref={dismissRef} onClick={dismiss}>
            dismiss
          </button>
        </div>
      )}

      {phase.kind === 'blocked' && (
        <div className="dz-pop" role="status">
          <div className="dz-head">DISARM BLOCKED</div>
          <div className="dz-row">{phase.detail}</div>
          <div className="dz-foot">
            the venue was not touched by this request
            {phase.detail.includes('in flight') &&
              ' — another disarm holds the single-flight lock (this window or another); try again when it finishes'}
          </div>
          <button type="button" className="dz-dismiss" ref={dismissRef} onClick={dismiss}>
            dismiss
          </button>
        </div>
      )}

      {phase.kind === 'failed' && (
        <div className="dz-pop" role={phase.partial ? 'alert' : 'status'}>
          <div className="dz-head">
            DISARM FAILED
            {phase.partial && <span className="dz-head-alarm"> — OUTCOME UNCERTAIN</span>}
          </div>
          <div className="dz-row">{phase.detail}</div>
          {phase.partial ? (
            // No role here: the panel itself is the alert on this path — a
            // nested role="alert" would double-announce.
            <div className="dz-alarm">
              {phase.detail.startsWith(D503_SWEEP)
                ? // The trip's own sweep, resumed and still not finished. It is
                  // RETRYING, not dead: every cycle re-runs it and the brake stays
                  // tripped — but the venue may still hold working entry orders.
                  'The trip’s sweep did NOT complete — it stays RETRYING (every ' +
                  'cycle re-runs it, and holding DISARM again resumes the same ' +
                  'sweep). Resting entry orders may still be working: verify at the ' +
                  'broker before trusting the book.'
                : 'The venue may be PARTIALLY disarmed — orders cancelled before the ' +
                  'failure stay cancelled. Verify on the Execution Safety screen (7) ' +
                  'and at the broker before trusting any resting order.'}
            </div>
          ) : (
            <div className="dz-foot">the dry run touched nothing — the venue is unchanged</div>
          )}
          <button type="button" className="dz-dismiss" ref={dismissRef} onClick={dismiss}>
            dismiss
          </button>
        </div>
      )}
    </span>
  )
}
