import { getCohorts, getHealth, getHeartbeats, usePolling } from './lib/api'
import { HeartbeatRail } from './components/HeartbeatRail'
import { Masthead } from './components/Masthead'
import { StatChip } from './components/StatChip'

const POLL_MS = 60_000 // SSE is Phase 2; a 60s poll matches the data cadence

function latest(...dates: (Date | null)[]): Date | null {
  let best: Date | null = null
  for (const d of dates) {
    if (d !== null && (best === null || d.getTime() > best.getTime())) best = d
  }
  return best
}

export default function App() {
  const health = usePolling(getHealth, POLL_MS)
  const beats = usePolling(getHeartbeats, POLL_MS)
  const cohorts = usePolling(getCohorts, POLL_MS)

  const asOf = latest(health.lastFetched, beats.lastFetched, cohorts.lastFetched)
  // Stale heartbeats must not feed the caution lamp: on any fetch error the
  // masthead sees null and shows UNKNOWN instead of yesterday's green.
  const mastheadBeats = beats.error === null ? beats.data : null

  const dbDown = health.data !== null && !health.data.connected

  return (
    <div className="app">
      <Masthead health={health.data} beats={mastheadBeats} asOf={asOf} />

      {dbDown && health.data !== null ? (
        // One friendly card, matching the backend's never-a-traceback posture.
        <div className="db-down-card">
          <div className="db-down-title">{health.data.label} — not reachable</div>
          <div className="db-down-sub">{health.data.error ?? 'database unreachable'}</div>
          <div className="db-down-hint">
            The cockpit keeps retrying every 60 seconds; nothing below is lost.
          </div>
        </div>
      ) : (
        <main className="grid">
          <section className="panel">
            <div className="panel-head">SYSTEMS</div>
            {beats.error !== null && (
              <div className="panel-error">heartbeats unavailable — {beats.error}</div>
            )}
            {beats.data !== null ? (
              <HeartbeatRail beats={beats.data} />
            ) : (
              beats.error === null && <div className="panel-wait">waiting for first fetch…</div>
            )}
          </section>

          <section className="panel">
            <div className="panel-head">
              COHORTS <span className="panel-caption">research book · replay-graded</span>
            </div>
            {cohorts.error !== null && (
              <div className="panel-error">cohorts unavailable — {cohorts.error}</div>
            )}
            {cohorts.data !== null ? (
              cohorts.data.cohorts.length === 0 ? (
                <div className="panel-wait">no closed research trades yet</div>
              ) : (
                cohorts.data.cohorts.map((row) => (
                  <div
                    key={`${row.key}|${row.strength ?? ''}`}
                    className="cohort-row"
                  >
                    <StatChip
                      stat={row.stat}
                      label={row.strength === null ? row.key : `${row.key} · ${row.strength}`}
                    />
                  </div>
                ))
              )
            ) : (
              cohorts.error === null && (
                <div className="panel-wait">waiting for first fetch…</div>
              )
            )}
          </section>
        </main>
      )}
    </div>
  )
}
