import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import {
  getAttention,
  getCohorts,
  getForwardBooks,
  getFunnel,
  getGate,
  getHealth,
  getHeartbeats,
  getPerformance,
  getPlaybooks,
  useEventWake,
  usePolling,
} from './lib/api'
import type {
  Facet,
  ForwardBooks,
  Heartbeat,
  PlaybookBook,
  PlayType,
  Polled,
  Window,
} from './lib/api'
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

/* ---------- Screens (Phase 3, Task 14) ----------

   There is deliberately NO router: StaticFiles(html=True) is not an SPA
   fallback (a deep link would 404) and pywebview loads the root URL once —
   screens are App STATE, switched by the 1-9 keys per the design's fixed
   numbering. Screen 10 (Reference) has no digit; it rides the masthead link.
   Screens beyond this scaffold land with Tasks 15-20 and replace their
   placeholders here. */
export type ScreenId =
  | 'mission'
  | 'candidates'
  | 'positions'
  | 'forward'
  | 'playbooks'
  | 'analyst'
  | 'safety'
  | 'weather'
  | 'systems'
  | 'reference'

/** The screen registry: design numbering, digit-key mapping, display titles. */
const SCREENS: { id: ScreenId; digit: string | null; title: string }[] = [
  { id: 'mission', digit: '1', title: 'MISSION CONTROL' },
  { id: 'candidates', digit: '2', title: 'CANDIDATES' },
  { id: 'positions', digit: '3', title: 'POSITIONS & LEDGER' },
  { id: 'forward', digit: '4', title: 'FORWARD BOOKS' },
  { id: 'playbooks', digit: '5', title: 'PLAYBOOKS' },
  { id: 'analyst', digit: '6', title: 'ANALYST' },
  { id: 'safety', digit: '7', title: 'EXECUTION SAFETY' },
  { id: 'weather', digit: '8', title: 'MARKET WEATHER' },
  { id: 'systems', digit: '9', title: 'SYSTEMS' },
  { id: 'reference', digit: null, title: 'REFERENCE' },
]

/** True when a keydown happened while typing — digit keys must never steal a
 * value being entered into a form (INPUT/TEXTAREA/SELECT/contentEditable). */
function isTypingContext(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return (
    tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable
  )
}

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

/** Owns the forward-books poll; render-prop so the payload feeds the
 * Needs-Your-Hand strip (above the screen switch, on EVERY screen), the
 * Mission Control panel, and the full-width Forward Books screen from one
 * fetch — the endpoint costs ~1s server-side, polling it per consumer would
 * multiply that. This poll is PERMANENT (the strip needs it everywhere), which
 * is why it wraps the screen switch instead of living in a screen. */
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

/** The Forward Books wall — one panel, shared verbatim by Mission Control's
 * center zone and the full-width `forward` screen (same data, same cards). */
function ForwardBooksPanel({ fb, facet }: { fb: Polled<ForwardBooks>; facet: Facet }) {
  return (
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
  )
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

/* ---------- Screen bodies ---------- */

/** Mission Control — the Phase-2 grid, verbatim. The funnel poll lives HERE
 * (not in App): it is a per-screen poll and dies with its screen; only the
 * plan's permanent roster (health, heartbeats, gate, forward-books, attention)
 * outlives a screen switch. Zones B/D/E slot in with Tasks 16/17/20. */
function MissionControlScreen({
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
  return (
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
        <CohortsSection key={`cohorts|${facet}`} facet={facet} wake={wake} />
        <PerformanceSection
          key={`perf|${facet}|${win}|${playType}`}
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
  )
}

/** A screen Tasks 15-20 will fill in — the Phase-2 "visible commitments, dead
 * controls beat absent ones" posture: the slot exists, named, honest about when
 * it goes live. */
function PlaceholderScreen({ title, task }: { title: string; task: number }) {
  return (
    <main className="grid-single">
      <section className="panel">
        <div className="panel-head">{title}</div>
        <div className="panel-wait">
          not built yet — lands with Task {task} of the Phase 3 plan
        </div>
      </section>
    </main>
  )
}

/** Screen 4: the Forward Books wall, full-width (same panel as Mission
 * Control's center zone — more columns, same cards, same poll). */
function ForwardScreen({ fb, facet }: { fb: Polled<ForwardBooks>; facet: Facet }) {
  return (
    <main className="grid-single">
      <ForwardBooksPanel fb={fb} facet={facet} />
    </main>
  )
}

/** One playbook's integrity row: sidecar health, md health, drift, cadence.
 * UNKNOWN states render dashed-dim, errors amber — never a fabricated ok. */
function PlaybookIntegrityRow({ book }: { book: PlaybookBook }) {
  const verdictsCell =
    book.verdicts_error === null ? (
      <span className="pbint-cell">{book.verdicts.length} verdicts</span>
    ) : book.verdicts_error === 'missing' ? (
      <span className="pbint-cell">no verdicts yet</span>
    ) : (
      <span className="pbint-cell err">verdicts {book.verdicts_error}</span>
    )
  const drift =
    book.drift === null ? (
      <span className="pbint-unknown">drift unknown</span>
    ) : book.drift.ok ? (
      <span className="pbint-cell">md in sync</span>
    ) : (
      <span
        className="pbint-due"
        title={book.drift.missing
          .map((m) => `${m.token} missing from ${m.tier}`)
          .join('\n')}
      >
        drift · {book.drift.missing.length} missing
      </span>
    )
  return (
    <div className="pbint-row">
      <span className="pbint-name">{book.play_type}</span>
      {verdictsCell}
      {book.md_error !== null && <span className="pbint-cell err">md {book.md_error}</span>}
      {drift}
      <span className="pbint-cell right">
        last reflected {book.frontmatter.last_reflected ?? 'never'}
      </span>
      {book.reflection_due && <span className="pbint-due">reflection due</span>}
    </div>
  )
}

/** Screen 9: the heartbeat rail plus playbook integrity, full-width. The
 * playbooks poll is per-screen — it mounts here and dies with the screen. */
function SystemsScreen({ beats, wake }: { beats: Polled<Heartbeat[]>; wake: number }) {
  const playbooks = usePolling(getPlaybooks, POLL_MS, wake)
  return (
    <main className="grid-2">
      <section className="panel">
        <div className="panel-head">SYSTEMS</div>
        <PanelBody polled={beats} noun="heartbeats">
          {(data) => <HeartbeatRail beats={data} />}
        </PanelBody>
      </section>

      <section className="panel">
        <div className="panel-head">
          PLAYBOOK INTEGRITY
          <span className="panel-caption">numbers come from the sidecar, prose from the md</span>
        </div>
        <PanelBody polled={playbooks} noun="playbook integrity">
          {(data) => (
            <>
              {data.books.map((b) => (
                <PlaybookIntegrityRow key={b.play_type} book={b} />
              ))}
            </>
          )}
        </PanelBody>
      </section>
    </main>
  )
}

export default function App() {
  const wake = useEventWake() // called ONCE; every poll rides the same counter

  const [screen, setScreen] = useState<ScreenId>('mission')
  const [facet, setFacet] = useState<Facet>('research')
  const [cost, setCost] = useState<CostLevel>('0.05')
  const [win, setWin] = useState<Window>('all')
  const [playType, setPlayType] = useState<PlayType>('all')
  // Lives here, not in PerformancePanel: local state would be reset by the
  // key-remount blast every time facet/window/play-type flips — and screen
  // switches unmount Mission Control entirely, so its selections live here too.
  const [tab, setTab] = useState<BreakdownTab>('timeframe')

  // The 1-9 keys, one App-level listener (plan scope decision 9): digits follow
  // the design's fixed numbering; typing contexts and modifier chords bail so a
  // digit in a form field (or Ctrl+1 in the browser) never switches screens.
  // The `/` palette, j/k rows, and Enter drill-in are deferred together.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
      if (isTypingContext(e.target)) return
      const hit = SCREENS.find((s) => s.digit === e.key)
      if (hit !== undefined) setScreen(hit.id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  /* The PERMANENT poll roster (plan scope decision 15) — exactly these five
     outlive a screen switch: health (masthead + db-down card), heartbeats
     (masthead caution lamp), gate (masthead chips + DISARM enablement via
     broker_configured), forward-books (the Needs-Your-Hand strip — hoisted in
     WithForwardBooks below), attention (the strip's proposal/reflection/
     analysis items, consumed by Tasks 19-20). Everything else mounts with its
     screen and dies with it. Roster math: forward-books + gate + attention is
     ~1.3s per 60s server-side; the heavy per-screen endpoints only poll while
     visible. */
  const health = usePolling(getHealth, POLL_MS, wake)
  const beats = usePolling(getHeartbeats, POLL_MS, wake)
  const gate = usePolling(getGate, POLL_MS, wake)
  const attention = usePolling(getAttention, POLL_MS, wake)

  const asOf = latest(
    health.lastFetched,
    beats.lastFetched,
    gate.lastFetched,
    attention.lastFetched,
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
        onReference={() => setScreen('reference')}
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
                  "nothing needs your hand" while a book settled during the outage.
                  The strip persists across EVERY screen, above the switch. */}
              <NeedsHandStrip
                cards={fb.error === null ? (fb.data?.cards ?? null) : null}
              />

              {screen === 'mission' && (
                <MissionControlScreen
                  beats={beats}
                  fb={fb}
                  facet={facet}
                  wake={wake}
                  win={win}
                  playType={playType}
                  tab={tab}
                  onWin={setWin}
                  onPlayType={setPlayType}
                  onTab={setTab}
                />
              )}
              {screen === 'candidates' && (
                <PlaceholderScreen title="CANDIDATES" task={17} />
              )}
              {screen === 'positions' && (
                <PlaceholderScreen title="POSITIONS & LEDGER" task={16} />
              )}
              {screen === 'forward' && <ForwardScreen fb={fb} facet={facet} />}
              {screen === 'playbooks' && (
                <PlaceholderScreen title="PLAYBOOKS" task={18} />
              )}
              {screen === 'analyst' && <PlaceholderScreen title="ANALYST" task={19} />}
              {screen === 'safety' && (
                <PlaceholderScreen title="EXECUTION SAFETY" task={15} />
              )}
              {screen === 'weather' && (
                <PlaceholderScreen title="MARKET WEATHER" task={20} />
              )}
              {screen === 'systems' && <SystemsScreen beats={beats} wake={wake} />}
              {screen === 'reference' && (
                <PlaceholderScreen title="REFERENCE" task={20} />
              )}
            </>
          )}
        </WithForwardBooks>
      )}
    </div>
  )
}
