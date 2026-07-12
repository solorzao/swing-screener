/* The screen registry (Phase 3, Task 14).

   There is deliberately NO router: StaticFiles(html=True) is not an SPA
   fallback (a deep link would 404) and pywebview loads the root URL once —
   screens are App STATE, switched by the 1-9 keys per the design's fixed
   numbering. Screen 10 (Reference) has no digit; it rides the masthead link.

   This registry is the ONE source of screen-naming truth: the keydown map,
   the masthead current-screen indicator, and the placeholder titles all
   render from it — a screen renamed here is renamed everywhere. */

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

export interface ScreenDef {
  id: ScreenId
  /** The 1-9 jump key; null = no digit (Reference is masthead-link only). */
  digit: string | null
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
  { id: 'weather', digit: '8', title: 'MARKET WEATHER', task: 20 },
  { id: 'systems', digit: '9', title: 'SYSTEMS' },
  { id: 'reference', digit: null, title: 'REFERENCE', task: 20 },
]

const BY_ID = new Map(SCREENS.map((s) => [s.id, s]))

/** Registry lookup — total over ScreenId by construction (every union member
 * has a row above; the throw is a build-time-typo tripwire, not a live path). */
export function screenDef(id: ScreenId): ScreenDef {
  const def = BY_ID.get(id)
  if (def === undefined) throw new Error(`unknown screen: ${id}`)
  return def
}

/** The display NUMBER (design numbering): the digit key, or '10' for the
 * digitless Reference screen. */
export const screenNumber = (def: ScreenDef): string => def.digit ?? '10'
