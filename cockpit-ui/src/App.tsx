import { useState } from 'react'
import type { ReactNode } from 'react'
import {
  getCohorts,
  getForwardBooks,
  getFunnel,
  getGate,
  getHealth,
  getHeartbeats,
  getPerformance,
  useEventWake,
  usePolling,
} from './lib/api'
import type { Facet, ForwardBooks, PlayType, Polled, Window } from './lib/api'
import { FacetCaption } from './components/FacetToggle'
import { FunnelBar } from './components/FunnelBar'
import { HeartbeatRail } from './components/HeartbeatRail'
import { Masthead } from './components/Masthead'
import type { CostLevel } from './components/Masthead'
import { NeedsHandStrip } from './components/NeedsHandStrip'
import { PerformancePanel } from './components/PerformancePanel'
import type { BreakdownTab } from './components/PerformancePanel'
import { Segmented } from './components/Segmented'
import { SettlementCard } from './components/SettlementCard'
import { StatChip } from './components/StatChip'

/* Poll budget: forward-books + performance each cost ~1s server-side, so 60s is
   the floor for EVERY poll; the SSE wake (useEventWake) makes changes feel
   instant without touching that budget. */
const POLL_MS = 60_000

const WINDOWS: Window[] = ['all', '90', '180', '365']
const PLAY_TYPES: PlayType[] = ['all', 'continuation', 'reversal']

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

/* usePolling's contract: a changed fetcher does NOT refetch — new params would
   show the old params' data under the new label for up to POLL_MS. The sanctioned
   idiom is key-remounting the polled subtree (App keys these sections by their
   params), which forces an immediate fetch and honestly drops the wrong-params
   data while it is in flight. */

/** Owns the forward-books poll; render-prop so the payload feeds BOTH the
 * Needs-Your-Hand strip (above the grid) and the Forward Books panel (inside it)
 * from one fetch — the endpoint costs ~1s server-side, polling it twice would
 * double that. */
function WithForwardBooks({
  facet,
  wake,
  children,
}: {
  facet: Facet
  wake: number
  children: (fb: Polled<ForwardBooks>) => ReactNode
}) {
  const fb = usePolling(() => getForwardBooks(facet), POLL_MS, wake)
  return <>{children(fb)}</>
}

function CohortsSection({ facet, wake }: { facet: Facet; wake: number }) {
  const cohorts = usePolling(() => getCohorts(facet), POLL_MS, wake)
  return (
    <section className="panel">
      <div className="panel-head">
        COHORTS <FacetCaption facet={facet} />
      </div>
      <PanelBody polled={cohorts} noun="cohorts">
        {(data) =>
          data.cohorts.length === 0 ? (
            <div className="panel-wait">no closed trades on this facet yet</div>
          ) : (
            data.cohorts.map((row) => (
              <div key={`${row.key}|${row.strength ?? ''}`} className="cohort-row">
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
  )
}

function PerformanceSection({
  facet,
  win,
  playType,
  tab,
  wake,
  onWin,
  onPlayType,
  onTab,
}: {
  facet: Facet
  win: Window
  playType: PlayType
  tab: BreakdownTab
  wake: number
  onWin: (win: Window) => void
  onPlayType: (playType: PlayType) => void
  onTab: (tab: BreakdownTab) => void
}) {
  const perf = usePolling(() => getPerformance(playType, win, facet), POLL_MS, wake)
  return (
    <section className="panel">
      <div className="panel-head perf-head">
        PERFORMANCE <FacetCaption facet={facet} />
        <span className="spacer" />
        <Segmented
          title="play type"
          options={PLAY_TYPES.map((p) => ({
            value: p,
            label: p === 'continuation' ? 'cont' : p === 'reversal' ? 'rev' : 'all',
          }))}
          value={playType}
          onChange={onPlayType}
        />
        <Segmented
          title="leaderboard window (days)"
          options={WINDOWS.map((w) => ({
            value: w,
            label: w === 'all' ? 'all' : `${w}d`,
          }))}
          value={win}
          onChange={onWin}
        />
      </div>
      <PanelBody polled={perf} noun="performance">
        {(data) => <PerformancePanel data={data} tab={tab} onTab={onTab} />}
      </PanelBody>
    </section>
  )
}

export default function App() {
  const wake = useEventWake() // called ONCE; every poll rides the same counter

  const [facet, setFacet] = useState<Facet>('research')
  const [cost, setCost] = useState<CostLevel>('0.05')
  const [win, setWin] = useState<Window>('all')
  const [playType, setPlayType] = useState<PlayType>('all')
  // Lives here, not in PerformancePanel: local state would be reset by the
  // key-remount blast every time facet/window/play-type flips.
  const [tab, setTab] = useState<BreakdownTab>('timeframe')

  const health = usePolling(getHealth, POLL_MS, wake)
  const beats = usePolling(getHeartbeats, POLL_MS, wake)
  const funnel = usePolling(getFunnel, POLL_MS, wake)
  const gate = usePolling(getGate, POLL_MS, wake)

  const asOf = latest(
    health.lastFetched,
    beats.lastFetched,
    funnel.lastFetched,
    gate.lastFetched,
  )
  // Stale heartbeats must not feed the caution lamp: on any fetch error the
  // masthead sees null and shows UNKNOWN instead of yesterday's green. The gate
  // chip takes the same hard line — stale READY is worse than "…".
  const mastheadBeats = beats.error === null ? beats.data : null
  const mastheadGate = gate.error === null ? gate.data : null

  const dbDown = health.data !== null && !health.data.connected

  return (
    <div className="app">
      <Masthead
        health={health.data}
        beats={mastheadBeats}
        gate={mastheadGate}
        asOf={asOf}
        facet={facet}
        onFacet={setFacet}
        cost={cost}
        onCost={setCost}
      />

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
        <WithForwardBooks key={`fb|${facet}`} facet={facet} wake={wake}>
          {(fb) => (
            <>
              {/* The masthead's hard line, not PanelBody's stale-dim: on a fetch
                  error the strip sees null and shows "…" — last-good content at
                  full brightness could be a stale EMPTY state reading as a fresh
                  "nothing needs your hand" while a book settled during the outage. */}
              <NeedsHandStrip
                cards={fb.error === null ? (fb.data?.cards ?? null) : null}
              />

              <main className="grid">
                <section className="panel">
                  <div className="panel-head">SYSTEMS</div>
                  <PanelBody polled={beats} noun="heartbeats">
                    {(data) => <HeartbeatRail beats={data} />}
                  </PanelBody>
                </section>

                <div className="col-stack">
                  <section className="panel">
                    <div className="panel-head">
                      FORWARD BOOKS <FacetCaption facet={facet} />
                    </div>
                    <PanelBody polled={fb} noun="forward books">
                      {(data) =>
                        data.cards.length === 0 ? (
                          <div className="panel-wait">no experiments registered</div>
                        ) : (
                          <div className="scard-wall">
                            {data.cards.map((c) => (
                              <SettlementCard key={c.name} card={c} />
                            ))}
                          </div>
                        )
                      }
                    </PanelBody>
                  </section>

                  <section className="panel">
                    <div className="panel-head">
                      REVERSAL FUNNEL
                      <span className="panel-caption">latest daily digest</span>
                    </div>
                    <PanelBody polled={funnel} noun="funnel">
                      {(data) =>
                        data.funnel === null ? (
                          <div className="panel-wait">
                            no funnel recorded yet — accrues from the next daily digest
                          </div>
                        ) : (
                          <FunnelBar funnel={data.funnel} />
                        )
                      }
                    </PanelBody>
                  </section>
                </div>

                <div className="col-stack">
                  <CohortsSection key={`cohorts|${facet}`} facet={facet} wake={wake} />
                  <PerformanceSection
                    key={`perf|${facet}|${win}|${playType}`}
                    facet={facet}
                    win={win}
                    playType={playType}
                    tab={tab}
                    wake={wake}
                    onWin={setWin}
                    onPlayType={setPlayType}
                    onTab={setTab}
                  />
                </div>
              </main>
            </>
          )}
        </WithForwardBooks>
      )}
    </div>
  )
}
