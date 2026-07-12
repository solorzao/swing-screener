import {
  POLL_MS,
  getCohorts,
  getFunnel,
  getPerformance,
  getPositions,
  usePolling,
} from '../lib/api'
import type {
  Facet,
  ForwardBooks,
  Heartbeat,
  PlayType,
  Polled,
  Window,
} from '../lib/api'
import { FacetCaption } from '../components/FacetToggle'
import { ForwardBooksPanel } from '../components/ForwardBooksPanel'
import { FunnelBar } from '../components/FunnelBar'
import { HeartbeatRail } from '../components/HeartbeatRail'
import { PanelBody } from '../components/PanelBody'
import { PerformancePanel } from '../components/PerformancePanel'
import type { BreakdownTab } from '../components/PerformancePanel'
import { RiskStrip } from '../components/RiskStrip'
import { Segmented } from '../components/Segmented'
import { StatChip } from '../components/StatChip'

const WINDOWS: Window[] = ['all', '90', '180', '365']
const PLAY_TYPES: PlayType[] = ['all', 'continuation', 'reversal']

function CohortsSection({ facet, wake }: { facet: Facet; wake: number }) {
  const cohorts = usePolling(() => getCohorts(facet), POLL_MS, wake, facet)
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
}) {
  const funnel = usePolling(getFunnel, POLL_MS, wake)
  const positions = usePolling(getPositions, POLL_MS, wake)
  return (
    <>
      <div className="zone-b">
        <RiskStrip polled={positions} />
      </div>
      <main className="grid">
        <section className="panel">
          <div className="panel-head">SYSTEMS</div>
          <PanelBody polled={beats} noun="heartbeats">
            {(data) => <HeartbeatRail beats={data} />}
          </PanelBody>
        </section>

        <div className="col-stack">
          <ForwardBooksPanel fb={fb} facet={facet} />

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
          <CohortsSection facet={facet} wake={wake} />
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
