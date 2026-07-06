import { useEffect, useState } from 'react'
import { postAzureLogin } from '../lib/api'
import type { Health, Heartbeat } from '../lib/api'

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
  asOf,
}: {
  health: Health | null
  beats: Heartbeat[] | null
  asOf: Date | null
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
    postAzureLogin().then(
      (r) => {
        if (r.started || r.already_running) return // health decides from here
        setPhase(r.error === 'az-not-found' ? 'no-cli' : 'retry')
      },
      () => setPhase('retry'), // 403/409/network: chip already shows the down line
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

      <span className="mh-spacer" />

      <span className="mh-clock">data as of {fmtClock(asOf)}</span>

      {/* Phase 2 commitments, deliberately visible but dead. */}
      <span className="mh-seg">
        <button type="button" disabled title="Phase 2">
          net @0.05
        </button>
        <button type="button" disabled title="Phase 2">
          @0.10
        </button>
      </span>
      <button type="button" className="disarm" disabled title="Phase 2">
        DISARM
      </button>
    </header>
  )
}
