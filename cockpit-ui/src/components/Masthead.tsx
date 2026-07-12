import { useEffect, useState } from 'react'
import { postAzureLogin } from '../lib/api'
import type { Facet, Gate, Health, Heartbeat } from '../lib/api'
import { FacetToggle } from './FacetToggle'
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

export function Masthead({
  health,
  beats,
  gate,
  asOf,
  facet,
  onFacet,
  cost,
  onCost,
  onReference,
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
  /** Screen 10 (Reference) has no digit key — this masthead link is its one door. */
  onReference: () => void
}) {
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
        onClick={onReference}
      >
        REFERENCE
      </button>

      <button type="button" className="disarm" disabled title="Phase 3">
        DISARM
      </button>
    </header>
  )
}
