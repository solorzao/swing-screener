import { useCallback, useEffect, useState } from 'react'
import {
  POLL_MS,
  getAttention,
  getForwardBooks,
  getGate,
  getHealth,
  getHeartbeats,
  useEventWake,
  usePolling,
} from './lib/api'
import type { Facet, PlayType, Window } from './lib/api'
import { SCREENS } from './lib/screens'
import type { ScreenId } from './lib/screens'
import { Masthead } from './components/Masthead'
import type { CostLevel } from './components/Masthead'
import { NeedsHandStrip } from './components/NeedsHandStrip'
import type { BreakdownTab } from './components/PerformancePanel'
import { CandidatesScreen } from './screens/CandidatesScreen'
import { ForwardScreen } from './screens/ForwardScreen'
import { MissionControlScreen } from './screens/MissionControlScreen'
import { PlaceholderScreen } from './screens/PlaceholderScreen'
import { PositionsScreen } from './screens/PositionsScreen'
import { SafetyScreen } from './screens/SafetyScreen'
import { SystemsScreen } from './screens/SystemsScreen'

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

  /* The PERMANENT poll roster (plan scope decision 15) — exactly these five
     outlive a screen switch: health (masthead + db-down card), heartbeats
     (masthead caution lamp + both SYSTEMS panels), gate (masthead chips +
     DISARM enablement via broker_configured), forward-books (the
     Needs-Your-Hand strip's source, shared with the Mission Control panel and
     the forward screen — one ~1s fetch, three consumers), attention (the
     strip's proposal/reflection/analysis items, consumed by Tasks 19-20).
     Everything else mounts with its screen and dies with it. A facet flip
     re-params ONLY the fb poll (usePolling's paramsKey — no remount, so
     screens that ignore facet keep their form/scroll state). Roster math:
     forward-books + gate + attention is ~1.3s per 60s server-side; the heavy
     per-screen endpoints only poll while visible. */
  const health = usePolling(getHealth, POLL_MS, wake)
  const beats = usePolling(getHeartbeats, POLL_MS, wake)
  const gate = usePolling(getGate, POLL_MS, wake)
  const attention = usePolling(getAttention, POLL_MS, wake)
  const fb = usePolling(() => getForwardBooks(facet), POLL_MS, wake, facet)

  const asOf = latest(
    health.lastFetched,
    beats.lastFetched,
    gate.lastFetched,
    attention.lastFetched,
    fb.lastFetched,
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
          <NeedsHandStrip cards={fb.error === null ? (fb.data?.cards ?? null) : null} />

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
          ) : screen === 'forward' ? (
            <ForwardScreen fb={fb} facet={facet} />
          ) : screen === 'safety' ? (
            <SafetyScreen wake={wake} />
          ) : screen === 'systems' ? (
            <SystemsScreen beats={beats} wake={wake} />
          ) : (
            // Everything else is a Task 15-20 placeholder; each future task
            // replaces its branch with an import + one switch line here.
            <PlaceholderScreen id={screen} />
          )}
        </>
      )}
    </div>
  )
}
