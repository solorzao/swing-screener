import type { BracketState } from '../lib/api'

/* The venue-truth lamp (cockpit/common._bracket states). Shape redundancy per
   the colorblind rule: armed = filled circle, db-only = amber triangle,
   unprotected = red square, unknown = dashed hollow circle. UNKNOWN is never
   green — absence of evidence is not protection. */

const TITLES: Record<BracketState, string> = {
  armed: 'a live protective sell stop is resting at the venue',
  'db-only':
    'no live stop at the venue — only the recorded ExecutionLog level ' +
    '(the level DISARM restores from)',
  unprotected: 'no stop anywhere — not at the venue, not recorded; needs your hand',
  unknown: 'no broker snapshot — venue truth unknown, never assumed',
}

export function BracketLamp({ state }: { state: BracketState }) {
  return (
    <span className={`bl bl-${state}`} title={TITLES[state]}>
      <span className="bl-lamp" aria-hidden="true" />
      {state}
    </span>
  )
}
