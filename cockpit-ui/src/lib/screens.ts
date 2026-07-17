/* The screen registry (Phase 3, Task 14).

   There is deliberately NO router: StaticFiles(html=True) is not an SPA
   fallback (a deep link would 404) and pywebview loads the root URL once —
   screens are App STATE. The nine design-numbered screens ride the 1-9 keys;
   the five digitless screens (Reference, Journal, GEX Lab, System Audit,
   Metrics) ride registry-rendered masthead links AND the `g`-leader chord:
   press `g`, then the screen's chord letter (600 ms window, bails in typing
   contexts exactly like the digits).

   This registry is the ONE source of screen-naming truth: the keydown map,
   the chord map, the masthead links + current-screen indicator, and the
   placeholder titles all render from it — a screen renamed here is renamed
   everywhere. */

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
  | 'journal'
  | 'gexlab'
  | 'systemaudit'
  | 'metrics'

export interface ScreenDef {
  id: ScreenId
  /** The 1-9 jump key; null = no digit (digitless screens ride the masthead
   * links and the g-chord). */
  digit: string | null
  /** The g-leader chord letter (digitless screens only): g then this key. */
  chord?: string
  title: string
  /** The Phase-3 plan task that builds the screen; absent = already built.
   * PlaceholderScreen renders from this — it disappears as tasks land. */
  task?: number
}

export const SCREENS: ScreenDef[] = [
  { id: 'mission', digit: '1', title: 'MISSION CONTROL' },
  { id: 'candidates', digit: '2', title: 'CANDIDATES' },
  { id: 'positions', digit: '3', title: 'POSITIONS & LEDGER' },
  { id: 'forward', digit: '4', title: 'FORWARD BOOKS' },
  { id: 'playbooks', digit: '5', title: 'PLAYBOOKS' },
  { id: 'analyst', digit: '6', title: 'ANALYST' },
  { id: 'safety', digit: '7', title: 'EXECUTION SAFETY' },
  { id: 'weather', digit: '8', title: 'MARKET WEATHER' },
  { id: 'systems', digit: '9', title: 'SYSTEMS' },
  { id: 'reference', digit: null, chord: 'r', title: 'REFERENCE' },
  { id: 'journal', digit: null, chord: 'j', title: 'JOURNAL' },
  { id: 'gexlab', digit: null, chord: 'x', title: 'GEX LAB' },
  { id: 'systemaudit', digit: null, chord: 'a', title: 'SYSTEM AUDIT' },
  { id: 'metrics', digit: null, chord: 'm', title: 'METRICS' },
]

/** The g-leader itself — one place, so App's keydown and the masthead hint
 * can never disagree. */
export const CHORD_LEADER = 'g'

const BY_ID = new Map(SCREENS.map((s) => [s.id, s]))

/** Registry lookup — total over ScreenId by construction (every union member
 * has a row above; the throw is a build-time-typo tripwire, not a live path). */
export function screenDef(id: ScreenId): ScreenDef {
  const def = BY_ID.get(id)
  if (def === undefined) throw new Error(`unknown screen: ${id}`)
  return def
}

/** The display NUMBER (design numbering): the digit key for the 1-9 screens, or
 * a sequential 10, 11, … for the digitless screens — derived from registry
 * order, so a new digitless screen never collides on a hard-coded number. */
const DIGITLESS = SCREENS.filter((s) => s.digit === null)
export const screenNumber = (def: ScreenDef): string =>
  def.digit ?? String(10 + DIGITLESS.indexOf(def))
