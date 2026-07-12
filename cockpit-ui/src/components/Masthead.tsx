import { useEffect, useRef, useState } from 'react'
import { ApiError, postAzureLogin, postDisarm } from '../lib/api'
import type { DisarmResult, Facet, Gate, Health, Heartbeat } from '../lib/api'
import { screenDef, screenNumber } from '../lib/screens'
import type { ScreenId } from '../lib/screens'
import { FacetToggle } from './FacetToggle'
import { HoldToConfirm } from './HoldToConfirm'
import { Segmented } from './Segmented'

/** The one selectable cost level. `@0.10` is disabled-honest: replay-only, no
 * re-priced book exists (plan scope decision 4). */
export type CostLevel = '0.05' | '0.10'

/* The 44px bar on every screen. Dark-cockpit doctrine: the MASTER CAUTION lamp is
   dark when everything is clear; light means attention required. Pass `beats: null`
   when heartbeat data is unavailable — UNKNOWN must never read as healthy. */

function fmtClock(d: Date | null): string {
  if (d === null) return '—'
  return d.toLocaleTimeString('en-US', { hour12: false })
}

/* Sign-in phases (design doc 2026-07-06): the button renders ONLY when health says
   azure && !connected. 'waiting' is exited by health flipping connected (button
   unrenders) or by the deadline (re-arm as retry). No login-succeeded signal
   exists anywhere — health is the one recovery oracle. */
type LoginPhase = 'idle' | 'waiting' | 'retry' | 'no-cli'
const LOGIN_DEADLINE_MS = 120_000

/* ---------- DISARM (the sixth action — plan Task 15 / scope decision 1) ---------- */

/* DISARM is the venue sweep, and ONLY that: cancels entry-side orders, keeps
   (and restores) bracket stops, never closes positions, never computes a stop
   level. It does NOT flip SWING_EXECUTION_MODE — env is per-process. The flow:
   hold-start fires the dry_run=1 preview; the real POST arms only once the
   preview has LANDED (HoldToConfirm's `armed` gate — a completed hold waits on
   the response, it never fires blind).

   The 409s are STATES, not crashes: "no broker configured" and "disarm already
   in flight" render as distinct dismissible panels. A REAL-run failure (503 or
   the network dying mid-POST) is rendered PARTIAL-loud: the router invalidates
   the snapshot and bumps the wake nonce even on the failure path, because a
   partial disarm has still moved venue state. */
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
    // real-run failure may have moved SOME venue state before raising.
    return { kind: 'failed', detail: err.message, partial: realRun }
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

/** What a run reported, rendered EXACTLY from the wire — verbs follow the
 * response's own dry_run flag, counts come from the arrays as returned, and a
 * non-empty `unprotected` is the alarm. No optimistic rendering, no fabricated
 * zeros: an empty list renders as the wire's honest "none". */
function DisarmOutcome({ result }: { result: DisarmResult }) {
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
          ? 'no stops (none were missing)'
          : `stops for ${result.stops_restored.join(', ')} — at the recorded level, copied never computed`}
      </div>
      {result.unprotected.length > 0 && (
        <div className="dz-alarm" role="alert">
          UNPROTECTED — no recorded stop level anywhere for{' '}
          {result.unprotected.join(', ')}. Left alone (never auto-closed) — this
          needs your hand at the broker.
        </div>
      )}
    </>
  )
}

function DisarmControl({ gate }: { gate: Gate | null }) {
  const [phase, setPhase] = useState<DisarmPhase>({ kind: 'idle' })
  // Orphans stale preview responses: bumped on every hold-start, abort, and
  // fire, so a slow dry-run landing after the flow moved on can never
  // resurrect a dead preview panel.
  const seqRef = useRef(0)

  const brokerConfigured = gate !== null && gate.broker_configured
  // A dismissible panel (result / blocked / failed) LOCKS the button: the
  // unprotected alarm is persistent-until-dismissed, and a fresh hold must not
  // silently replace it. One rule, no lost alarms.
  const panelUp =
    phase.kind === 'done' || phase.kind === 'blocked' || phase.kind === 'failed'
  const disabled = !brokerConfigured || phase.kind === 'firing' || panelUp

  const title = !brokerConfigured
    ? gate === null
      ? 'gate status unavailable — DISARM stays disabled'
      : 'no broker configured'
    : panelUp
      ? 'dismiss the DISARM result first'
      : 'hold 900 ms to DISARM — cancels entry-side, keeps bracket stops'

  const onHoldStart = () => {
    const seq = ++seqRef.current
    setPhase({ kind: 'preview', result: null })
    postDisarm(true).then(
      (r) => {
        if (seqRef.current !== seq) return
        setPhase((p) => (p.kind === 'preview' ? { kind: 'preview', result: r } : p))
      },
      (err: unknown) => {
        if (seqRef.current !== seq) return
        setPhase(disarmFailure(err, false)) // vetoes the hold via `vetoed` below
      },
    )
  }

  const onAbort = () => {
    seqRef.current++
    // Only a live preview resets to idle — an abort caused by a veto must keep
    // the blocked/failed panel that vetoed it.
    setPhase((p) => (p.kind === 'preview' ? { kind: 'idle' } : p))
  }

  const onFire = () => {
    // HoldToConfirm calls this only when armed — i.e. the preview LANDED.
    seqRef.current++
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
          <button
            type="button"
            className="dz-dismiss"
            onClick={() => setPhase({ kind: 'idle' })}
          >
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
          <button
            type="button"
            className="dz-dismiss"
            onClick={() => setPhase({ kind: 'idle' })}
          >
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
            <div className="dz-alarm" role="alert">
              The venue may be PARTIALLY disarmed — orders cancelled before the
              failure stay cancelled. Verify on the Execution Safety screen (7)
              and at the broker before trusting any resting order.
            </div>
          ) : (
            <div className="dz-foot">the dry run touched nothing — the venue is unchanged</div>
          )}
          <button
            type="button"
            className="dz-dismiss"
            onClick={() => setPhase({ kind: 'idle' })}
          >
            dismiss
          </button>
        </div>
      )}
    </span>
  )
}

export function Masthead({
  health,
  beats,
  gate,
  asOf,
  facet,
  onFacet,
  cost,
  onCost,
  screen,
  onNavigate,
}: {
  health: Health | null
  beats: Heartbeat[] | null
  /** Pass null while gate data is unavailable — the chip shows "…", never a guess. */
  gate: Gate | null
  asOf: Date | null
  facet: Facet
  onFacet: (facet: Facet) => void
  cost: CostLevel
  onCost: (cost: CostLevel) => void
  /** The active screen — the indicator renders it from the registry. */
  screen: ScreenId
  /** Screen navigation: the REFERENCE link (screen 10 has no digit key — this
   * is its one door) and the indicator's click-home both route here. */
  onNavigate: (id: ScreenId) => void
}) {
  const def = screenDef(screen)
  const anyLit =
    beats !== null && beats.some((b) => b.state === 'late' || b.state === 'down')
  // No data at all counts as unknown — a dead poller is never allowed to look green.
  const anyUnknown = beats === null || beats.some((b) => b.state === 'unknown')

  const [phase, setPhase] = useState<LoginPhase>('idle')
  const connected = health !== null && health.connected

  useEffect(() => {
    if (connected) setPhase('idle') // recovered: next outage starts fresh
  }, [connected])

  useEffect(() => {
    if (phase !== 'waiting') return
    const id = setTimeout(() => setPhase('retry'), LOGIN_DEADLINE_MS)
    return () => clearTimeout(id)
  }, [phase])

  const onSignIn = () => {
    setPhase('waiting')
    // Both callbacks transition only OUT OF 'waiting': a slow rejection landing
    // after recovery already reset the phase must not resurrect a stale
    // 'retry'/'no-cli' for the next outage's first render.
    postAzureLogin().then(
      (r) => {
        if (r.started || r.already_running) return // health decides from here
        setPhase((p) =>
          p === 'waiting' ? (r.error === 'az-not-found' ? 'no-cli' : 'retry') : p,
        )
      },
      // 403/409/network: chip already shows the down line
      () => setPhase((p) => (p === 'waiting' ? 'retry' : p)),
    )
  }

  return (
    <header className="masthead">
      <span className="mh-wordmark">
        <b>SWING SCREENER</b> · COCKPIT
      </span>

      {/* Where you are, registry-rendered ("3 · POSITIONS & LEDGER") — and the
          mouse road home from digitless screens: clicking returns to Mission
          Control, same as pressing 1. */}
      <button
        type="button"
        className="mh-screen"
        title={`current screen ${screenNumber(def)} · ${def.title} — click (or press 1) for Mission Control`}
        onClick={() => onNavigate('mission')}
      >
        {screenNumber(def)} · {def.title}
      </button>

      <span className={anyLit ? 'caution lit' : 'caution'}>MASTER CAUTION</span>
      {!anyLit && anyUnknown && <span className="unknown-tag">UNKNOWN</span>}

      <span className="db-chip">
        <span
          className={`db-dot ${health === null ? 'none' : health.connected ? 'ok' : 'bad'}`}
          aria-hidden="true"
        />
        {/* Label text only — the backend guarantees it never contains the URL. */}
        {health === null ? '…' : health.label}
        {health !== null && health.azure && !health.connected &&
          (phase === 'no-cli' ? (
            <span className="db-login-note">Azure CLI not installed</span>
          ) : (
            <button
              type="button"
              className="db-login"
              disabled={phase === 'waiting'}
              onClick={onSignIn}
            >
              {phase === 'waiting'
                ? 'waiting for browser sign-in…'
                : phase === 'retry'
                  ? 'Sign in to Azure — try again'
                  : 'Sign in to Azure'}
            </button>
          ))}
      </span>

      <span className="spacer" />

      <span className="mh-clock">data as of {fmtClock(asOf)}</span>

      {/* Autonomy gate: advisory, and NEVER green — operational readiness must not
          read as edge-exists (the tier chip owns the green claim, Phase 3). */}
      <span
        className="gate-chip"
        title={gate !== null ? gate.countdown : 'gate status unavailable'}
      >
        {gate === null
          ? '…'
          : gate.ready
            ? 'READY'
            : `NOT READY · ${gate.countdown.split('\n')[0] ?? ''}`}
      </span>
      {gate !== null && (
        <span className="mode-chip" title="execution mode">
          {gate.execution_mode}
        </span>
      )}

      <Segmented
        options={[
          {
            value: '0.05',
            label: 'net @0.05',
            title:
              'every level exit haircut 0.05 ATR at exit; flip/time-stop exits are never haircut',
          },
          {
            value: '0.10',
            label: '@0.10',
            disabled: true,
            title: 'not measured — replay-only level, no re-priced book exists',
          },
        ]}
        value={cost}
        onChange={onCost}
      />

      <FacetToggle facet={facet} onFacet={onFacet} />

      {/* Screen 10 — the one screen without a digit key (design numbering
          stops at 9); this link is its only entrance. */}
      <button
        type="button"
        className="mh-ref"
        title="Reference — screen 10 (universe, digest log, exit log)"
        onClick={() => onNavigate('reference')}
      >
        REFERENCE
      </button>

      {/* DISARM enablement keys on the PERMANENT gate poll's broker_configured
          (settings truthiness, never connectivity); App force-nulls the gate on
          a fetch error, so a stale broker_configured can never arm the button. */}
      <DisarmControl gate={gate} />
    </header>
  )
}
