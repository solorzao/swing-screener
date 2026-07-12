import { useCallback, useEffect, useState } from 'react'
import {
  POLL_MS,
  getAttention,
  getForwardBooks,
  getGate,
  getHealth,
  getHeartbeats,
  getTicker,
  useEventWake,
  usePolling,
} from './lib/api'
import type { Facet, PlayType, Window } from './lib/api'
import { SCREENS } from './lib/screens'
import type { ScreenId } from './lib/screens'
import { EventTicker } from './components/EventTicker'
import { Masthead } from './components/Masthead'
import type { CostLevel } from './components/Masthead'
import { NeedsHandStrip } from './components/NeedsHandStrip'
import type { BreakdownTab } from './components/PerformancePanel'
import { AnalystScreen } from './screens/AnalystScreen'
import { CandidatesScreen } from './screens/CandidatesScreen'
import { ForwardScreen } from './screens/ForwardScreen'
import { JournalScreen } from './screens/JournalScreen'
import { GexLabScreen } from './screens/GexLabScreen'
import { MissionControlScreen } from './screens/MissionControlScreen'
import { PlaceholderScreen } from './screens/PlaceholderScreen'
import { PlaybooksScreen } from './screens/PlaybooksScreen'
import { PositionsScreen } from './screens/PositionsScreen'
import { ReferenceScreen } from './screens/ReferenceScreen'
import { SafetyScreen } from './screens/SafetyScreen'
import { SystemAuditScreen } from './screens/SystemAuditScreen'
import { SystemsScreen } from './screens/SystemsScreen'
import { WeatherScreen } from './screens/WeatherScreen'

/** The last analysis id the user has SEEN (localStorage; scope decision 14 —
 * "unread" is client-side, no migration). Missing / unparseable reads 0, so a
 * fresh install treats every existing analysis as already-seen only once it
 * views the screen; a NEW id past this is the unread nudge. */
const SEEN_ANALYSIS_KEY = 'cockpit.analysis.lastSeenId'

function readSeenAnalysis(): number {
  try {
    const raw = window.localStorage.getItem(SEEN_ANALYSIS_KEY)
    const n = raw === null ? 0 : Number.parseInt(raw, 10)
    return Number.isFinite(n) ? n : 0
  } catch {
    return 0 // storage disabled — the nudge just never fires, never crashes
  }
}

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

export default function App() {
  const wake = useEventWake() // called ONCE; every poll rides the same counter

  const [screen, setScreen] = useState<ScreenId>('mission')
  const [facet, setFacet] = useState<Facet>('research')
  const [cost, setCost] = useState<CostLevel>('0.05')
  const [win, setWin] = useState<Window>('all')
  const [playType, setPlayType] = useState<PlayType>('all')
  // Lives here, not in PerformancePanel: a screen switch unmounts Mission
  // Control entirely, and the breakdown-tab choice must survive the round trip.
  const [tab, setTab] = useState<BreakdownTab>('timeframe')
  // Zone D → Candidates prefill hand-off: a LOG click on a Mission Control pick
  // card sets the signal id here and navigates to Candidates, which consumes it
  // once into its local logging state. `clearPrefill` is stable so the consuming
  // effect settles in one pass.
  const [prefillSignalId, setPrefillSignalId] = useState<number | null>(null)
  const clearPrefill = useCallback(() => setPrefillSignalId(null), [])
  const onLogPick = useCallback((signalId: number) => {
    setPrefillSignalId(signalId)
    setScreen('candidates')
  }, [])
  // The unread-analysis nudge (scope decision 14): last-seen id in localStorage,
  // mirrored to state so the strip re-renders when it changes. Viewing the
  // Analyst screen marks the latest id seen (below) — reached by digit 6, the
  // strip item, or the masthead spend chip, all through `screen === 'analyst'`.
  const [seenAnalysisId, setSeenAnalysisId] = useState<number>(readSeenAnalysis)

  // The 1-9 keys, one App-level listener (plan scope decision 9): digits follow
  // the design's fixed numbering (the SCREENS registry); typing contexts and
  // modifier chords bail so a digit in a form field (or Ctrl+1 in the browser)
  // never switches screens. `/` palette, j/k rows, Enter drill-in: deferred.
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

  /* The PERMANENT poll roster (plan scope decision 15) — exactly these SIX
     outlive a screen switch, feeding the three persistent chrome elements
     (masthead · Needs-Your-Hand strip · Zone E ticker): health (masthead +
     db-down card), heartbeats (masthead caution lamp + both SYSTEMS panels),
     gate (masthead chips + DISARM enablement via broker_configured),
     forward-books (the Needs-Your-Hand strip's source, shared with the Mission
     Control panel and the forward screen — one ~1s fetch, three consumers),
     attention (the strip's proposal/reflection/analysis items — Tasks 19-20),
     and ticker (Zone E's merged event feed — the always-visible bottom strip;
     its poll joins the permanent roster because the strip is on every screen).
     Everything else mounts with its screen and dies with it. A facet flip
     re-params ONLY the fb poll (usePolling's paramsKey — no remount, so
     screens that ignore facet keep their form/scroll state). Roster math:
     forward-books + gate + attention + ticker is a few light reads per 60s
     server-side; the heavy per-screen endpoints only poll while visible. */
  const health = usePolling(getHealth, POLL_MS, wake)
  const beats = usePolling(getHeartbeats, POLL_MS, wake)
  const gate = usePolling(getGate, POLL_MS, wake)
  const attention = usePolling(getAttention, POLL_MS, wake)
  const fb = usePolling(() => getForwardBooks(facet), POLL_MS, wake, facet)
  const ticker = usePolling(getTicker, POLL_MS, wake)

  const asOf = latest(
    health.lastFetched,
    beats.lastFetched,
    gate.lastFetched,
    attention.lastFetched,
    fb.lastFetched,
    ticker.lastFetched,
  )
  // Stale heartbeats must not feed the caution lamp: on any fetch error the
  // masthead sees null and shows UNKNOWN instead of yesterday's green. The gate
  // chip takes the same hard line — stale READY is worse than "…".
  const mastheadBeats = beats.error === null ? beats.data : null
  const mastheadGate = gate.error === null ? gate.data : null

  // Unread analysis (scope decision 14): the freshest attention id vs the seen
  // id. Last-good attention is fine for a low-stakes nudge — worse case it lags
  // a minute. Viewing the Analyst screen marks the latest seen (any entry
  // route funnels through `screen === 'analyst'`), which clears the nudge.
  const latestAnalysisId = attention.data?.latest_analysis_id ?? null
  const analysisUnread = latestAnalysisId !== null && latestAnalysisId > seenAnalysisId
  useEffect(() => {
    if (screen !== 'analyst' || latestAnalysisId === null) return
    if (latestAnalysisId <= seenAnalysisId) return
    setSeenAnalysisId(latestAnalysisId)
    try {
      window.localStorage.setItem(SEEN_ANALYSIS_KEY, String(latestAnalysisId))
    } catch {
      /* storage disabled — state still clears the nudge for this session */
    }
  }, [screen, latestAnalysisId, seenAnalysisId])

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
        screen={screen}
        onNavigate={setScreen}
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
        <>
          {/* The masthead's hard line, not PanelBody's stale-dim: on a fetch
              error the strip sees null and shows "…" — last-good content at
              full brightness could be a stale EMPTY state reading as a fresh
              "nothing needs your hand" while a book settled during the outage.
              The strip persists across EVERY screen, above the switch. */}
          <NeedsHandStrip
            cards={fb.error === null ? (fb.data?.cards ?? null) : null}
            attention={attention.data}
            analysisUnread={analysisUnread}
            onNavigate={setScreen}
          />

          {screen === 'mission' ? (
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
              onLogPick={onLogPick}
            />
          ) : screen === 'candidates' ? (
            <CandidatesScreen
              facet={facet}
              wake={wake}
              prefillSignalId={prefillSignalId}
              onPrefillConsumed={clearPrefill}
            />
          ) : screen === 'positions' ? (
            <PositionsScreen wake={wake} />
          ) : screen === 'playbooks' ? (
            <PlaybooksScreen wake={wake} />
          ) : screen === 'analyst' ? (
            <AnalystScreen wake={wake} />
          ) : screen === 'forward' ? (
            <ForwardScreen fb={fb} facet={facet} />
          ) : screen === 'safety' ? (
            <SafetyScreen wake={wake} />
          ) : screen === 'systems' ? (
            <SystemsScreen beats={beats} wake={wake} />
          ) : screen === 'weather' ? (
            <WeatherScreen wake={wake} />
          ) : screen === 'gexlab' ? (
            <GexLabScreen wake={wake} />
          ) : screen === 'reference' ? (
            <ReferenceScreen wake={wake} />
          ) : screen === 'journal' ? (
            <JournalScreen wake={wake} />
          ) : screen === 'systemaudit' ? (
            <SystemAuditScreen wake={wake} />
          ) : (
            // Unreachable: every ScreenId has an explicit branch above (Task 20
            // built the last two). The placeholder survives as the defensive
            // default so a future ScreenId can never render blank.
            <PlaceholderScreen id={screen} />
          )}
        </>
      )}

      {/* Zone E — the merged event ticker: the third persistent chrome element,
          rendered OUTSIDE the db-down branch so it (like the masthead) is on
          every screen and every state. Its poll rides the permanent roster
          above; a db outage / fetch error keeps the last-good feed, or "…" when
          nothing has been fetched yet. */}
      <EventTicker feed={ticker} />
    </div>
  )
}
