import type { Health, Heartbeat } from '../lib/api'

/* The 44px bar on every screen. Dark-cockpit doctrine: the MASTER CAUTION lamp is
   dark when everything is clear; light means attention required. Pass `beats: null`
   when heartbeat data is unavailable — UNKNOWN must never read as healthy. */

function fmtClock(d: Date | null): string {
  if (d === null) return '—'
  return d.toLocaleTimeString('en-US', { hour12: false })
}

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
