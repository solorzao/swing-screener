import type { ReactNode } from 'react'
import { getCohorts, getHealth, getHeartbeats, usePolling } from './lib/api'
import type { Polled } from './lib/api'
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

/* Shared body treatment — the template every future panel copies. While a fetch
   error is present but the last good data is still on screen, the body dims
   (.stale) under a "showing last good data" line: stale must LOOK different from
   fresh, not just carry a footnote. With no data at all, the plain
   unavailable/waiting lines stand alone. (The masthead takes the harder line and
   force-nulls stale heartbeats instead — see below.) */
function PanelBody<T>({
  polled,
  noun,
  children,
}: {
  polled: Polled<T>
  noun: string
  children: (data: T) => ReactNode
}) {
  const { data, error } = polled
  return (
    <>
      {error !== null && (
        <div className="panel-error">
          {data !== null
            ? `showing last good data · ${error}`
            : `${noun} unavailable — ${error}`}
        </div>
      )}
      {data !== null ? (
        <div className={error !== null ? 'stale' : undefined}>{children(data)}</div>
      ) : (
        error === null && <div className="panel-wait">waiting for first fetch…</div>
      )}
    </>
  )
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
            {health.data.azure
              ? 'The cockpit keeps retrying every 60 seconds — if your Azure sign-in expired, use “Sign in to Azure” in the masthead.'
              : 'The cockpit keeps retrying every 60 seconds; nothing below is lost.'}
          </div>
        </div>
      ) : (
        <main className="grid">
          <section className="panel">
            <div className="panel-head">SYSTEMS</div>
            <PanelBody polled={beats} noun="heartbeats">
              {(data) => <HeartbeatRail beats={data} />}
            </PanelBody>
          </section>

          <section className="panel">
            <div className="panel-head">
              COHORTS <span className="panel-caption">research book · replay-graded</span>
            </div>
            <PanelBody polled={cohorts} noun="cohorts">
              {(data) =>
                data.cohorts.length === 0 ? (
                  <div className="panel-wait">no closed research trades yet</div>
                ) : (
                  data.cohorts.map((row) => (
                    <div
                      key={`${row.key}|${row.strength ?? ''}`}
                      className="cohort-row"
                    >
                      <StatChip
                        stat={row.stat}
                        label={
                          row.strength === null ? row.key : `${row.key} · ${row.strength}`
                        }
                      />
                    </div>
                  ))
                )
              }
            </PanelBody>
          </section>
        </main>
      )}
    </div>
  )
}
