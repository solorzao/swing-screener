import type { ReactNode } from 'react'
import {
  POLL_MS,
  getCohorts,
  getFunnel,
  getPerformance,
  getPicks,
  getPositions,
  usePolling,
} from '../lib/api'
import { HelpTerm } from '../components/HelpTerm'
import type {
  CohortRow,
  Cohorts,
  Facet,
  ForwardBooks,
  Heartbeat,
  PlayType,
  Polled,
  TickerFeed,
  TickerSource,
  Window,
} from '../lib/api'
import { fmtClock } from '../lib/fmt'
import { resolveCohortStat } from '../lib/picks'
import { FacetCaption } from '../components/FacetToggle'
import { ForwardBooksPanel } from '../components/ForwardBooksPanel'
import { FunnelBar } from '../components/FunnelBar'
import { HeartbeatRail } from '../components/HeartbeatRail'
import { PanelBody } from '../components/PanelBody'
import { PerformancePanel } from '../components/PerformancePanel'
import type { BreakdownTab } from '../components/PerformancePanel'
import { PickCard } from '../components/PickCard'
import { RiskStrip } from '../components/RiskStrip'
import { Segmented } from '../components/Segmented'
import { StatChip } from '../components/StatChip'

const WINDOWS: Window[] = ['all', '90', '180', '365']
const PLAY_TYPES: PlayType[] = ['all', 'continuation', 'reversal']

/** Plural-friendly labels for the recap's per-source counts. */
const SOURCE_LABEL: Record<TickerSource, string> = {
  exit: 'exits',
  execution: 'executions',
  email: 'emails',
  analyst: 'analyst calls',
  analysis: 'analyses',
}

/* SINCE YOU LAST LOOKED — the morning answer to "what happened while I was
   away", built from the ALREADY-fetched Zone E feed (no new endpoint) plus the
   last-visit watermark App reads-and-restamps once per open. The ticker strip
   animates events away; this zone holds the new-since-last-visit ones still:
   per-source counts, then the newest few verbatim. First run (no stamp) says
   so honestly instead of fabricating "nothing new". */
function RecapZone({
  ticker,
  prevVisit,
}: {
  ticker: Polled<TickerFeed>
  prevVisit: Date | null
}) {
  const events = ticker.error === null ? (ticker.data?.events ?? null) : null
  let body: ReactNode
  if (prevVisit === null) {
    body = (
      <span className="recap-empty">
        first open on this browser — the recap accrues from your next visit
      </span>
    )
  } else if (events === null) {
    body = <span className="recap-empty">…</span>
  } else {
    const cutoff = prevVisit.getTime()
    const fresh = events.filter((e) => new Date(e.ts).getTime() > cutoff)
    if (fresh.length === 0) {
      body = (
        <span className="recap-empty">
          nothing new since your last look ({prevVisit.toLocaleString()})
        </span>
      )
    } else {
      const counts = new Map<TickerSource, number>()
      for (const e of fresh) counts.set(e.source, (counts.get(e.source) ?? 0) + 1)
      body = (
        <>
          <span className="recap-counts mono">
            {[...counts.entries()]
              .map(([s, n]) => `${n} ${SOURCE_LABEL[s]}`)
              .join(' · ')}
          </span>
          {fresh.slice(0, 4).map((e) => (
            <span
              key={`${e.source}|${e.ts}|${e.headline}`}
              className="recap-item"
              title={e.detail}
            >
              {fmtClock(e.ts)} {e.ticker !== null && `${e.ticker} `}
              {e.headline}
            </span>
          ))}
          {fresh.length > 4 && (
            <span className="recap-more">+{fresh.length - 4} more in the feed below</span>
          )}
        </>
      )
    }
  }
  return (
    <section className="panel recap">
      <div className="panel-head">
        SINCE YOU LAST LOOKED
        {prevVisit !== null && (
          <span className="panel-caption">last visit {prevVisit.toLocaleString()}</span>
        )}
      </div>
      <div className="recap-body">{body}</div>
    </section>
  )
}

function CohortsSection({ facet, cohorts }: { facet: Facet; cohorts: Polled<Cohorts> }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <HelpTerm term="Cohort">COHORTS</HelpTerm> <FacetCaption facet={facet} />
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

/* Zone D — TODAY (the design's right-column pick surface, plan Task 17). The
   digest's surfaced picks in digest order as compact PickCards, then the
   liveness-dropped extras flagged beneath — never counted in the surfaced three.
   Its own picks poll (per-screen; dies with Mission Control); the facet-scoped
   cohorts poll is HOISTED to the screen and shared with CohortsSection (one
   /api/cohorts per interval, two consumers). The LOG action hands the pick's
   signal id up to App, which navigates to the Candidates screen and prefills
   the form there (Mission Control has no room to host it). */
function TodayZone({
  wake,
  cohortRows,
  onLogPick,
}: {
  wake: number
  cohortRows: CohortRow[]
  onLogPick: (signalId: number) => void
}) {
  const picks = usePolling(getPicks, POLL_MS, wake)
  return (
    <section className="panel">
      <div className="panel-head">
        TODAY
        <span className="panel-caption">
          the digest’s surfaced picks · prices as of last close
          {picks.data !== null && ` · quotes ${fmtClock(picks.data.quotes_as_of)}`}
        </span>
      </div>
      <PanelBody polled={picks} noun="picks">
        {(data) => {
          const surfaced = [...data.daily, ...data.reversal]
          if (surfaced.length === 0 && data.extras.length === 0) {
            return (
              <div className="panel-wait">
                no picks today — next evening screen ~18:05 ET
              </div>
            )
          }
          return (
            <div className="pk-list">
              {surfaced.map((p) => (
                <PickCard
                  key={p.signal_id}
                  pick={p}
                  cohortStat={resolveCohortStat(cohortRows, p.cohort)}
                  onLog={onLogPick}
                />
              ))}
              {data.extras.length > 0 && (
                <>
                  <div className="pk-extra-head">
                    <HelpTerm term="flagged">flagged extras</HelpTerm> · dropped for liveness, never in the three
                  </div>
                  {data.extras.map((p) => (
                    <PickCard
                      key={p.signal_id}
                      pick={p}
                      isExtra
                      cohortStat={resolveCohortStat(cohortRows, p.cohort)}
                      onLog={onLogPick}
                    />
                  ))}
                </>
              )}
            </div>
          )
        }}
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
  const perf = usePolling(
    () => getPerformance(playType, win, facet),
    POLL_MS,
    wake,
    `${facet}|${win}|${playType}`,
  )
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

/** Mission Control — the Phase-2 grid plus Zone B (Task 16). The funnel and
 * positions polls live HERE (not in App): per-screen polls that die with
 * their screen; only the plan's permanent roster (health, heartbeats, gate,
 * forward-books, attention) outlives a screen switch. Cohorts/performance
 * refetch on a param flip via usePolling's paramsKey (the honest drop), never
 * by remounting. Zone B is the design's RISK top band — open-position strips
 * + compact caps off the same /api/positions read the Positions screen uses.
 * Zones D/E slot in with Tasks 17/20. */
export function MissionControlScreen({
  beats,
  fb,
  facet,
  wake,
  win,
  playType,
  tab,
  onWin,
  onPlayType,
  onTab,
  onLogPick,
  ticker,
  prevVisit,
}: {
  beats: Polled<Heartbeat[]>
  fb: Polled<ForwardBooks>
  facet: Facet
  wake: number
  win: Window
  playType: PlayType
  tab: BreakdownTab
  onWin: (win: Window) => void
  onPlayType: (playType: PlayType) => void
  onTab: (tab: BreakdownTab) => void
  /** Zone D LOG hand-off: navigate to Candidates and prefill there. */
  onLogPick: (signalId: number) => void
  /** The permanent Zone E feed (App's poll) — the recap zone reads it. */
  ticker: Polled<TickerFeed>
  /** The previous visit's stamp (null on a first run) — App owns the read/
   * restamp so a mid-session re-render can't move the goalpost. */
  prevVisit: Date | null
}) {
  const funnel = usePolling(getFunnel, POLL_MS, wake)
  const positions = usePolling(getPositions, POLL_MS, wake)
  // One facet-scoped cohorts poll for the whole screen — Zone D's per-card
  // StatChip join and the COHORTS panel both read it (no double /api/cohorts).
  const cohorts = usePolling(() => getCohorts(facet), POLL_MS, wake, facet)
  return (
    <>
      <div className="zone-b">
        <RiskStrip polled={positions} />
      </div>
      <RecapZone ticker={ticker} prevVisit={prevVisit} />
      <main className="grid">
        <section className="panel">
          <div className="panel-head"><HelpTerm term="SYSTEMS rail">SYSTEMS</HelpTerm></div>
          <PanelBody polled={beats} noun="heartbeats">
            {(data) => <HeartbeatRail beats={data} />}
          </PanelBody>
        </section>

        <div className="col-stack">
          <ForwardBooksPanel fb={fb} facet={facet} />

          <section className="panel">
            <div className="panel-head">
              <HelpTerm term="reversal funnel">REVERSAL FUNNEL</HelpTerm>
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
          <TodayZone
            wake={wake}
            cohortRows={cohorts.data?.cohorts ?? []}
            onLogPick={onLogPick}
          />
          <CohortsSection facet={facet} cohorts={cohorts} />
          <PerformanceSection
            facet={facet}
            win={win}
            playType={playType}
            tab={tab}
            wake={wake}
            onWin={onWin}
            onPlayType={onPlayType}
            onTab={onTab}
          />
        </div>
      </main>
    </>
  )
}
